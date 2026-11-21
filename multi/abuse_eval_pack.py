#!/usr/bin/env python3
"""Read-only, stratified review sampling and human-label evaluation for BIMA.

The current decision being evaluated is BIMA's legacy gambling alert, NOT a
multi-abuse classifier. Do not mistake unreviewed samples for ground truth.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import sqlite3
import sys

SCHEMA_VERSION = 1
GROUPS = {
    'alerted': 'alert', 'pending_alert': 'alert', 'dry_run_alert': 'alert',
    'complete': 'complete',
    'inconclusive': 'uncertain', 'retry_http': 'uncertain', 'failed': 'uncertain',
}
GROUP_NAMES = ('alert', 'complete', 'uncertain')
MAX_PER_GROUP = 1000


def open_database_ro(filename):
    path = Path(filename).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Database tidak ditemukan: {path}')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.execute('PRAGMA query_only=ON')
    columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
    if not {'id', 'status', 'risk', 'updated_at'}.issubset(columns):
        db.close()
        raise ValueError('Tabel jobs BIMA tidak sesuai dengan format yang diharapkan')
    return db


def predicted_positive(status, risk):
    if status in ('alerted', 'pending_alert', 'dry_run_alert'):
        return True
    if status == 'complete':
        if risk in ('medium_risk', 'high_risk'):
            return True
        if risk == 'low_risk':
            return False
        return None
    return None


def sample_database(conn, *, seed=20261121, alert=50, complete=100, uncertain=50):
    quotas = {'alert': alert, 'complete': complete, 'uncertain': uncertain}
    if any(n < 0 or n > MAX_PER_GROUP for n in quotas.values()):
        raise ValueError('Kuota per kelompok harus 0 sampai 1000')
    counts = Counter({name: 0 for name in GROUP_NAMES})
    chosen = {name: [] for name in GROUP_NAMES}
    rng = {name: random.Random(f'{seed}:{name}') for name in GROUP_NAMES}
    statuses = tuple(GROUPS)
    placeholders = ','.join('?' for _ in statuses)
    # Read-only transaction provides a consistent snapshot while crawler writes WAL.
    conn.execute('BEGIN')
    try:
        for job_id, status, risk, updated_at in conn.execute(
            'SELECT id,status,risk,updated_at FROM jobs WHERE status IN ('
            + placeholders + ') ORDER BY status,id', statuses
        ):
            group = GROUPS[status]
            counts[group] += 1
            row = {'job_id': job_id, 'stratum': group, 'job_status': status,
                   'risk': risk, 'status_updated_at': updated_at,
                   'predicted_gambling': predicted_positive(status, risk)}
            quota = quotas[group]
            if quota == 0:
                continue
            if len(chosen[group]) < quota:
                chosen[group].append(row)
            else:
                index = rng[group].randrange(counts[group])
                if index < quota:
                    chosen[group][index] = row
    finally:
        conn.rollback()  # End the read transaction; never commits to source DB.
    items = []
    for group in GROUP_NAMES:
        probability = len(chosen[group]) / counts[group] if counts[group] else 0
        for row in sorted(chosen[group], key=lambda r: r['job_id']):
            row['selection_probability'] = probability
            items.append(row)
    return {
        'schema_version': SCHEMA_VERSION,
        'classification_scope': 'legacy_illegal_gambling',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'seed': seed, 'eligible_counts': dict(counts), 'sample_counts': {
            name: len(chosen[name]) for name in GROUP_NAMES},
        'warnings': [
            'Sampel ini belum berlabel dan tidak mengukur akurasi produksi.',
            'Inconclusive/retry adalah UNKNOWN, bukan negative.',
            'status_updated_at bukan waktu pasti konten diambil.',
        ],
        'samples': items,
    }


def save_new_private_json(path, data):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError(f'Folder tujuan belum ada: {path.parent}')
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')


def evaluate_labeled(manifest, labels):
    if manifest.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Versi manifest tidak sesuai')
    expected = {row['job_id']: row for row in manifest['samples']}
    if len(expected) != len(manifest['samples']):
        raise ValueError('Manifest memiliki job_id duplikat')
    matrix = Counter()
    not_scorable = 0
    for job_id, label in labels.items():
        if job_id not in expected:
            raise ValueError(f'Label job tidak ada dalam sampel: {job_id}')
        if label not in ('gambling', 'non_gambling', 'uncertain'):
            raise ValueError(f'Label tidak dikenal pada job {job_id}')
        pred = expected[job_id]['predicted_gambling']
        if label == 'uncertain' or pred is None:
            not_scorable += 1
            continue
        truth = label == 'gambling'
        matrix['tp' if pred and truth else 'fp' if pred else
               'fn' if truth else 'tn'] += 1
    unreviewed = len(expected) - len(labels)
    tp, fp, fn, tn = (matrix[k] for k in ('tp', 'fp', 'fn', 'tn'))
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {'reviewed': len(labels), 'unreviewed': unreviewed,
            'not_scorable': not_scorable,
            'confusion_matrix_on_scorable_reviews_only': dict(tp=tp,fp=fp,fn=fn,tn=tn),
            'precision_reviewed_only': p, 'recall_reviewed_only': r,
            'warning': ('Metrik hanya pada sampel yang sudah direview; bukan estimasi '
                        'kualitas seluruh .id tanpa sampling dan penanganan label yang lengkap.')}


def read_labels(path):
    labels = {}
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {'job_id','label'}.issubset(reader.fieldnames):
            raise ValueError('CSV perlu kolom job_id,label')
        for row in reader:
            jid = int(row['job_id'])
            if jid in labels:
                raise ValueError(f'Label duplikat: {jid}')
            labels[jid] = row['label'].strip().lower()
    return labels


def main(argv=None):
    parser = argparse.ArgumentParser(description='BIMA read-only abuse evaluation pack')
    sub = parser.add_subparsers(dest='command', required=True)
    take = sub.add_parser('sample', help='Pilih kasus dari database BIMA, tanpa perubahan DB')
    take.add_argument('--db', required=True)
    take.add_argument('--out', required=True)
    take.add_argument('--seed', type=int, default=20261121)
    for name, default in [('alert',50), ('complete',100), ('uncertain',50)]:
        take.add_argument('--'+name, type=int, default=default)
    eval_cmd = sub.add_parser('evaluate', help='Hitung metrik terbatas dari label manusia')
    eval_cmd.add_argument('--manifest', required=True)
    eval_cmd.add_argument('--labels', required=True)
    args = parser.parse_args(argv)
    if args.command == 'sample':
        from contextlib import closing
        with closing(open_database_ro(args.db)) as conn:
            manifest = sample_database(conn, seed=args.seed,
                                       alert=args.alert,complete=args.complete,
                                       uncertain=args.uncertain)
        save_new_private_json(args.out, manifest)
        print(json.dumps({'saved': args.out, 'eligible_counts': manifest['eligible_counts'],
                          'sample_counts': manifest['sample_counts']}, ensure_ascii=False))
    else:
        with open(args.manifest, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        print(json.dumps(evaluate_labeled(manifest, read_labels(args.labels)),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, sqlite3.Error) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        raise SystemExit(2)
