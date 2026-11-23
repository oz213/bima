#!/usr/bin/env python3
"""BIMA Phase-1 monitoring. Read-only against production SQLite and NFS.

No imports from the crawler, no BIMA API calls, no restarts or migrations.
Only --tick persists a tiny *local* monitor state. Telegram is opt-in.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DB_DEFAULT = '/mnt/registrar-sandbox/bima/state/crawler_production.sqlite3'
CRAWLER_DEFAULT = '/home/fauzi/crawler-rebuild/multi/crawler_multi.py'
INPUT_DEFAULT = '/mnt/registrar-sandbox/bima/datasets/urls.json'
MODEL_DEFAULT = '/home/fauzi/bima-shadow/models/candidate'
NFS_DEFAULT = '/mnt/registrar-sandbox'
BACKUP_DEFAULT = '/mnt/registrar-sandbox/bima/backups/registry-rollover'
SERVICES = ('pandi-crawler-multi.service', 'pandi-telegram-monitor.service')
GIB = 1024**3


def check_mount(root: Path) -> str:
    """Verify actual FS type; don't touch DB if NFS has silently unmounted."""
    result = subprocess.run(
        ['findmnt', '-rn', '-T', str(root), '-o', 'FSTYPE'],
        capture_output=True, text=True, timeout=4, check=False,
    )
    if result.returncode:
        raise RuntimeError('findmnt failed')
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else 'unknown'


def service_state(unit: str) -> str:
    result = subprocess.run(
        ['systemctl', 'is-active', unit], capture_output=True,
        text=True, timeout=4, check=False,
    )
    return result.stdout.strip() or 'unknown'


def crawler_pids(script: str, db: str, proc_root: Path = Path('/proc')) -> list[int]:
    """Observe matching crawler processes; never print process arguments/secrets."""
    result = []
    try:
        entries = proc_root.iterdir()
        current_uid = os.geteuid()
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != current_uid:
                    continue
                raw = (entry / 'cmdline').read_bytes()
                argv = [v.decode('utf-8', 'replace') for v in raw.split(b'\0') if v]
                if not any(os.path.realpath(arg) == os.path.realpath(script) for arg in argv):
                    continue
                db_arg = None
                for pos, arg in enumerate(argv):
                    if arg == '--db' and pos + 1 < len(argv):
                        db_arg = argv[pos + 1]
                        break
                    if arg.startswith('--db='):
                        db_arg = arg.split('=', 1)[1]
                        break
                if db_arg and os.path.realpath(db_arg) == os.path.realpath(db):
                    result.append(int(entry.name))
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(result)


def free_gib(path: Path) -> float:
    return round(shutil.disk_usage(path).free / GIB, 2)


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve()), str(root.resolve()))) == str(root.resolve())
    except (OSError, ValueError):
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def managed_rollover_backup_count(root: Path) -> int:
    """Inventory only verified-shaped managed backup folders; no DB scans."""
    if not root.is_dir() or root.is_symlink():
        return 0
    found = 0
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        marker = folder / '.bima-registry-rollover-v3.json'
        db = folder / 'crawler_production.sqlite3'
        if marker.is_symlink() or not marker.is_file() or not db.is_file():
            continue
        try:
            if db.stat().st_size < 1:
                continue
            rec = json.loads(marker.read_text('utf-8'))
        except (OSError, ValueError):
            continue
        if rec == {'format': 'bima-registry-rollover-v3', 'cycle': folder.name}:
            found += 1
    return found


def database_snapshot(path: Path) -> dict:
    """Bounded SQL on the current-cycle queue, never PRAGMA quick_check on live NFS."""
    if not path.is_file():
        raise FileNotFoundError('database_missing')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        mode = str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        row = conn.execute(
            'SELECT a.cycle_id, a.ready, r.url_count, r.source_sha256 '
            'FROM registry_snapshot_active_v2 a '
            'JOIN registry_snapshot_runs_v2 r ON r.cycle_id=a.cycle_id '
            'WHERE a.singleton=1'
        ).fetchone()
        if not row:
            return {'journal_mode': mode, 'ready': False, 'cycle': None}
        pending = int(conn.execute(
            'SELECT COUNT(*) FROM registry_snapshot_queue_v2 WHERE pending=1'
        ).fetchone()[0])
        return {
            'journal_mode': mode,
            'cycle': str(row[0]), 'ready': bool(row[1]),
            'total': int(row[2]), 'pending': pending,
            'non_pending': int(row[2]) - pending,
            'source_sha256': str(row[3]),
        }


