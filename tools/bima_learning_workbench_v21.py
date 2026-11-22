#!/usr/bin/env python3
"""BIMA Continuous Learning v2.1 read-only operator workbench."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
from learning import review_workbench_v21 as workbench


def output_report(report, output_path):
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if output_path is None:
        print(payload, end='')
        return
    destination = Path(output_path).expanduser()
    if not destination.parent.is_dir() or destination.is_symlink():
        raise ValueError('Report output must be a new regular file in an existing directory')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(destination), flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print('Private metadata report created: ' + str(destination))


def main(argv=None):
    parser = argparse.ArgumentParser(description='BIMA learning v2.1: metadata-only review/audit')
    parser.add_argument('--learning-db', default='multi/state/bima_learning_v450.sqlite3')
    parser.add_argument('--output', type=Path, help='New file, mode 0600, never overwrite')
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('status', help='Dataset health, class balance, training gates (read-only)')
    queue = modes.add_parser('queue', help='Next human-review actions (no evidence text)')
    queue.add_argument('--limit', type=int, default=25)
    queue.add_argument('--focus', choices=workbench.VALID_FOCUS, default='all')
    sample = modes.add_parser('sample-production', help='Read-only candidate negatives/uncertain sites')
    sample.add_argument('--production-db', required=True)
    sample.add_argument('--per-status', type=int, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            report = workbench.status(args.learning_db)
        elif args.command == 'queue':
            report = workbench.queue(args.learning_db, limit=args.limit, focus=args.focus)
        else:
            report = workbench.sample_production(args.production_db, per_status=args.per_status)
        output_report(report, args.output)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
