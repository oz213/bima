#!/usr/bin/env python3
"""Enrich an existing BIMA v2.2 review pack with bounded live HTTP evidence."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
from learning import dataset_enrichment_v23 as enricher


def main(argv=None):
    parser = argparse.ArgumentParser(description='BIMA bounded HTTP evidence enrichment v2.3')
    parser.add_argument('--pack', type=Path, required=True, help='Existing v2.2 dataset directory')
    parser.add_argument('--production-db', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='New private output directory')
    parser.add_argument('--limit', type=int, default=60, help='Maximum host fetches (1..250)')
    parser.add_argument('--offset', type=int, default=0,
                        help='Skip previously collected candidates when batching')
    parser.add_argument('--fetch', action='store_true', help='Explicitly authorize HTTP-only refetch')
    args = parser.parse_args(argv)
    try:
        if not args.fetch:
            cases, _ = enricher._read_candidates(args.pack)
            chosen = enricher.choose_cases(cases, args.limit, args.offset)
            summary = {'mode': 'preview_only', 'candidate_count': len(cases),
                       'eligible_to_refetch': len(chosen),
                       'requires_explicit_fetch': True}
        else:
            summary = enricher.enrich(args.pack, args.production_db, args.out,
                                     limit=args.limit, offset=args.offset)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print('ERROR:', type(exc).__name__, str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
