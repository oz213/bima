#!/usr/bin/env python3
"""Collect a private BIMA review/training pack; read-only to operational DBs.

Run under fauzi on the BIMA host. Does not crawl sites, train, or modify model.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
from learning import dataset_collection_v22 as collector


def main(argv=None):
    p = argparse.ArgumentParser(description='BIMA dataset acquisition v2.2')
    p.add_argument('--production-db', type=Path, required=True)
    p.add_argument('--learning-db', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--seed', type=int, default=20261122)
    p.add_argument('--alert', type=int, default=50)
    p.add_argument('--complete', type=int, default=100)
    p.add_argument('--uncertain', type=int, default=50)
    public = p.add_mutually_exclusive_group()
    public.add_argument('--download-public', action='store_true',
                        help='Download pinned public annotated CSV (no images)')
    public.add_argument('--public-csv', type=Path,
                        help='Use the same public CSV previously downloaded locally')
    args = p.parse_args(argv)
    try:
        prod = args.production_db.resolve(strict=True)
        learn = args.learning_db.resolve(strict=False)
        if prod == learn or (learn.exists() and os.path.samefile(prod, learn)):
            raise ValueError('Production and learning DB paths must differ')
        out = args.out.expanduser().absolute()
        if out.is_symlink() or out.exists():
            raise ValueError('Output directory must not exist before collection')
        if out == prod or out == learn or prod in out.parents or learn in out.parents:
            raise ValueError('Output must be separate from source databases')
        if out == ROOT or ROOT in out.parents:
            raise ValueError('Dataset pack must be outside the Git repository')
        public_data = None
        if args.download_public:
            public_data = collector.public_csv_from_web()
        elif args.public_csv:
            path = args.public_csv.expanduser()
            if path.is_symlink() or not path.is_file():
                raise ValueError('Public CSV must be a regular non-symlink local file')
            if path.stat().st_size > collector.MAX_PUBLIC_BYTES:
                raise ValueError('Public CSV too large')
            public_data = path.read_bytes()
        external = []
        public_stats = {}
        if public_data is not None:
            external, public_stats = collector.collect_external_csv(public_data)
            if not external:
                raise ValueError('No valid external text samples; inspect CSV schema')
        internal, internal_stats = collector.collect_registry(
            prod, seed=args.seed, alert=args.alert, complete=args.complete,
            uncertain=args.uncertain)
        verified, verified_stats = collector.collect_verified(learn)
        if out.parent.is_symlink() or not out.parent.is_dir():
            raise ValueError('Output parent must be a real existing directory')
        result = collector.publish_pack(
            out, candidates=internal, verified=verified, external=external,
            summary={**internal_stats, **verified_stats, **public_stats})
        print(json.dumps({
            'status': 'collected', 'output_dir': str(out),
            'counts': result['counts'], 'summary': result['summary'],
            'note': 'Independent human verification required for candidate labels; no production changes',
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
