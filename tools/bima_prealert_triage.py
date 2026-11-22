#!/usr/bin/env python3
"""Read-only BIMA Pre-Alert triage; never sends BIMA API or modifies SQLite.

Examples:
  python3 tools/bima_prealert_triage.py --db multi/state/crawler_production.sqlite3
  python3 tools/bima_prealert_triage.py --db multi/state/crawler_production.sqlite3 --case 3
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


OPEN = ('send_unknown', 'sending', 'needs_review', 'prealert_error')


def inspect(db_file, case_id=None):
    source = Path(db_file).resolve()
    if not source.is_file():
        raise FileNotFoundError('Crawler DB does not exist')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        audited = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='bima_prealert_dispatch_audit'").fetchone())
        audit_cols = ('a.http_status,a.elapsed_ms,a.result,a.error_kind'
                      if audited else 'NULL,NULL,NULL,NULL')
        where = ('WHERE p.id=?' if case_id is not None else
                 "WHERE p.status IN ('send_unknown','sending','needs_review')")
        params = (case_id,) if case_id is not None else ()
        sql = (
            'SELECT p.id,p.job_id,p.url,p.bucket,p.status,p.feedback_label,'
            'p.bima_attempted_at,p.bima_result_at,j.status,' + audit_cols +
            ' FROM bima_prealerts p LEFT JOIN jobs j ON j.id=p.job_id ' +
            ('LEFT JOIN bima_prealert_dispatch_audit a ON a.prealert_id=p.id '
             if audited else '') +
            where + ' ORDER BY p.id DESC LIMIT 50')
        records = conn.execute(sql, params).fetchall()
        result = []
        for (cid, job_id, url, bucket, status, label, attempted, completed,
             job_status, http_status, elapsed_ms, audit_result, error_kind) in records:
            result.append({
                'case': cid, 'job': job_id,
                'domain': urlsplit(url).hostname or '-',
                'bucket': bucket, 'status': status,
                'label': label or '-', 'job_status': job_status,
                'attempted_utc': attempted or '-', 'completed_utc': completed or '-',
                'http_status': http_status if http_status is not None else '-',
                'elapsed_ms': elapsed_ms if elapsed_ms is not None else '-',
                'audit': audit_result or 'not_recorded',
                'error_kind': error_kind or '-',
            })
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect Pre-Alert without retry or DB write')
    parser.add_argument('--db', required=True, help='Crawler SQLite database path')
    parser.add_argument('--case', type=int, default=None, help='Pre-Alert case ID (not job ID)')
    args = parser.parse_args(argv)
    if args.case is not None and args.case < 1:
        parser.error('--case must be a positive integer')
    for row in inspect(args.db, args.case):
        print(' | '.join(f'{name}={value}' for name, value in row.items()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
