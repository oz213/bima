#!/usr/bin/env python3
"""BIMA v4.3.9 read-only discovery/priority operational health report.

No provider requests, no credentials, no result URLs, no network, no SQLite
writes, no service management, and no automatic scheduling. The report uses
sanitized counters only, so it is suitable for private operator diagnostics.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone, time as clock_time
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys

MAX_JSONL_LINE = 2 * 1024 * 1024
EXPECTED_CRON = ('30', '20', '*', '*', '*')
SPOOL_NAME = re.compile(r'^pending-\d{8}T\d{6}Z-[A-Za-z0-9]{8}\.jsonl$')


def _last_jsonl(path: Path):
    """Read only the last complete nonblank line, never return raw log text."""
    if not path.is_file() or path.is_symlink():
        return None, 'missing'
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if not end:
                return None, 'empty'
            start = max(0, end - MAX_JSONL_LINE)
            handle.seek(start)
            data = handle.read(MAX_JSONL_LINE)
        # If the file exceeds the bounded tail window, the first line may be
        # cut off. Select the final complete line only.
        lines = [line.strip() for line in data.split(b'\n') if line.strip()]
        if not lines:
            return None, 'empty'
        last = lines[-1]
        if len(last) > MAX_JSONL_LINE:
            return None, 'oversized_last_record'
        parsed = json.loads(last.decode('utf-8'))
        if not isinstance(parsed, dict):
            return None, 'invalid_last_record'
        return parsed, 'ok'
    except (OSError, ValueError, UnicodeError):
        return None, 'invalid_last_record'


def _mtime(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _safe_number(value):
    # Avoid accidentally printing arbitrary strings from untrusted JSONL.
    return value if type(value) is int and value >= 0 else None


def _provider_summary(data):
    if not isinstance(data, dict):
        return {}
    out = {}
    for name in ('google', 'brave', 'urlscan'):
        rec = data.get(name)
        if not isinstance(rec, dict):
            continue
        status = rec.get('status')
        # Whitelist tokens. Never echo untrusted errors/queries/URLs.
        if status not in ('ok', 'error', 'planned', 'skipped_missing_credentials',
                          'skipped_missing_quota_ledger', 'skipped_monthly_cap',
                          'skipped_expired'):
            status = 'unknown'
        out[name] = {'status': status, 'requests': _safe_number(rec.get('requests')),
                     'valid_results': _safe_number(rec.get('valid_results'))}
    return out


def _database_summary(path: Path):
    if not path.is_file():
        return {'state': 'missing'}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                     timeout=3)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA busy_timeout=3000')
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return {'state': 'missing_jobs_table'}
            counts = {'pending': 0, 'retry_http': 0, 'pending_alert': 0,
                      'http_inflight': 0, 'browser_queued': 0}
            statuses = tuple(counts)
            placeholders = ','.join('?' for _ in statuses)
            for status, count in conn.execute(
                    'SELECT status, COUNT(*) FROM jobs WHERE status IN (' + placeholders + ') GROUP BY status', statuses):
                counts[status] = count
            priority = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_job_priority'"
            ).fetchone() is not None
            priorities = {}
            if priority:
                for provider, status, n in conn.execute(
                    'SELECT p.provider, j.status, COUNT(*) '
                    'FROM discovery_job_priority p JOIN jobs j ON j.id=p.job_id '
                    'GROUP BY p.provider,j.status'
                ):
                    # Only aggregate known providers and safe status tokens.
                    if provider in ('google', 'urlscan', 'brave'):
                        key = status if status in (
                            'pending', 'complete', 'inconclusive', 'alerted',
                            'http_inflight', 'browser_queued', 'retry_http',
                            'pending_alert', 'failed', 'review_required'
                        ) else 'other'
                        priorities.setdefault(provider, {})[key] = n
            return {'state': 'ok', 'jobs': counts,
                    'priority_table_exists': priority,
                    'priority_counts': priorities,
                    'active_discovery_pending': sum(x.get('pending', 0) for x in priorities.values())}
    except (OSError, sqlite3.Error):
        return {'state': 'unreadable_or_busy'}


def _spool_summary(path: Path):
    if not path.exists() and not path.is_symlink():
        return {'state': 'not_created', 'pending_files': 0, 'failed_files': 0}
    try:
        st = path.lstat()
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_mode & 0o077):
            return {'state': 'unsafe_permissions'}
        files = list(path.iterdir())
        pending = [p for p in files if SPOOL_NAME.fullmatch(p.name)]
        failed = [p for p in files if p.name.endswith('.jsonl.failed')]
        if any(not stat.S_ISREG(p.lstat().st_mode) or p.lstat().st_mode & 0o077
               or p.lstat().st_uid != os.geteuid() for p in pending):
            return {'state': 'unsafe_pending_file'}
        return {'state': 'ok', 'pending_files': len(pending), 'failed_files': len(failed),
                'pending_bytes': sum(p.stat().st_size for p in pending)}
    except OSError:
        return {'state': 'unreadable'}


def _cron_status(crontab):
    if crontab is None:
        return {'state': 'not_checked'}
    matches = []
    global_brave = False
    for line in crontab.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line == 'BIMA_ENABLE_BRAVE_DAILY=1':
            global_brave = True
        if 'run_multisource_daily.sh' not in line:
            continue
        fields = line.split()
        schedule = tuple(fields[:5])
        auto = 'BIMA_DISCOVERY_AUTO_IMPORT=1' in fields
        brave = 'BIMA_ENABLE_BRAVE_DAILY=1' in fields
        matches.append((schedule, auto, brave))
    return {
        'state': 'ok' if len(matches) == 1 and matches[0][:2] == (EXPECTED_CRON, True)
                and not matches[0][2] and not global_brave else 'attention',
        'entries': len(matches),
        'auto_import_enabled': len(matches) == 1 and matches[0][1],
        'brave_daily_enabled': global_brave or any(item[2] for item in matches),
        'schedule_matches': len(matches) == 1 and matches[0][0] == EXPECTED_CRON,
    }


def summarize(log_dir: Path, db: Path, *, now=None, crontab=None, stale_hours=36, enabled_date=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    log_dir = Path(log_dir)
    discovery_path = log_dir / 'multi-source.jsonl'
    import_path = log_dir / 'multi-source-import.jsonl'
    discovery, disc_read = _last_jsonl(discovery_path)
    imported, import_read = _last_jsonl(import_path)
    discovery_time, import_time = _mtime(discovery_path), _mtime(import_path)
    spool = _spool_summary(log_dir / 'multi-source-pending')
    database = _database_summary(Path(db))
    cron = _cron_status(crontab)
    marker = log_dir / ('multi-source-import-' + now.strftime('%Y%m%d') + '.started')
    marker_exists = marker.is_file() and not marker.is_symlink()
    # A cron run takes time; do not mark missing results as failures while
    # today's scheduled 20:30 UTC execution may still be running.
    next_run = datetime.combine(now.date(), clock_time(20, 30), timezone.utc)
    if now >= next_run:
        next_run += timedelta(days=1)
    grace_ends = datetime.combine(now.date(), clock_time(20, 30), timezone.utc) + timedelta(minutes=60)
    # If the operator supplies the UTC date when auto-import was enabled, we
    # can detect a missed first run on following days without guessing about
    # the previous days' configuration.
    expected_run = datetime.combine(now.date(), clock_time(20, 30), timezone.utc)
    if now < expected_run + timedelta(minutes=60):
        expected_run -= timedelta(days=1)
    scheduled_since_enable = enabled_date is not None and expected_run.date() >= enabled_date
    first_run_not_yet_due = not scheduled_since_enable and not marker_exists
    problems = []
    if cron.get('state') == 'attention':
        problems.append('cron_configuration')
    if database.get('state') != 'ok':
        problems.append('jobs_database')
    if spool.get('state') in ('unsafe_permissions', 'unsafe_pending_file', 'unreadable'):
        problems.append('spool_security')
    if spool.get('failed_files', 0) > 0:
        problems.append('provider_failed_spool')
    if disc_read not in ('ok', 'missing'):
        problems.append('discovery_log_format')
    if import_read not in ('ok', 'missing'):
        problems.append('import_log_format')
    if discovery is not None and discovery.get('ok') is not True:
        problems.append('discovery_run_error')
    if (discovery is not None and discovery.get('mode') == 'api_preview' and
            _provider_summary(discovery.get('providers')).get('google', {}).get('status') != 'ok'):
        problems.append('google_provider_not_ok')
    if imported is not None and imported.get('ok') is not True:
        problems.append('import_run_error')
    if imported is not None:
        n = _safe_number(imported.get('new_jobs'))
        if n is None or n > 5:
            problems.append('new_job_limit')
        if imported.get('brave_ingestion_enabled') is not False:
            problems.append('brave_ingestion_unexpected')
    if database.get('active_discovery_pending', 0) > 20:
        problems.append('discovery_priority_limit')
    if (import_time is not None and now - import_time > timedelta(hours=stale_hours)):
        problems.append('import_log_stale')
    if scheduled_since_enable and (import_time is None or import_time < expected_run):
        problems.append('scheduled_import_not_observed')
    elif import_time is None and not first_run_not_yet_due:
        problems.append('import_log_missing_after_schedule')
    status = 'attention' if problems else ('awaiting_first_run' if imported is None else 'ok')
    return {
        'version': '4.3.9', 'mode': 'read_only', 'status': status,
        'checked_at_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'scheduled_daily_utc': '20:30',
        'next_scheduled_utc': next_run.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'next_scheduled_wib': next_run.astimezone(timezone(timedelta(hours=7))).isoformat(),
        'auto_import_enabled_date_utc': enabled_date.isoformat() if enabled_date else None,
        'cron': cron,
        'last_discovery': {
            'record': disc_read,
            'mtime_utc': discovery_time.strftime('%Y-%m-%dT%H:%M:%SZ') if discovery_time else None,
            'ok': discovery.get('ok') is True if discovery else None,
            'requests': _safe_number(discovery.get('api_requests')) if discovery else None,
            'new_candidates': _safe_number(discovery.get('new_candidates')) if discovery else None,
            'providers': _provider_summary(discovery.get('providers')) if discovery else {},
        },
        'last_import': {
            'record': import_read,
            'mtime_utc': import_time.strftime('%Y-%m-%dT%H:%M:%SZ') if import_time else None,
            'ok': imported.get('ok') is True if imported else None,
            'new_jobs': _safe_number(imported.get('new_jobs')) if imported else None,
            'priority_added': _safe_number(imported.get('priority_added')) if imported else None,
            'spool_pending_after': _safe_number(imported.get('spool_files_pending_after')) if imported else None,
            'brave_ingestion_enabled': imported.get('brave_ingestion_enabled') is True if imported else None,
            'today_started_marker_exists': marker_exists,
        },
        'spool': spool,
        'database': database,
        'problems': problems,
        'no_api_calls': True, 'no_database_writes': True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', type=Path, default=Path.home() / 'bima-logs')
    parser.add_argument('--db', type=Path, default=Path('multi/state/crawler_production.sqlite3'))
    parser.add_argument('--stale-hours', type=int, default=36)
    parser.add_argument('--check-cron', action='store_true', help='Read the invoking user crontab, never display its content')
    parser.add_argument('--strict', action='store_true', help='Exit 1 on attention, useful for operator scripts')
    parser.add_argument('--enabled-date-utc', default='',
                        help='UTC date YYYY-MM-DD when the daily auto-import cron was enabled')
    args = parser.parse_args(argv)
    if not 1 <= args.stale_hours <= 168:
        parser.error('--stale-hours must be 1..168')
    crontab = None
    if args.check_cron:
        try:
            proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                                  timeout=4, check=False)
            crontab = proc.stdout if proc.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            crontab = ''
    enabled_date = None
    if args.enabled_date_utc:
        try:
            enabled_date = datetime.strptime(args.enabled_date_utc, '%Y-%m-%d').date()
        except ValueError:
            parser.error('--enabled-date-utc must be YYYY-MM-DD')
    result = summarize(args.log_dir, args.db, crontab=crontab,
                       stale_hours=args.stale_hours, enabled_date=enabled_date)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 1 if args.strict and result['status'] == 'attention' else 0


if __name__ == '__main__':
    sys.exit(main())
