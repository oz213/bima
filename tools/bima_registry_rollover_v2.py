#!/usr/bin/env python3
"""One-command BIMA Registry snapshot cutover (operator-only, Linux/systemd).

Uses the existing reviewed preview/apply CLI. It does not alter crawler model,
Telegram decisions, job IDs or alert delivery state. Fail closed on partial apply.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ('pandi-crawler-multi.service', 'pandi-telegram-monitor.service')
MIN_FREE_FOR_APPLY = 1024**3
LOCK_PATH = '/run/lock/bima-registry-rollover-v2.lock'


class CutoverError(RuntimeError):
    pass


def report(message):
    print('[BIMA rollover] ' + message, flush=True)


def command(argv, *, capture=False, timeout=None):
    result = subprocess.run(argv, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            timeout=timeout, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()[-800:]
        raise CutoverError('%s failed (exit %s)%s' %
                           (argv[0], result.returncode, ': ' + details if details else ''))
    return result.stdout if capture else ''


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def state_readonly(db):
    """Read existing cycle state without changing or creating production tables."""
    uri = Path(db).resolve(strict=True).as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.execute('PRAGMA query_only=ON')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='registry_snapshot_active_v2'").fetchone()
        if not exists:
            return {'cycle': None, 'ready': False, 'sha': None, 'url_count': None}
        row = conn.execute(
            'SELECT a.cycle_id,a.ready,r.source_sha256,r.url_count '
            'FROM registry_snapshot_active_v2 a JOIN registry_snapshot_runs_v2 r '
            'ON a.cycle_id=r.cycle_id WHERE a.singleton=1'
        ).fetchone()
    if not row:
        return {'cycle': None, 'ready': False, 'sha': None, 'url_count': None}
    return dict(zip(('cycle', 'ready', 'sha', 'url_count'),
                    (row[0], bool(row[1]), row[2], row[3])))


def decide_cycle(state, stage_sha, supplied_cycle, repeat_source=False):
    """An interrupted two-phase publication MUST resume the original cycle."""
    if state['cycle'] and not state['ready']:
        if state['sha'] != stage_sha:
            raise CutoverError('Unfinished cycle %s exists for a different source; '
                               'restore its original urls.json.next before retrying' % state['cycle'])
        if supplied_cycle and supplied_cycle != state['cycle']:
            raise CutoverError('Unfinished cycle must resume as %s' % state['cycle'])
        return state['cycle'], True
    if state['ready'] and stage_sha == state['sha'] and not repeat_source:
        raise CutoverError('Staged content equals the active cycle. Supply a new urls.json.next, '
                           'or explicitly use --repeat-source for an intentional recrawl')
    return (supplied_cycle or 'registry-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            + '-%06d' % (time.time_ns() % 1000000)), False


def service_state(service):
    r = subprocess.run(['systemctl', 'is-active', service], text=True,
                       capture_output=True, timeout=15, check=False)
    status = r.stdout.strip()
    if status not in ('active', 'inactive'):
        raise CutoverError('Service %s is %r (expected active/inactive); inspect systemctl status' %
                           (service, status))
    return status


def systemctl(action, service):
    command(['systemctl', action, service], timeout=240)


def unit_args():
    data = command(['systemctl', 'show', '-p', 'ExecStart', '--value', SERVICES[0]],
                   capture=True, timeout=15)
    match = re.search(r'argv\[\]=(.*?)(?:\s+;\s+|$)', data)
    if not match:
        raise CutoverError('Unable to inspect crawler ExecStart; verify --input/--db manually')
    return shlex.split(match.group(1))


def flag_value(tokens, key):
    for i, token in enumerate(tokens):
        if token == key and i + 1 < len(tokens):
            return tokens[i + 1]
        if token.startswith(key + '='):
            return token[len(key) + 1:]
    return None


def verify_unit_paths(input_path, db_path):
    tokens = unit_args()
    working_dir = command(['systemctl', 'show', '-p', 'WorkingDirectory', '--value',
                           SERVICES[0]], capture=True, timeout=15).strip()
    wd = Path(working_dir) if working_dir not in ('', '/') else Path('/')
    for key, expected in (('--input', input_path), ('--db', db_path)):
        specified = flag_value(tokens, key)
        if not specified:
            raise CutoverError('Cannot verify %s in crawler ExecStart; '
                               'inspect service manually (then use --skip-unit-check)' % key)
        parsed = Path(specified)
        effective = (parsed if parsed.is_absolute() else wd / parsed).resolve()
        if effective != expected.resolve():
            raise CutoverError('Crawler %s points to %s, but rollover targets %s' %
                               (key, effective, expected))


BACKUP_CODE = r'''
import os, shutil, sqlite3, sys
from contextlib import closing
from pathlib import Path
backup = Path(sys.argv[1])
backup.mkdir(mode=0o700, parents=True, exist_ok=False)
for path in (Path(sys.argv[2]), Path(sys.argv[3])):
    if not path.is_file():
        if path == Path(sys.argv[3]):
            continue
        raise RuntimeError('Required production DB missing')
    target = backup / path.name
    if target.exists():
        raise RuntimeError('Backup target already exists')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
        src.execute('PRAGMA query_only=ON')
        with closing(sqlite3.connect(str(target), timeout=30)) as dst:
            src.backup(dst, pages=2048, sleep=0.1)
    os.chmod(target, 0o600)
    with closing(sqlite3.connect(target)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup quick_check failed')
old = Path(sys.argv[4])
if old.is_file():
    shutil.copy2(old, backup / 'urls.json')
    os.chmod(backup / 'urls.json', 0o600)
print('BACKUP VERIFIED: ' + str(backup), flush=True)
'''


class Rollover:
    def __init__(self, options):
        self.args = options
        self.app = options.app.resolve()
        self.db = (options.db or self.app / 'multi/state/crawler_production.sqlite3').resolve()
        self.learning = options.learning_db or self.app / 'multi/state/bima_learning_v450.sqlite3'
        self.source = options.source or self.app / 'urls.json.next'
        self.target = options.input or self.app / 'urls.json'
        self.tool = self.app / 'tools/bima_registry_snapshot_v2.py'
        self.python = self.app / 'venv/bin/python3'
        self.backup_root = options.backup_root
        self.cycle = None
        self.initial = None
        self.preview = None
        self.service_before = None
        self.backup_dir = None

    def owner(self, *args, capture=False):
        return command(['runuser', '-u', self.args.owner, '--', *map(str, args)],
                       capture=capture)

    def invoke_tool(self, mode):
        params = [self.python, '-B', self.tool, mode, '--db', self.db]
        if mode != 'status':
            params += ['--source', self.source, '--input', self.target,
                       '--cycle', self.cycle, '--min-urls', str(self.args.min_urls)]
            if self.args.allow_major_shrink:
                params.append('--allow-major-shrink')
        return json.loads(self.owner(*params, capture=True))

    def preflight(self):
        for name, path in (('source', self.source), ('database', self.db),
                           ('snapshot tool', self.tool), ('venv Python', self.python)):
            if path.is_symlink() or not path.is_file():
                raise CutoverError('%s missing or symlink: %s' % (name, path))
        if (self.target.is_symlink() or (self.target.exists() and not self.target.is_file())
                or self.target.resolve() == self.source.resolve()
                or not self.target.parent.resolve().samefile(self.source.parent.resolve())):
            raise CutoverError('Active urls.json and urls.json.next must be regular files in same directory')
        if not self.args.skip_unit_check:
            verify_unit_paths(self.target, self.db)
        self.service_before = {s: service_state(s) for s in SERVICES}
        self.initial = state_readonly(self.db)
        # Preview validates exact BIMA URL normalization, JSON content and minimum count.
        # Use the existing cycle for an incomplete publication; otherwise create a new ID.
        stage_sha = sha256_file(self.source)
        self.cycle, resumed = decide_cycle(self.initial, stage_sha, self.args.cycle,
                                           self.args.repeat_source)
        self.preview = self.invoke_tool('preview')
        if self.preview['source_sha256'] != stage_sha:
            raise CutoverError('Source changed during preview; regenerate a stable urls.json.next')
        baseline = self.initial['url_count'] if self.initial['cycle'] and self.initial['ready'] else None
        if baseline is None and self.initial['cycle'] is None and self.target.is_file():
            # On FIRST v2 cutover, compare with the existing active urls.json,
            # not merely the minimum threshold of 100k URLs.
            previous = [self.python, '-B', self.tool, 'preview', '--db', self.db,
                        '--source', self.target, '--input', self.target,
                        '--cycle', self.cycle, '--min-urls', '1']
            baseline = json.loads(self.owner(*previous, capture=True))['unique_urls']
            report('Existing active urls.json has %s valid unique URLs' % baseline)
        if baseline is not None and not self.args.allow_major_shrink:
            if self.preview['unique_urls'] < baseline // 2:
                raise CutoverError('Snapshot contains <50%% of current %s URLs. '
                                   'Check PDNS extraction; use --allow-major-shrink only if intentional'
                                   % baseline)
        if self.backup_root.is_symlink() or not self.backup_root.parent.is_dir():
            raise CutoverError('Invalid backup root parent or symlink')
        if not self.backup_root.exists():
            self.backup_root.mkdir(mode=0o700, parents=False, exist_ok=True)
            os.chown(self.backup_root, __import__('pwd').getpwnam(self.args.owner).pw_uid,
                     __import__('pwd').getpwnam(self.args.owner).pw_gid)
        sizes = [p.stat().st_size for p in (self.db, self.learning, self.target) if p.is_file()]
        # Backup bytes plus WAL/temporary write headroom for a large 1.5M-URL transaction.
        backup_bytes = sum(sizes)
        apply_headroom = max(2 * MIN_FREE_FOR_APPLY, self.db.stat().st_size // 2)
        db_free = shutil.disk_usage(self.db.parent).free
        backup_free = shutil.disk_usage(self.backup_root).free
        if self.db.stat().st_dev == self.backup_root.stat().st_dev:
            required = backup_bytes + apply_headroom
            if db_free < required:
                raise CutoverError('Not enough free disk for backup + apply (need >= %s bytes)' % required)
        elif db_free < apply_headroom or backup_free < backup_bytes:
            raise CutoverError('Not enough disk for backup or SQLite apply')
        if sha256_file(self.source) != stage_sha:
            raise CutoverError('Staged input changed after preview')
        report('PREVIEW OK: cycle=%s urls=%s invalid=%s%s' %
               (self.cycle, self.preview['unique_urls'], self.preview['invalid'],
                ' (resuming incomplete cycle)' if resumed else ''))
        report('Services before: %s' % self.service_before)
        report('Backup directory: %s' % (self.backup_root / self.cycle))
        return stage_sha

    def backup(self):
        self.backup_dir = self.backup_root / self.cycle
        if self.backup_dir.exists():
            # A partial publication retry can reuse only a COMPLETE verified backup.
            for source in (self.db, self.learning):
                if not source.is_file():
                    continue
                expected = self.backup_dir / source.name
                if not expected.is_file():
                    raise CutoverError('Previous backup is incomplete; inspect %s' % self.backup_dir)
                with closing(sqlite3.connect(expected)) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise CutoverError('Existing backup failed integrity check')
            if self.target.is_file() and not (self.backup_dir / 'urls.json').is_file():
                raise CutoverError('Previous urls.json backup is incomplete; inspect ' + str(self.backup_dir))
            report('Existing backup verified: ' + str(self.backup_dir))
            return
        self.owner(self.python, '-B', '-c', BACKUP_CODE, self.backup_dir,
                   self.db, self.learning, self.target)

    def restored_state(self):
        current = state_readonly(self.db)
        return current['cycle'] == self.initial['cycle'] and current['ready'] == self.initial['ready']

    def restore_original_services(self):
        for s in SERVICES:
            if self.service_before[s] == 'active':
                systemctl('start', s)
        report('Original active services restarted; original DB cycle was not changed')

    def health(self):
        limit = time.monotonic() + self.args.health_timeout
        got_model = False
        pid = None
        while time.monotonic() < limit:
            statuses = {s: service_state(s) for s in SERVICES}
            if any(status != 'active' for status in statuses.values()):
                raise CutoverError('Post-start service check failed: %s' % statuses)
            current_pid = command(['systemctl', 'show', '-p', 'MainPID', '--value',
                                   SERVICES[0]], capture=True, timeout=10).strip()
            if current_pid.isdecimal() and int(current_pid) > 0:
                pid = current_pid
                logs = command(['journalctl', '--utc', '_PID=' + pid,
                                '--no-pager', '-o', 'cat', '-n', '250'],
                               capture=True, timeout=15)
                if 'IndoBERT loaded ONCE' in logs:
                    got_model = True
                    report('IndoBERT loaded ONCE: PID=' + pid)
                    break
            time.sleep(3)
        if not got_model:
            raise CutoverError('Crawler services are running, but no IndoBERT loaded ONCE log '
                               'seen in %ss. Inspect journalctl before declaring success' %
                               self.args.health_timeout)
        if any(service_state(s) != 'active' for s in SERVICES):
            raise CutoverError('A service stopped after startup')
        return pid

    def archive_staged(self):
        target = self.backup_dir / 'urls.json.next.applied'
        if target.exists():
            raise CutoverError('Archive target unexpectedly exists')
        # Archive the consumed staging file so the command cannot be re-run by
        # accident on the same urls.json.next. Active urls.json stays in place.
        self.owner('mv', '--', self.source, target)
        return target

    def execute(self):
        stage_sha = self.preflight()  # All validation before stopping services.
        stopped = False
        try:
            report('Stopping crawler and Telegram monitor...')
            stopped = True  # Restore original services even if stopping the second one fails.
            for service in SERVICES:
                if service_state(service) == 'active':
                    systemctl('stop', service)
            if any(service_state(s) != 'inactive' for s in SERVICES):
                raise CutoverError('Services not completely stopped')
            report('Creating verified SQLite backup...')
            self.backup()
            if sha256_file(self.source) != stage_sha:
                raise CutoverError('Source changed before apply; keep original services stopped')
            report('Applying Registry snapshot (may take substantial disk I/O)...')
            result = self.invoke_tool('apply')
            current = state_readonly(self.db)
            if (current['cycle'] != self.cycle or not current['ready'] or
                    current['sha'] != stage_sha or sha256_file(self.target) != stage_sha):
                raise CutoverError('Apply returned without matching ready cycle/hash; do NOT start')
            report('APPLIED: active_pending=%s protected=%s' %
                   (result.get('active_pending', '-'), result.get('protected_hosts', '-')))
            for service in SERVICES:
                systemctl('start', service)
            pid = self.health()
            try:
                archive = self.archive_staged()
                report('Staging archived: ' + str(archive))
            except (CutoverError, OSError) as exc:
                report('WARNING: snapshot is active, but staging could not be archived: ' + str(exc))
            report('SUCCESS: cycle=%s, crawler PID=%s' % (self.cycle, pid))
            report('Backup retained at ' + str(self.backup_dir))
        except BaseException as exc:
            if stopped:
                try:
                    if self.restored_state():
                        self.restore_original_services()
                    else:
                        current = state_readonly(self.db)
                        if not current['ready']:
                            report('FAIL CLOSED: snapshot publication incomplete; leave services stopped. '
                                   'Rerun with the SAME urls.json.next to resume.')
                        else:
                            report('Snapshot is already READY; no automatic rollback. '
                                   'Inspect service health and the active cycle before retrying.')
                except Exception as restore_error:
                    report('RESTORE FAILED: ' + str(restore_error))
            raise


def parse_args(argv=None):
    app = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description='Safely roll over BIMA PDNS urls.json.next in ONE command')
    p.add_argument('--app', type=Path, default=app)
    p.add_argument('--source', type=Path, help='Default: <app>/urls.json.next')
    p.add_argument('--input', type=Path, help='Default: <app>/urls.json')
    p.add_argument('--db', type=Path, help='Default: <app>/multi/state/crawler_production.sqlite3')
    p.add_argument('--learning-db', type=Path, help='Optional custom training SQLite backup path')
    p.add_argument('--owner', default='fauzi')
    p.add_argument('--backup-root', type=Path, default=Path('/home/fauzi/bima-backups'))
    p.add_argument('--cycle', help='Optional fixed ID; auto-generated by default')
    p.add_argument('--min-urls', type=int, default=100000)
    p.add_argument('--allow-major-shrink', action='store_true')
    p.add_argument('--repeat-source', action='store_true',
                   help='Explicitly recrawl identical bytes as prior cycle')
    p.add_argument('--skip-unit-check', action='store_true',
                   help='Only after manually confirming crawler --input and --db')
    p.add_argument('--health-timeout', type=int, default=180)
    args = p.parse_args(argv)
    if args.min_urls < 1 or not 10 <= args.health_timeout <= 900:
        p.error('min-urls must be positive and health-timeout must be 10..900 seconds')
    return args


def main(argv=None):
    if os.geteuid() != 0:
        print('ERROR: run with sudo (systemd stop/start + locked backup)', file=sys.stderr)
        return 1
    args = parse_args(argv)
    try:
        with open(LOCK_PATH, 'a+', encoding='utf-8') as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CutoverError('Another BIMA Registry rollover is running')
            Rollover(args).execute()
        return 0
    except (CutoverError, OSError, sqlite3.Error, subprocess.TimeoutExpired,
            ValueError, KeyError, json.JSONDecodeError) as exc:
        report('ERROR: ' + str(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
