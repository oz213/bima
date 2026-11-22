#!/usr/bin/env python3
"""Operator-only BIMA Continuous Learning v2. Offline candidate, no live model swap."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
from learning import continuous_v2 as v2


def evidence(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 480000:
        raise ValueError('Evidence must be a regular UTF-8 file <=480KB (no symlinks)')
    return path.read_text(encoding='utf-8')


def main(argv=None):
    p = argparse.ArgumentParser(description='BIMA Continuous Learning v2 (safe/offline)')
    p.add_argument('--learning-db', type=Path,
                   default=Path('multi/state/bima_learning_v450.sqlite3'))
    sub = p.add_subparsers(dest='cmd', required=True)
    sync = sub.add_parser('sync-prealerts', help='Import human SITE verdicts only')
    sync.add_argument('--production-db', type=Path, required=True)
    view = sub.add_parser('stats')
    view.add_argument('--limit', type=int, default=20)
    attach = sub.add_parser('attach-text', help='Attach analyst-reviewed text evidence')
    attach.add_argument('--uid', required=True)
    attach.add_argument('--file', type=Path, required=True)
    attach.add_argument('--reviewer', required=True)
    label = sub.add_parser('label-text', help='Explicitly label attached evidence text')
    label.add_argument('--uid', required=True)
    label.add_argument('--label', choices=('gambling', 'non_gambling', 'not_for_training'),
                       required=True)
    label.add_argument('--reviewer', required=True)
    label.add_argument('--note', required=True)
    manual = sub.add_parser('add-reviewed', help='New full human review from current cycle')
    manual.add_argument('--production-db', type=Path, required=True)
    manual.add_argument('--cycle', required=True)
    manual.add_argument('--job', type=int, required=True)
    manual.add_argument('--site', choices=('true_positive', 'false_positive'), required=True)
    manual.add_argument('--text-label', choices=('gambling','non_gambling','not_for_training'),
                        required=True)
    manual.add_argument('--file', type=Path, required=True)
    manual.add_argument('--reviewer', required=True)
    manual.add_argument('--note', required=True)
    train = sub.add_parser('learn', help='Train/evaluate a candidate without production activation')
    train.add_argument('--runs', type=Path, default=Path('multi/state/learning-runs-v2'))
    try:
        args = p.parse_args(argv)
        if args.cmd == 'sync-prealerts':
            result = v2.sync_prealerts(args.production_db, args.learning_db)
        elif args.cmd == 'stats':
            if args.limit < 0 or args.limit > 100:
                raise ValueError('--limit must be 0..100')
            result = v2.stats(args.learning_db, limit=args.limit)
        elif args.cmd == 'attach-text':
            result = v2.attach_text(args.learning_db, args.uid, evidence(args.file),
                                    args.reviewer)
        elif args.cmd == 'label-text':
            result = v2.label_text(args.learning_db, args.uid, args.label, args.reviewer,
                                   args.note)
        elif args.cmd == 'add-reviewed':
            result = v2.add_manual(args.production_db, args.learning_db, args.cycle,
                                   args.job, args.site, evidence(args.file), args.text_label,
                                   args.reviewer, args.note)
        else:
            result = v2.learn(args.learning_db, args.runs)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
