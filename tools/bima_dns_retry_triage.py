#!/usr/bin/env python3
"""BIMA DNS retry snapshot. SQLite mode=ro; does not modify queue or trigger DNS.

Usage:
  python3 -B tools/bima_dns_retry_triage.py --db multi/state/crawler_production.sqlite3
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def snapshot(db_path: str, *, now_utc: str | None = None) -> dict:
    path = Path(db_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('DB path must be a regular file')
    now = now_utc or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=12)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA busy_timeout=12000')
        db.execute('BEGIN')  # One consistent read-only snapshot, even while crawler writes.
        status_rows = db.execute(
            "SELECT status, COUNT(*) FROM jobs "
            "WHERE status IN ('pending','retry_http','browser_queued','http_inflight') "
            "GROUP BY status ORDER BY status"
        ).fetchall()
        retry_rows = db.execute(
            "SELECT COALESCE(error_category,'null'), COALESCE(dns_result,'null'), "
            "COUNT(*), ROUND(AVG(attempts), 2), "
            "SUM(CASE WHEN next_retry_at IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN next_retry_at<=? THEN 1 ELSE 0 END), "
            "MIN(next_retry_at), MAX(next_retry_at) "
            "FROM jobs WHERE status='retry_http' "
            "GROUP BY error_category,dns_result ORDER BY COUNT(*) DESC",
            (now,),
        ).fetchall()
        db.rollback()  # End read transaction; no data was changed.
    return {
        'now': now,
        'statuses': {name: count for name, count in status_rows},
        'retries': [dict(category=row[0], dns=row[1], count=row[2],
                         avg_attempts=row[3], no_deadline=row[4], due_now=row[5],
                         first_due=row[6], last_due=row[7]) for row in retry_rows],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only BIMA retry and DNS diagnostics')
    parser.add_argument('--db', required=True, help='Production SQLite path (read-only)')
    args = parser.parse_args()
    report = snapshot(args.db)
    print('=== BIMA DNS/HTTP RETRY SNAPSHOT (READ ONLY) ===')
    print('UTC now:', report['now'])
    for name, value in report['statuses'].items():
        print(f'{name:22} {value:>12,}')
    print('\n=== RETRY BY TRANSPORT CATEGORY AND DNS RESULT ===')
    for item in report['retries']:
        print(f"{item['category']:17} {item['dns']:20} "
              f"total={item['count']:,} due={item['due_now']:,} "
              f"no_deadline={item['no_deadline']:,} avg_attempts={item['avg_attempts']}")
        print(f"    deadline range UTC: {item['first_due'] or '-'} .. {item['last_due'] or '-'}")
    print('\nCatatan: dns=none dengan category=timeout bukan DNS NXDOMAIN.')
    print('Tidak ada perubahan DB, retry, permintaan HTTP/DNS, atau pengiriman alert.')


if __name__ == '__main__':
    main()
