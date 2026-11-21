#!/usr/bin/env python3
"""BIMA v4.3.6: safely stage Google/urlscan JSONL candidates into existing jobs.

Default is a SQLite read-only preview. No provider requests, browser, Telegram,
BIMA alerts, IndoBERT initialization, schema migrations, or service restarts.
Writes require BOTH --apply and --acknowledge-queue-write. Brave search result
URLs must NEVER be imported from historical multisource logs.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import stat
import sys
from urllib.parse import urlsplit

from google_discovery import load_scopes, normalize_candidate

MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 100000
MAX_RECORDS = 100000
ALLOWED_SOURCES = frozenset({'google', 'urlscan'})
ALLOWED_EVIDENCE = frozenset({'search_index_signal', 'historical_scan_metadata'})
SUPPORTED_VERSIONS = frozenset({'4.3.4', '4.3.5'})


class QueueImportError(ValueError):
    """Curated messages without source URLs or log content."""


def _read_log(path, scopes):
    """Read bounded JSONL. Reject Brave-derived candidates before touching URL."""
    filename = Path(path)
    try:
        st = filename.lstat()
    except OSError:
        raise QueueImportError('Discovery JSONL does not exist or is unreadable') from None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_LOG_BYTES:
        raise QueueImportError('Discovery JSONL must be a regular file <=64 MiB')
    counters = {
        'log_records': 0, 'preview_records': 0, 'raw_candidate_entries': 0,
        'excluded_brave_entries': 0, 'rejected_entries': 0,
        'duplicates_in_log': 0, 'truncated_preview_runs': 0,
        'skipped_non_preview_records': 0,
    }
    unique = OrderedDict()
    total_bytes = 0
    scope_set = set(scopes)
    try:
        with filename.open('rb') as stream:
            for line in stream:
                counters['log_records'] += 1
                total_bytes += len(line)
                if (counters['log_records'] > MAX_RECORDS or len(line) > MAX_LINE_BYTES
                        or total_bytes > MAX_LOG_BYTES):
                    raise QueueImportError('Discovery JSONL exceeds bounded line/record limits')
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeError, ValueError):
                    raise QueueImportError('Discovery JSONL contains invalid JSON; no writes made') from None
                if not isinstance(record, dict):
                    raise QueueImportError('Discovery JSONL record must be a JSON object')
                if record.get('version') not in SUPPORTED_VERSIONS or record.get('mode') != 'api_preview':
                    counters['skipped_non_preview_records'] += 1
                    continue
                items = record.get('preview_candidates', [])
                if not isinstance(items, list) or len(items) > 500:
                    raise QueueImportError('Discovery JSONL preview_candidates invalid')
                counters['preview_records'] += 1
                if record.get('preview_truncated'):
                    counters['truncated_preview_runs'] += 1
                for item in items:
                    counters['raw_candidate_entries'] += 1
                    if not isinstance(item, dict):
                        counters['rejected_entries'] += 1
                        continue
                    sources = item.get('sources')
                    if isinstance(sources, list) and 'brave' in sources:
                        counters['excluded_brave_entries'] += 1
                        continue
                    if (not isinstance(sources, list) or not sources or
                            not all(isinstance(s, str) and s in ALLOWED_SOURCES for s in sources) or
                            len(set(sources)) != len(sources) or
                            item.get('evidence_level') not in ALLOWED_EVIDENCE):
                        counters['rejected_entries'] += 1
                        continue
                    via = item.get('found_via_scopes')
                    raw = item.get('url')
                    claimed_scope = item.get('matched_scope')
                    if (not isinstance(via, list) or not via or len(via) > 14 or
                            not isinstance(raw, str) or not isinstance(claimed_scope, str)):
                        counters['rejected_entries'] += 1
                        continue
                    valid = False
                    for suffix in via:
                        if (not isinstance(suffix, str) or not suffix.startswith('.') or
                                suffix[1:] not in scope_set):
                            continue
                        match = normalize_candidate(raw, suffix[1:], scopes)
                        if match is not None and match == (raw, claimed_scope):
                            valid = True
                            break
                    if not valid:
                        counters['rejected_entries'] += 1
                        continue
                    if raw in unique:
                        counters['duplicates_in_log'] += 1
                        continue
                    if len(unique) >= MAX_CANDIDATES:
                        raise QueueImportError('Too many unique candidates; rotate/archive JSONL')
                    unique[raw] = None
    except OSError:
        raise QueueImportError('Discovery JSONL read failure; no writes made') from None
    return list(unique), counters


def _db_connection(path, apply):
    filename = Path(path)
    if not filename.is_file():
        raise QueueImportError('Existing BIMA production SQLite database not found')
    uri = filename.resolve().as_uri() + ('?mode=rw' if apply else '?mode=ro')
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=20)
        conn.execute('PRAGMA busy_timeout=20000')
        if not apply:
            conn.execute('PRAGMA query_only=ON')
        _validate_schema(conn)
        return conn
    except (sqlite3.DatabaseError, OSError):
        raise QueueImportError('Cannot open or verify BIMA jobs database') from None


def _validate_schema(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        raise QueueImportError('BIMA database is missing jobs table')
    columns = {row[1]: row for row in conn.execute('PRAGMA table_info("jobs")')}
    if not {'id', 'url', 'status', 'attempts', 'updated_at'}.issubset(columns):
        raise QueueImportError('BIMA jobs schema is incompatible')
    # The original BIMA importer relies on these defaults. Fail closed if
    # external migrations replaced them instead of silently inserting bad jobs.
    if (str(columns['status'][4]).strip('"\'').lower() != 'pending' or
            str(columns['attempts'][4]).strip('"\'') != '0'):
        raise QueueImportError('BIMA jobs pending/attempts defaults are incompatible')
    unique_url = False
    for index in conn.execute('PRAGMA index_list("jobs")'):
        if not index[2] or (len(index) > 4 and index[4]):
            continue
        name = index[1].replace('"', '""')
        parts = [r[2] for r in conn.execute('PRAGMA index_info("' + name + '")')]
        if parts == ['url']:
            unique_url = True
            break
    if not unique_url:
        raise QueueImportError('BIMA jobs.url UNIQUE index is missing')


def _queue_keys(url):
    """Root without slash is semantically the same seed as root with slash.

    Existing registry urls.json may have imported ``https://foo.id`` while
    proactive_discovery.normalize_url emits ``https://foo.id/``.
    """
    parts = urlsplit(url)
    if parts.path == '/' and not parts.query and not parts.fragment:
        return (url, url[:-1])
    return (url,)


def _job_exists(conn, url):
    keys = _queue_keys(url)
    if len(keys) == 1:
        return conn.execute('SELECT 1 FROM jobs WHERE url=? LIMIT 1', keys).fetchone() is not None
    return conn.execute('SELECT 1 FROM jobs WHERE url IN (?,?) LIMIT 1', keys).fetchone() is not None


def _pending(conn):
    return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0])


def audit(args):
    if args.apply and not args.acknowledge_queue_write:
        raise QueueImportError('--apply requires --acknowledge-queue-write')
    if args.max_pending < 1 or args.max_pending > 3000000 or not 1 <= args.max_new <= 100:
        raise QueueImportError('max-pending must be 1..3000000 and max-new 1..100')
    scopes = load_scopes(args.domains)
    candidates, counters = _read_log(args.input, scopes)
    if Path(args.db).resolve() == Path(args.input).resolve():
        raise QueueImportError('Input JSONL cannot be the SQLite database')
    result = {
        'ok': True, 'version': '4.3.6',
        'mode': 'apply' if args.apply else 'preview',
        'db_read_only': not args.apply, 'db_writes': 0,
        'new_jobs': 0, 'alerts_sent': 0, 'browser_launched': False,
        'providers_ingested': ['google', 'urlscan'],
        'brave_ingestion_enabled': False, 'discovery_only': not args.apply,
        'max_new': args.max_new, 'max_pending': args.max_pending,
        'unique_valid_candidates': len(candidates), **counters,
    }
    with closing(_db_connection(args.db, args.apply)) as conn:
        if not args.apply:
            pending = _pending(conn)
            missing = 0
            existing = 0
            for url in candidates:
                if _job_exists(conn, url):
                    existing += 1
                else:
                    missing += 1
            capacity = max(0, min(args.max_new, args.max_pending - pending))
            result.update({
                'pending_before': pending, 'pending_after': pending,
                'already_in_jobs': existing, 'not_in_jobs': missing,
                'would_insert': min(missing, capacity),
                'blocked_by_pending_limit': pending >= args.max_pending,
            })
            return result
        # The transaction serializes all writers, including a live crawler.
        # Never modify existing jobs' status, attempts, results or timestamps.
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            pending = _pending(conn)
            result['pending_before'] = pending
            inserted = 0
            existing = 0
            if pending < args.max_pending:
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                for url in candidates:
                    if inserted >= args.max_new or pending >= args.max_pending:
                        break
                    if _job_exists(conn, url):
                        existing += 1
                        continue
                    cursor = conn.execute(
                        'INSERT OR IGNORE INTO jobs(url,updated_at) VALUES (?,?)',
                        (url, timestamp),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        pending += 1
                    else:
                        existing += 1
            result.update({
                'new_jobs': inserted, 'db_writes': inserted,
                'pending_after': pending, 'already_in_jobs_scanned': existing,
                'blocked_by_pending_limit': pending >= args.max_pending,
            })
    return result


def make_parser():
    p = argparse.ArgumentParser(description='BIMA v4.3.6 JSONL-to-jobs integration, preview by default')
    p.add_argument('--input', default='/home/fauzi/bima-logs/multi-source.jsonl',
                   help='Existing multi_source_discovery.py JSONL log (not Google v4.3.3 log)')
    p.add_argument('--db', default='multi/state/crawler_production.sqlite3')
    p.add_argument('--domains', default='domains.json')
    p.add_argument('--max-new', type=int, default=20)
    p.add_argument('--max-pending', type=int, default=10000,
                   help='Backpressure guard; large Registry backlogs may prevent imports')
    p.add_argument('--apply', action='store_true', help='Insert a bounded number of new jobs')
    p.add_argument('--acknowledge-queue-write', action='store_true',
                   help='Confirm an intentional write to live crawler jobs')
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        result = audit(args)
    except (QueueImportError, ValueError, sqlite3.DatabaseError, OSError) as exc:
        msg = str(exc) if isinstance(exc, QueueImportError) else type(exc).__name__
        result = {'ok': False, 'mode': 'error', 'error': msg, 'db_writes': 0,
                  'new_jobs': 0, 'alerts_sent': 0}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