def collect(args) -> dict:
    """Return sanitized, read-only status; do not touch NFS DB on mount failure."""
    problems = []
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'host': socket.gethostname(),
        'services': {}, 'storage': {}, 'snapshot': None, 'issues': problems,
    }
    for unit in SERVICES:
        try:
            state = service_state(unit)
        except (OSError, subprocess.TimeoutExpired):
            state = 'unknown'
        report['services'][unit] = state
        if state != 'active':
            problems.append('service_' + ('crawler' if unit == SERVICES[0] else 'telegram') + '_inactive')

    try:
        root_free = free_gib(Path('/'))
        report['storage']['root_free_gib'] = root_free
        if root_free < args.min_root_free_gib:
            problems.append('root_disk_low')
    except OSError:
        problems.append('root_disk_unreadable')

    nfs = Path(args.nfs_root)
    try:
        fstype = check_mount(nfs)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        fstype = 'unknown'
    report['storage']['nfs_fstype'] = fstype
    if fstype not in ('nfs', 'nfs4'):
        problems.append('nfs_mount_missing')
        report['status'] = 'degraded'
        return report  # fail closed: no DB/dataset/model access on non-NFS

    try:
        nfs_free = free_gib(nfs)
        report['storage']['nfs_free_gib'] = nfs_free
        if nfs_free < args.min_nfs_free_gib:
            problems.append('nfs_disk_low')
    except OSError:
        problems.append('nfs_disk_unreadable')

    db, dataset, backups = Path(args.db), Path(args.input), Path(args.backup_root)
    if not inside(db, nfs) or not inside(dataset, nfs) or not inside(backups, nfs):
        problems.append('database_or_dataset_not_on_nfs')
        report['status'] = 'degraded'
        return report

    try:
        num = managed_rollover_backup_count(backups)
        report['storage']['managed_rollover_backups'] = num
        if num == 0:
            problems.append('rollover_backup_missing')
        elif num > 1:
            problems.append('rollover_backup_retention_over_one')
    except OSError:
        problems.append('rollover_backup_unreadable')

    try:
        observed = crawler_pids(args.crawler_script, args.db)
        report['observed_crawler_processes'] = len(observed)
        if len(observed) > 1:
            problems.append('duplicate_crawler_processes')
    except OSError:
        report['observed_crawler_processes'] = None

    try:
        report['snapshot'] = database_snapshot(db)
        snap = report['snapshot']
        if not snap.get('ready'):
            problems.append('snapshot_not_ready')
        if snap['journal_mode'] != 'delete':
            problems.append('sqlite_journal_not_delete')
        if snap.get('total') is not None and not (0 <= snap['pending'] <= snap['total']):
            problems.append('snapshot_pending_inconsistent')
    except (OSError, ValueError, sqlite3.Error):
        problems.append('database_unreadable')

    try:
        if not dataset.is_file() or dataset.stat().st_size == 0:
            problems.append('active_dataset_missing')
        elif args.verify_input_sha:
            snap = report['snapshot']
            if snap and snap.get('source_sha256'):
                report['storage']['dataset_sha_matches_cycle'] = (
                    sha256_file(dataset) == snap['source_sha256'])
                if not report['storage']['dataset_sha_matches_cycle']:
                    problems.append('dataset_sha_mismatch')
    except OSError:
        problems.append('active_dataset_unreadable')

    model = Path(args.model)
    try:
        if not model.is_dir() or not (model / 'config.json').is_file():
            problems.append('indobert_model_files_missing')
    except OSError:
        problems.append('indobert_model_files_unreadable')
    # File availability is not proof that the process has loaded IndoBERT.
    report['status'] = 'degraded' if problems else 'healthy'
    return report


