#!/usr/bin/env python3
"""BIMA opt-in shadow summary. Reads metadata-only JSONL, never changes anything."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def summarize(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Audit JSONL missing or symlink')
    unique = set()
    counts = collections.Counter()
    models = set()
    with path.open(encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            event_id = row.get('event_id')
            if not event_id or event_id in unique:
                continue
            unique.add(event_id)
            candidate = row.get('candidate') or {}
            counts['total'] += 1
            counts['candidate_positive'] += bool(candidate.get('hit'))
            counts['model_disagreement'] += row.get('model_disagreement') is True
            counts['masked_strong_chunk'] += bool(candidate.get('masked_strong_chunk'))
            counts['input_capped'] += bool(row.get('input_capped'))
            counts['token_truncated'] += bool(candidate.get('truncated_token_chunks', 0))
            if row.get('candidate_checkpoint_sha256'):
                models.add(row['candidate_checkpoint_sha256'])
    return {'observations': dict(counts),
            'distinct_candidate_checkpoints': len(models),
            'accuracy_validated': False,
            'model_production_changed': False,
            'note': 'Disagreements and positives are not human labels.'}


def main(argv=None):
    p = argparse.ArgumentParser(description='Show metadata-only BIMA shadow rollup')
    p.add_argument('--audit', required=True, help='Path to shadow_comparisons_v1.jsonl')
    args = p.parse_args(argv)
    print(json.dumps(summarize(args.audit), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
