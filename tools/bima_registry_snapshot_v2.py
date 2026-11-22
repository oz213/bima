#!/usr/bin/env python3
"""Safely preview/apply a new Registry PDNS snapshot. Do not run live services.

Both crawler and Telegram monitor must be stopped for --apply. On success,
only registry-cycle work is reset; old alert / review state is protected.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
from registry_snapshot_v2 import active_cycle, active_ready, apply_snapshot, mark_ready, read_snapshot

SERVICES = ('pandi-crawler-multi.service', 'pandi-telegram-monitor.service')


def require_stopped():
    if not shutil.which('systemctl'):
        raise RuntimeError('Apply must run on Linux with systemctl, not a Mac fixture')
    for service in SERVICES:
        result = subprocess.run(['systemctl', 'is-active', service],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.stdout.strip() in ('active', 'activating', 'deactivating'):
            raise RuntimeError('%s is not stopped; do not apply while services run' % service)
        if result.returncode not in (0, 3):
            raise RuntimeError('Cannot verify %s is stopped' % service)


def atomic_copy(source, target, expected_sha256):
    import hashlib
    if source.resolve() == target.resolve():
        raise ValueError('Source staging file must differ from active urls.json')
    if target.is_symlink() or source.is_symlink():
        raise ValueError('Symlink source/target is forbidden')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix='.bima-urls-v2-', suffix='.json',
                                    dir=target.parent)
        temp = Path(name)
        with os.fdopen(fd, 'wb') as output, source.open('rb') as inp:
            shutil.copyfileobj(inp, output)
            output.flush()
            os.fsync(output.fileno())
        actual = hashlib.sha256(temp.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise ValueError('Source changed during apply; snapshot not published')
        os.chmod(temp, 0o640)
        os.replace(temp, target)
        # Ensure directory entry has reached the filesystem before returning.
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description='BIMA Registry snapshot lifecycle v2')
    parser.add_argument('mode', choices=('preview', 'apply', 'status'))
    parser.add_argument('--source', type=Path, help='Staged PDNS urls JSON file')
    parser.add_argument('--input', type=Path,
                        help='Actual --input path used by crawler systemd service')
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--cycle', help='Unique operator cycle ID, e.g. 20261122T113000Z')
    parser.add_argument('--min-urls', type=int, default=100000,
                        help='Fail closed if the new PDNS list is too small (default 100000)')
    parser.add_argument('--allow-major-shrink', action='store_true',
                        help='Explicitly acknowledge >50%% shrink compared with prior cycle')
    args = parser.parse_args(argv)
    try:
        db_path = args.db.resolve(strict=True)
        if args.mode == 'status':
            with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as conn:
                conn.execute('PRAGMA query_only=ON')
                current = active_cycle(conn)
                status = {'active_cycle': current, 'input_ready': active_ready(conn)}
                if current:
                    summary = conn.execute(
                        'SELECT COUNT(*),SUM(pending),SUM(blocked) FROM registry_snapshot_queue_v2'
                    ).fetchone()
                    status['snapshot_urls'] = summary[0]
                    status['pending'] = summary[1] or 0
                    status['protected_hosts'] = summary[2] or 0
                print(json.dumps(status, indent=2))
                return 0
        if not args.source or not args.cycle or not args.input:
            raise ValueError('--source, --input and --cycle are required for preview/apply')
        snapshot = read_snapshot(args.source)
        if args.min_urls < 1 or len(snapshot['urls']) < args.min_urls:
            raise ValueError('Only %s URLs; expected at least %s. Check PDNS extraction' % (
                len(snapshot['urls']), args.min_urls))
        result = {'cycle_id': args.cycle, 'source_sha256': snapshot['sha256'],
                  'unique_urls': len(snapshot['urls']), 'invalid': snapshot['invalid']}
        if args.mode == 'preview':
            with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as conn:
                conn.execute('PRAGMA query_only=ON')
                result['currently_active_cycle'] = active_cycle(conn)
                result['source_will_not_be_applied'] = True
            print(json.dumps(result, indent=2))
            return 0
        require_stopped()
        if shutil.disk_usage(db_path.parent).free < 1024 * 1024 * 1024:
            raise RuntimeError('Less than 1 GiB free near database; cannot safely apply snapshot')
        if not args.input.parent.resolve().samefile(args.source.parent.resolve()):
            # Staging files in the same project directory avoid unexpected mounts.
            raise ValueError('Stage the snapshot beside the active urls.json')
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            applied = apply_snapshot(conn, cycle_id=args.cycle, snapshot=snapshot,
                                     allow_major_shrink=args.allow_major_shrink)
        # The DB can be committed even if replacing input fails. Re-run exactly
        # the same --apply with the same staged source to complete publication.
        atomic_copy(args.source, args.input, snapshot['sha256'])
        # Defensive two-phase publish. A failed file swap leaves the active
        # Registry queue disabled, even if a service is accidentally restarted.
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            mark_ready(conn, args.cycle, snapshot['sha256'])
        result.update(applied)
        result['input_ready'] = True
        result['input_published'] = str(args.input)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