def advance_progress(state: dict, report: dict, now: float, stall_seconds: int) -> bool:
    """Returns stall only when pending work hasn't decreased for the threshold."""
    snap = report.get('snapshot') or {}
    ready = snap.get('ready') and snap.get('total') is not None
    active = report.get('services', {}).get(SERVICES[0]) == 'active'
    if not ready or not active or 'database_unreadable' in report['issues']:
        state['progress'] = {}
        return False
    pending, cycle = snap['pending'], snap['cycle']
    old = state.get('progress') or {}
    last_seen = float(old.get('last_seen') or 0)
    reset = (old.get('cycle') != cycle or old.get('pending') is None
             or pending != old.get('pending') or now < last_seen
             or now - last_seen > max(20 * 60, stall_seconds * 2))
    start = now if reset else float(old.get('last_change') or now)
    state['progress'] = {'cycle': cycle, 'pending': pending,
                         'last_seen': now, 'last_change': start}
    return pending > 0 and (now - start) >= stall_seconds


def read_state(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    """Write only local StateDirectory, never to the application SQLite/NFS."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    try:
        with tmp.open('x', encoding='utf-8') as dest:
            os.chmod(tmp, 0o600)
            json.dump(state, dest, sort_keys=True)
            dest.write('\n')
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def telegram_send(token: str, chat: str, message: str) -> None:
    """POST; do not log Telegram URL or tokens."""
    data = urlencode({'chat_id': chat, 'text': message[:3000],
                      'disable_web_page_preview': 'true'}).encode('utf-8')
    req = Request('https://api.telegram.org/bot' + token + '/sendMessage',
                  data=data, method='POST')
    with urlopen(req, timeout=10) as resp:
        result = json.load(resp)
    if result.get('ok') is not True:
        raise RuntimeError('telegram_response_not_ok')


def allowed_chats(raw: str) -> list[str]:
    out = []
    for text in raw.split(','):
        chat = text.strip()
        if not chat:
            continue
        if not re.fullmatch(r'-?\d+', chat) or chat == '0':
            raise ValueError('invalid Telegram chat ID')
        if chat not in out:
            out.append(chat)
    return out


def notice_plan(state: dict, issues: list[str]) -> tuple[str | None, str | None]:
    """Two identical bad ticks before first alert, one recovery per incident."""
    key = ','.join(sorted(set(issues)))
    if key != state.get('observed_key'):
        state['observed_key'], state['observed_ticks'] = key, 1
    else:
        state['observed_ticks'] = int(state.get('observed_ticks', 0)) + 1
    if key:
        if state['observed_ticks'] < 2:
            return None, None
        notice = 'degraded:' + key
        if state.get('sent_notice') == notice:
            return None, None
        return notice, 'BIMA HEALTH DEGRADED\nMasalah: ' + ', '.join(key.split(','))
    if (state['observed_ticks'] >= 2 and
            str(state.get('sent_notice', '')).startswith('degraded:')):
        return 'healthy', 'BIMA HEALTH RECOVERED\nSeluruh pemeriksaan dasar kembali sehat.'
    return None, None


def notify_once(state: dict, key: str, msg: str, send, chats: list[str], persist=None) -> None:
    """Persist per-recipient acknowledgments across partial Telegram failures."""
    if state.get('notice_key') != key:
        state['notice_key'], state['notified_to'] = key, []
    done = set(state.get('notified_to') or [])
    for chat in chats:
        if chat in done:
            continue
        try:
            send(chat, msg)
        except Exception as exc:
            print('BIMA health: Telegram delivery error: ' + type(exc).__name__,
                  file=sys.stderr)
        else:
            done.add(chat)
            state['notified_to'] = sorted(done)
            if persist is not None:
                try:
                    persist(state)
                except OSError:
                    print('BIMA health: unable to record Telegram delivery state',
                          file=sys.stderr)
                    return
    if all(chat in done for chat in chats) and chats:
        state['sent_notice'] = key
        state['notice_key'], state['notified_to'] = None, []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=DB_DEFAULT)
    parser.add_argument('--crawler-script', default=CRAWLER_DEFAULT)
    parser.add_argument('--input', default=INPUT_DEFAULT)
    parser.add_argument('--nfs-root', default=NFS_DEFAULT)
    parser.add_argument('--backup-root', default=BACKUP_DEFAULT)
    parser.add_argument('--model', default=MODEL_DEFAULT)
    parser.add_argument('--min-root-free-gib', type=float, default=5.0)
    parser.add_argument('--min-nfs-free-gib', type=float, default=5.0)
    parser.add_argument('--stall-minutes', type=int, default=30)
    parser.add_argument('--verify-input-sha', action='store_true',
                        help='Manual deep check; hashes the full NFS JSON, not for 5-minute timer')
    parser.add_argument('--tick', action='store_true',
                        help='Persist only local monitor state to measure stalled progress')
    parser.add_argument('--notify', action='store_true',
                        help='Opt in to Telegram alerts (requires --tick and credentials)')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--state-file', default=os.path.join(
        os.environ.get('STATE_DIRECTORY', '/var/lib/pandi-bima-health'), 'state.json'))
    args = parser.parse_args(argv)
    if args.notify and not args.tick:
        parser.error('--notify requires --tick')
    if args.stall_minutes < 5 or min(args.min_root_free_gib, args.min_nfs_free_gib) < 0:
        parser.error('invalid threshold')
    if args.tick and inside(Path(args.state_file), Path(args.nfs_root)):
        parser.error('health state must stay on local disk, not NFS')
    try:
        report = collect(args)
    except Exception as exc:
        # Avoid exposing absolute secrets or arbitrary exception messages.
        report = {'checked_at': datetime.now(timezone.utc).isoformat(),
                  'services': {}, 'storage': {}, 'snapshot': None,
                  'issues': ['health_check_exception_' + type(exc).__name__],
                  'status': 'degraded'}
    if args.tick:
        state_path = Path(args.state_file)
        state = read_state(state_path)
        if advance_progress(state, report, time.time(), args.stall_minutes * 60):
            report['issues'].append('crawler_progress_stalled')
        report['status'] = 'degraded' if report['issues'] else 'healthy'
        if args.notify or os.environ.get('BIMA_HEALTH_NOTIFY', '').strip() == '1':
            try:
                chats = allowed_chats(os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', ''))
                token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
            except ValueError:
                chats, token = [], ''
            if not chats or not token:
                report['issues'].append('telegram_notification_not_configured')
                report['status'] = 'degraded'
            else:
                # Never attempt a notification if dedup state cannot be written.
                # Otherwise an unwritable StateDirectory could cause repeat spam.
                try:
                    save_state(state_path, state)
                except OSError:
                    report['issues'].append('health_state_unwritable')
                    report['status'] = 'degraded'
                else:
                    key, message = notice_plan(state, report['issues'])
                    if key:
                        safe = (message + '\nSiklus: ' + str((report.get('snapshot') or {}).get('cycle') or '-')[:80])
                        notify_once(state, key, safe,
                                    lambda chat, msg: telegram_send(token, chat, msg),
                                    chats, persist=lambda current: save_state(state_path, current))
        try:
            save_state(state_path, state)
        except OSError:
            report['issues'].append('health_state_unwritable')
            report['status'] = 'degraded'
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        snap = report.get('snapshot') or {}
        print('BIMA HEALTH:', report['status'].upper())
        print('Services:', report.get('services'))
        print('Storage:', report.get('storage'))
        print('Snapshot:', {k: snap.get(k) for k in ('cycle', 'ready', 'total', 'pending', 'non_pending')})
        print('Issues:', ', '.join(report['issues']) or 'none')
    return 0 if report['status'] == 'healthy' else 1


if __name__ == '__main__':
    sys.exit(main())
