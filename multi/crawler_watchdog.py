#!/usr/bin/env python3
"""Independent Telegram watchdog for a PANDI crawler production process.

This is a read-only process monitor: it does not restart the crawler,
change SQLite, or send BIMA alerts. Requires only Python 3.9+ stdlib.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import time
from urllib.parse import quote, urlsplit
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger('pandi.crawler.watchdog')
DEFAULT_HOME = Path('/home/fauzi/crawler/multi')
DEFAULT_DB = DEFAULT_HOME / 'state/crawler_production.sqlite3'
DEFAULT_SCRIPT = DEFAULT_HOME / 'crawler_multi.py'


def now_local() -> str:
    return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def parse_chat_ids(value: str) -> list[str]:
    seen = set()
    result = []
    for item in value.split(','):
        s = item.strip()
        if not s:
            continue
        if not re.fullmatch(r'-?\d+', s) or s == '0':
            raise ValueError('TELEGRAM_ALLOWED_CHAT_IDS harus berisi angka, dipisah koma')
        if s not in seen:
            result.append(s)
            seen.add(s)
    return result


def argument_db(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg == '--db' and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith('--db='):
            return arg.split('=', 1)[1]
    return None


def matches_production_command(args: list[str], script: str, db: str) -> bool:
    if not any(a == script for a in args):
        return False
    found_db = argument_db(args)
    return bool(found_db and os.path.realpath(found_db) == os.path.realpath(db))


def find_production_pids(script: str, db: str, proc_root: str = '/proc') -> list[int]:
    """Match the real Python child, not runuser, grep, or smoke tests."""
    pids = []
    current_uid = os.geteuid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        proc_path = Path(proc_root) / entry
        try:
            if proc_path.stat().st_uid != current_uid:
                continue
            data = (proc_path / 'cmdline').read_bytes()
            args = [part.decode('utf-8', errors='replace')
                    for part in data.split(b'\0') if part]
            if matches_production_command(args, script, db):
                pids.append(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
    return sorted(pids)


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    """Do not log the request URL: it contains the bot token."""
    # The user's server has confirmed Telegram GET connectivity. Keep messages
    # short, and never log the full URL (it contains the bot token).
    params = urlencode({
        'chat_id': chat_id,
        'text': message[:3900],
        'disable_web_page_preview': 'true',
    })
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage?{params}', method='GET')
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise RuntimeError('Telegram sendMessage: ok=false')


def safe_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f'HTTP {exc.code}'
    if isinstance(exc, URLError):
        return f'URLError/{type(getattr(exc, "reason", None)).__name__}'
    return type(exc).__name__


def default_state() -> dict:
    return {
        'phase': 'unknown',
        'misses': 0,
        'down_sent_to': [],
        'recovered_sent_to': [],
        'down_at': None,
        'last_seen_running': None,
    }


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('Invalid state')
    except FileNotFoundError:
        return default_state()
    except (ValueError, OSError):
        LOG.warning('Watchdog state corrupt/unreadable; using clean state')
        return default_state()
    state = default_state()
    state.update(data)
    if not isinstance(state.get('down_sent_to'), list):
        state['down_sent_to'] = []
    if not isinstance(state.get('recovered_sent_to'), list):
        state['recovered_sent_to'] = []
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'{path.name}.tmp.{os.getpid()}')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as writer:
            json.dump(state, writer, sort_keys=True, indent=2)
            writer.write('\n')
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def deliver_once(chat_ids: list[str], delivered: list[str], text: str, send) -> tuple[list[str], bool]:
    """Record each successful recipient so partial failures do not cause spam."""
    notified = list(delivered)
    failures = False
    for chat_id in chat_ids:
        if chat_id in notified:
            continue
        try:
            send(chat_id, text)
            notified.append(chat_id)
        except Exception as error:
            LOG.error('Telegram notification failed for a chat: %s', safe_error(error))
            failures = True
    return notified, failures


def evaluate(state: dict, *, pids: list[int], maintenance: bool,
             chat_ids: list[str], misses_required: int, send,
             timestamp: str, hostname: str) -> tuple[dict, bool]:
    """One watchdog tick. Return (new_state, had_notification_failure)."""
    current = dict(state)
    current['down_sent_to'] = list(state.get('down_sent_to') or [])
    current['recovered_sent_to'] = list(state.get('recovered_sent_to') or [])
    if maintenance:
        if current.get('phase') != 'maintenance':
            LOG.info('Maintenance marker found; notifications paused')
        current.update(phase='maintenance', misses=0, down_sent_to=[],
                       recovered_sent_to=[], down_at=None)
        return current, False

    if pids:
        current['last_seen_running'] = timestamp
        current['misses'] = 0
        prior_down_recipients = list(current['down_sent_to'])
        failed = False
        if prior_down_recipients:
            message = (
                '✅ PANDI CRAWLER PULIH\n'
                f'Server: {hostname}\n'
                f'Waktu: {timestamp}\n'
                f'PID crawler produksi: {", ".join(map(str, pids[:5]))}\n'
                'Proses produksi kembali berjalan.'
            )
            current['recovered_sent_to'], failed = deliver_once(
                prior_down_recipients, current['recovered_sent_to'], message, send)
        if prior_down_recipients and set(prior_down_recipients).issubset(current['recovered_sent_to']):
            current['down_sent_to'] = []
            current['recovered_sent_to'] = []
            current['down_at'] = None
        current['phase'] = 'running'
        LOG.info('Crawler detected running: pid=%s', ','.join(map(str, pids[:5])))
        return current, failed

    if current.get('phase') != 'down':
        current.update(misses=0, down_sent_to=[], recovered_sent_to=[], down_at=timestamp)
    current['phase'] = 'down'
    current['misses'] = int(current.get('misses') or 0) + 1
    if current['misses'] < misses_required:
        LOG.warning('Production crawler absent: check %d/%d', current['misses'], misses_required)
        return current, False

    message = (
        '🚨 PANDI CRAWLER BERHENTI\n'
        f'Server: {hostname}\n'
        f'Terdeteksi: {timestamp}\n'
        f'Proses tidak ditemukan selama {current["misses"]} pemeriksaan.\n'
        f'Terakhir terpantau berjalan: {current.get("last_seen_running") or "belum tercatat"}\n'
        'Silakan periksa crawler produksi di server.'
    )
    current['down_sent_to'], failed = deliver_once(
        chat_ids, current['down_sent_to'], message, send)
    if not failed:
        LOG.warning('Crawler is DOWN; alert recipients=%d', len(current['down_sent_to']))
    return current, failed


def open_readonly(db_path: str):
    if not Path(db_path).is_file():
        raise FileNotFoundError('Database produksi belum tersedia')
    uri = 'file:' + quote(str(Path(db_path).resolve()), safe='/') + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=3000')
    conn.execute('PRAGMA query_only=ON')
    return conn


def read_activity_snapshot(db_path: str) -> dict:
    """Read-only status aggregate. Avoid changing indexes on a production DB."""
    with open_readonly(db_path) as conn:
        rows = conn.execute(
            'SELECT status, COUNT(*) n, '
            'COALESCE(SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END),0) attempted '
            'FROM jobs GROUP BY status'
        ).fetchall()
        counts = {str(row['status']): int(row['n']) for row in rows}
        attempted = sum(int(row['attempted']) for row in rows)
        return dict(counts=counts, attempted=attempted, total=sum(counts.values()))


def read_activity_examples(db_path: str, limit=5) -> dict:
    limit = max(1, min(int(limit), 8))
    with open_readonly(db_path) as conn:
        recent = conn.execute(
            "SELECT url,status,reason,risk FROM jobs "
            "WHERE attempts>0 AND status NOT IN ('pending','http_inflight','browser_queued') "
            "ORDER BY updated_at DESC,id DESC LIMIT ?", (limit,)
        ).fetchall()
        active = conn.execute(
            "SELECT url,status FROM jobs WHERE status='http_inflight' "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
        browser = conn.execute(
            "SELECT url,status FROM jobs WHERE status='browser_queued' "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
    return {'recent': [dict(row) for row in recent],
            'http': [dict(row) for row in active],
            'browser': [dict(row) for row in browser]}


def safe_activity_examples(db_path: str) -> dict:
    try:
        return read_activity_examples(db_path)
    except (OSError, sqlite3.Error) as error:
        LOG.warning('Recent URL activity unavailable: %s', type(error).__name__)
        return {'recent': [], 'http': [], 'browser': []}


def safe_domain(url: str) -> str:
    try:
        value = urlsplit(str(url))
        return (value.hostname or '(unknown)') + (value.path[:30] if value.path not in ('', '/') else '')
    except ValueError:
        return '(invalid URL)'


def activity_signature(snapshot: dict) -> list[int]:
    c = snapshot['counts']
    return [int(snapshot['attempted'])] + [int(c.get(status, 0)) for status in (
        'complete','alerted','pending_alert','inconclusive','retry_http',
        'http_inflight','browser_queued','dry_run_alert','failed')]


def format_activity(snapshot: dict, examples: dict, hostname: str, timestamp: str,
                    delta_attempted: int) -> str:
    c = snapshot['counts']
    lines = [
        '📊 PANDI CRAWLER | MONITORING BERKALA',
        f'Server: {hostname}',
        f'Waktu: {timestamp}',
        f'URL dicoba: {snapshot["attempted"]:,} / {snapshot["total"]:,}',
        f'Tambahan URL sejak laporan: {delta_attempted:+d}',
        f'Selesai: {c.get("complete",0):,} | Alert BIMA: {c.get("alerted",0):,}',
        f'Retry: {c.get("retry_http",0):,} | Inconclusive: {c.get("inconclusive",0):,}',
        f'HTTP aktif: {c.get("http_inflight",0):,} | Browser queued: {c.get("browser_queued",0):,}',
    ]
    if examples['http']:
        lines.append('Sedang diperiksa (HTTP): ' + ', '.join(safe_domain(r['url']) for r in examples['http']))
    if examples['browser']:
        lines.append('Antrean/proses browser: ' + ', '.join(safe_domain(r['url']) for r in examples['browser']))
    if examples['recent']:
        lines.append('Hasil tersimpan terbaru:')
        labels = {
            'alerted': 'JUDOL/BIMA terkirim',
            'pending_alert': 'JUDOL/BIMA pending',
            'complete': 'Selesai diperiksa',
            'inconclusive': 'Belum disimpulkan',
            'retry_http': 'Retry HTTP/DNS',
            'dry_run_alert': 'Judol (dry-run)',
            'failed': 'Gagal',
        }
        for r in examples['recent']:
            label = labels.get(r['status'], r['status'])
            if r['status'] == 'complete' and r['risk']:
                label += f' [{str(r["risk"])[:18]}]'
            if r['status'] in ('inconclusive','retry_http','failed') and r.get('reason'):
                label += f' ({str(r["reason"])[:35]})'
            lines.append(f'- {safe_domain(r["url"])}: {label}')
    return '\n'.join(lines)[:3850]


def evaluate_activity(state: dict, *, pids: list[int], maintenance: bool,
                      snapshot: dict | None, chat_ids: list[str], send,
                      now: float, timestamp: str, hostname: str,
                      summary_seconds: int = 300, stall_seconds: int = 900,
                      examples=None) -> tuple[dict, bool]:
    """Track progress, send periodic summary and one stall/recovery per incident."""
    cur = dict(state)
    activity = dict(state.get('activity') or {})
    cur['activity'] = activity
    failed = False
    if maintenance or not pids or snapshot is None:
        activity['last_progress_at'] = now
        activity['last_signature'] = None
        activity['stalled_to'] = []
        activity['recovery_to'] = []
        return cur, False

    sig = activity_signature(snapshot)
    process_id = str(pids[0])
    if activity.get('process_id') != process_id:
        activity['process_id'] = process_id
        activity['last_signature'] = None
        activity['last_progress_at'] = now
    before = activity.get('last_signature')
    changed = before is not None and sig != before
    if before is None or changed:
        activity['last_progress_at'] = now
    activity['last_signature'] = sig

    if changed and activity.get('stalled_to'):
        activity['recovery_pending'] = True
    if activity.get('recovery_pending'):
        message = ('✅ PANDI CRAWLER PROGRES PULIH\n'
                   f'Server: {hostname}\nWaktu: {timestamp}\n'
                   'Jumlah URL kembali berubah setelah sempat berhenti bertambah.')
        activity['recovery_to'], err = deliver_once(
            activity['stalled_to'], activity.get('recovery_to') or [], message, send)
        failed |= err
        if set(activity['stalled_to']).issubset(activity['recovery_to']):
            activity['stalled_to'] = []
            activity['recovery_to'] = []
            activity['recovery_pending'] = False

    c = snapshot['counts']
    unfinished = bool(c.get('pending',0) or c.get('http_inflight',0) or c.get('browser_queued',0))
    elapsed_without_progress = now - float(activity.get('last_progress_at') or now)
    if unfinished and elapsed_without_progress >= stall_seconds and not activity.get('recovery_pending'):
        message = ('⚠️ PANDI CRAWLER PROGRES MACET\n'
                   f'Server: {hostname}\nWaktu: {timestamp}\n'
                   f'Proses masih hidup tetapi hitungan pekerjaan tidak berubah selama '
                   f'{int(elapsed_without_progress/60)} menit.\n'
                   f'URL dicoba: {snapshot["attempted"]:,}. Cek Chrome, DNS, dan log crawler.')
        activity['stalled_to'], err = deliver_once(
            chat_ids, activity.get('stalled_to') or [], message, send)
        failed |= err

    last_summary = float(activity.get('last_summary_at') or 0)
    if now - last_summary >= summary_seconds:
        last_attempted = activity.get('last_summary_attempted')
        delta = 0 if last_attempted is None else snapshot['attempted'] - int(last_attempted)
        sample = examples() if callable(examples) else (examples or {'recent': [],'http': [],'browser': []})
        message = format_activity(snapshot, sample, hostname, timestamp, delta)
        activity['summary_to'], err = deliver_once(
            chat_ids, activity.get('summary_to') or [], message, send)
        failed |= err
        if set(chat_ids).issubset(activity['summary_to']):
            activity['last_summary_at'] = now
            activity['last_summary_attempted'] = snapshot['attempted']
            activity['summary_to'] = []
    return cur, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='PANDI crawler Telegram process watchdog')
    parser.add_argument('--test-message', action='store_true',
                        help='send a test Telegram message, leave watchdog state unchanged')
    parser.add_argument('--dry-run', action='store_true',
                        help='evaluate and log but do not send or change watchdog state')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    try:
        chats = parse_chat_ids(os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', ''))
    except ValueError as error:
        LOG.error('%s', error)
        return 2
    if not token or not chats:
        LOG.error('TELEGRAM_BOT_TOKEN dan TELEGRAM_ALLOWED_CHAT_IDS harus terisi')
        return 2

    db = os.path.realpath(os.environ.get('CRAWLER_DB_PATH', str(DEFAULT_DB)))
    script = os.path.realpath(os.environ.get('CRAWLER_SCRIPT_PATH', str(DEFAULT_SCRIPT)))
    state_file = Path(os.environ.get('CRAWLER_WATCHDOG_STATE', str(DEFAULT_HOME / 'state/.crawler_watchdog.json')))
    maintenance_file = Path(os.environ.get('CRAWLER_WATCHDOG_MAINTENANCE', str(DEFAULT_HOME / 'state/.crawler_maintenance')))
    try:
        required = int(os.environ.get('CRAWLER_WATCHDOG_MISSES', '2'))
        if not 1 <= required <= 10:
            raise ValueError()
    except ValueError:
        LOG.error('CRAWLER_WATCHDOG_MISSES harus bilangan 1..10')
        return 2

    def send(chat_id, text):
        if args.dry_run:
            LOG.info('DRY RUN: would send an alert to one authorized chat')
            return
        send_telegram_message(token, chat_id, text)

    if args.test_message:
        msg = ('✅ TEST PANDI CRAWLER WATCHDOG\n'
               f'Server: {socket.gethostname()}\n'
               f'Waktu: {now_local()}\n'
               'Koneksi notifikasi Telegram berhasil.')
        notified, failed = deliver_once(chats, [], msg, send)
        LOG.info('Test notification delivered: %d/%d', len(notified), len(chats))
        return 1 if failed else 0

    try:
        summary_min = int(os.environ.get('CRAWLER_ACTIVITY_SUMMARY_MINUTES', '5'))
        stall_min = int(os.environ.get('CRAWLER_STALL_MINUTES', '15'))
        if not (1 <= summary_min <= 1440 and 3 <= stall_min <= 1440):
            raise ValueError()
    except ValueError:
        LOG.error('CRAWLER_ACTIVITY_SUMMARY_MINUTES harus 1..1440; CRAWLER_STALL_MINUTES 3..1440')
        return 2

    # A service timer already avoids overlap, while an advisory lock also covers manual runs.
    lock_file = state_file.with_suffix(state_file.suffix + '.lock')
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open('a+b') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.warning('Another watchdog check is already running')
                return 0
            state = load_state(state_file)
            pids = find_production_pids(script, db)
            maintenance = maintenance_file.exists()
            new_state, failed = evaluate(
                state, pids=pids, maintenance=maintenance,
                chat_ids=chats, misses_required=required, send=send,
                timestamp=now_local(), hostname=socket.gethostname())
            try:
                snapshot = read_activity_snapshot(db)
            except (OSError, sqlite3.Error) as error:
                LOG.warning('Crawler activity data unavailable: %s', type(error).__name__)
                snapshot = None
            current_ts = now_local()
            new_state, activity_failed = evaluate_activity(
                new_state, pids=pids, maintenance=maintenance,
                snapshot=snapshot, chat_ids=chats, send=send,
                now=time.time(), timestamp=current_ts, hostname=socket.gethostname(),
                summary_seconds=summary_min * 60, stall_seconds=stall_min * 60,
                examples=(lambda: safe_activity_examples(db)) if snapshot else None)
            failed |= activity_failed
            if not args.dry_run:
                save_state(state_file, new_state)
            LOG.info('Watchdog result: %s; matching_production_pids=%d; checks_missed=%d',
                     new_state['phase'], len(pids), new_state['misses'])
            return 1 if failed else 0
    except (OSError, ValueError) as error:
        LOG.error('Watchdog state/process check failed: %s', safe_error(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
