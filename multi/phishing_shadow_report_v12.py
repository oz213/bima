#!/usr/bin/env python3
"""Read-only, bounded BIMA phishing shadow coverage report (jobs or v11 history).

Emits aggregate counters plus optional job IDs/coded signals for analyst review.
Never prints URL, model text, HTML, evidence paths, or secrets. It does not
crawl, enqueue, infer, edit SQLite, or send alerts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys

MAX_ROWS = 2000
MAX_CLASSIFIER_BYTES = 65536
SAFE_CODE = re.compile(r'^[a-z][a-z0-9_]{0,47}$')


class ReportError(ValueError):
    pass


def _triage(payload):
    if not isinstance(payload, dict):
        return None
    candidates = []
    for item in (payload, payload.get('http_classification'),
                 payload.get('browser_classification')):
        if isinstance(item, dict) and isinstance(item.get('phishing_triage'), dict):
            triage = item['phishing_triage']
            if str(triage.get('version', '')).startswith('phishing_shadow_v'):
                candidates.append(triage)
    if not candidates:
        return None
    # A Selenium classification may contain no HTML, while its nested HTTP
    # classification still contains the earlier actual HTML observation.
    rank = {'review_recommended': 5, 'login_form_only': 4, 'otp_form_only': 4,
            'insufficient_signal': 2, 'no_html': 1, 'parse_error': 0,
            'scanner_error': 0, 'out_of_scope': 0}
    return max(candidates, key=lambda item: (
        rank.get(item.get('status'), 0), item.get('version') == 'phishing_shadow_v12'))


def report(db, *, source='jobs', limit=500, review_limit=20):
    if source not in ('jobs', 'history'):
        raise ReportError('Invalid source')
    if not 1 <= limit <= MAX_ROWS or not 0 <= review_limit <= 50:
        raise ReportError('Invalid report limits')
    path = Path(db)
    if not path.is_file():
        raise ReportError('Existing SQLite database not found')
    uri = path.resolve(strict=True).as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA query_only=ON')
            table = ('jobs' if source == 'jobs' else 'discovery_history_outcomes_v11')
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone():
                raise ReportError('Requested source table is not installed')
            # ORDER BY primary key LIMIT: avoid a full-table scan on active NFS.
            pk = 'id'
            rows = conn.execute(
                f'SELECT {"id" if source == "jobs" else "job_id"}, status, '
                f'substr(classifier_json, 1, ?) FROM {table} '
                f'ORDER BY {pk} DESC LIMIT ?', (MAX_CLASSIFIER_BYTES + 1, limit)
            ).fetchall()
    except sqlite3.Error as exc:
        raise ReportError('Read-only SQLite query failed: ' + type(exc).__name__) from None

    result = {
        'ok': True, 'version': 'phishing_shadow_report_v12',
        'source': source, 'read_only': True,
        'sampling': 'latest_job_ids' if source == 'jobs' else 'latest_history_events',
        'rows_examined': len(rows), 'row_limit': limit,
        'with_shadow': 0, 'without_shadow': 0,
        'malformed_classifier': 0, 'oversized_classifier': 0,
        'v12_records': 0, 'v1_records': 0,
        'triage_statuses': {}, 'signal_codes': {},
        'review_job_ids': [], 'alerts_sent': 0,
        'not_a_phishing_verdict': True,
    }
    statuses = Counter()
    signals = Counter()
    seen_ids = set()
    for job_id, _job_status, raw in rows:
        if raw is None:
            result['without_shadow'] += 1
            continue
        if len(raw.encode('utf-8', errors='replace')) > MAX_CLASSIFIER_BYTES:
            result['oversized_classifier'] += 1
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            result['malformed_classifier'] += 1
            continue
        triage = _triage(doc)
        if triage is None:
            result['without_shadow'] += 1
            continue
        result['with_shadow'] += 1
        version = triage.get('version')
        result['v12_records' if version == 'phishing_shadow_v12' else 'v1_records'] += 1
        status = triage.get('status')
        if not isinstance(status, str) or not SAFE_CODE.fullmatch(status):
            status = 'invalid_status'
        statuses[status] += 1
        codes = triage.get('signal_codes', [])
        safe = sorted({x for x in codes[:64] if isinstance(x, str) and SAFE_CODE.fullmatch(x)}) \
            if isinstance(codes, list) else []
        signals.update(safe)
        if (status == 'review_recommended' and len(result['review_job_ids']) < review_limit
                and isinstance(job_id, int) and job_id > 0 and job_id not in seen_ids):
            result['review_job_ids'].append({'job_id': job_id, 'signal_codes': safe[:12]})
            seen_ids.add(job_id)
    result['triage_statuses'] = dict(sorted(statuses.items()))
    result['signal_codes'] = dict(sorted(signals.items()))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='multi/state/crawler_production.sqlite3')
    parser.add_argument('--source', choices=('jobs', 'history'), default='jobs')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--review-limit', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        data = report(args.db, source=args.source, limit=args.limit,
                      review_limit=args.review_limit)
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        return 0
    except (ReportError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, ReportError)
                          else type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
