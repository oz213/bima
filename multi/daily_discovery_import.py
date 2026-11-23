#!/usr/bin/env python3
"""BIMA: import every eligible Google/urlscan candidate from private FIFO spools.

This tool never contacts providers or starts the crawler. The operator-controlled
cron still performs at most one automatic import invocation per UTC day; 0 in
volume-limit flags means unlimited eligible candidates, not unlimited provider
API calls or unsafe/unbounded input-file sizes. Brave results stay excluded.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from discovery_queue import QueueImportError, audit

SPOOL_NAME = re.compile(r'^pending-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}\.jsonl$')
MAX_SPOOL_FILES = 365
MAX_SPOOL_BYTES = 64 * 1024 * 1024


def _list_spool(folder: Path):
    try:
        directory_stat = folder.lstat()
    except OSError:
        raise QueueImportError('Discovery spool directory does not exist') from None
    if (not stat.S_ISDIR(directory_stat.st_mode) or
            directory_stat.st_uid != os.geteuid() or
            directory_stat.st_mode & 0o077):
        raise QueueImportError('Discovery spool directory must be owned by the caller and private (0700)')
    try:
        files = sorted((p for p in folder.iterdir() if SPOOL_NAME.fullmatch(p.name)), key=lambda x: x.name)
    except OSError:
        raise QueueImportError('Discovery spool directory is unreadable') from None
    if len(files) > MAX_SPOOL_FILES:
        raise QueueImportError('Discovery spool exceeds 365 pending run files; manual review required')
    for path in files:
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or
                st.st_mode & 0o077 or st.st_size > MAX_SPOOL_BYTES or st.st_nlink != 1):
            raise QueueImportError('Discovery spool contains an unsafe or oversized pending file')
    return files


def _args(args, path, *, apply, new_limit):
    return argparse.Namespace(
        input=str(path), db=args.db, domains=args.domains,
        max_new=new_limit, max_priority=args.max_priority,
        max_pending=args.max_pending,
        allow_priority_over_pending_ceiling=args.allow_priority_over_pending_ceiling,
        apply=apply, acknowledge_queue_write=apply and args.acknowledge_queue_write,
    )


def drain(args):
    if args.apply and not args.acknowledge_queue_write:
        raise QueueImportError('--apply requires --acknowledge-queue-write')
    if args.max_new < 0 or args.max_priority < 0 or args.max_pending < 0:
        raise QueueImportError('Daily import limits must be >=0 (0 means no volume cap)')
    files = _list_spool(Path(args.spool_dir))
    result = {
        'ok': True, 'version': '4.3.8', 'mode': 'apply' if args.apply else 'preview',
        'db_read_only': not args.apply, 'spool_files_pending_before': len(files),
        'spool_files_examined': 0, 'spool_files_drained': 0, 'new_jobs': 0,
        'promoted_existing_pending': 0, 'priority_added': 0,
        'alerts_sent': 0, 'brave_ingestion_enabled': False,
        'max_new_per_invocation': args.max_new,
        'max_priority': args.max_priority,
        'priority_backlog_override': args.allow_priority_over_pending_ceiling,
    }
    remaining = None if args.max_new == 0 else args.max_new
    for path in files:
        if remaining is not None and remaining <= 0:
            break
        result['spool_files_examined'] += 1
        before = path.lstat()
        current = audit(_args(args, path, apply=args.apply,
                              new_limit=0 if remaining is None else remaining))
        if args.apply:
            n = int(current['new_jobs'])
            if remaining is not None:
                if n > remaining:
                    raise QueueImportError('Daily importer exceeded the remaining allowance')
                remaining -= n
            result['new_jobs'] += n
            result['promoted_existing_pending'] += current.get('promoted_existing_pending', 0)
            result['priority_added'] += current.get('priority_added', 0)
            # Delete only when every candidate is already in jobs or no longer
            # promotable. All results remain in the existing private audit JSONL.
            after = audit(_args(args, path, apply=False, new_limit=1))
            if after['not_in_jobs'] == 0 and after['existing_pending_promotable'] == 0:
                latest = path.lstat()
                if (latest.st_ino != before.st_ino or latest.st_dev != before.st_dev or
                        latest.st_size != before.st_size or latest.st_mtime_ns != before.st_mtime_ns):
                    raise QueueImportError('Discovery pending file changed during import; not deleting')
                path.unlink()
                result['spool_files_drained'] += 1
            # When the lane is full, scanning more input cannot add priorities.
            if (args.max_priority and
                    current.get('pending_discovery_priority_after', 0) >= args.max_priority):
                break
        else:
            result['new_jobs'] += current['would_insert']
            result['promoted_existing_pending'] += current['would_promote_existing']
            # Preview counts are upper bounds across files: no reservations made.
            if remaining is not None:
                remaining -= current['would_insert']
    result['spool_files_pending_after'] = len(_list_spool(Path(args.spool_dir)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spool-dir', required=True)
    parser.add_argument('--db', default='multi/state/crawler_production.sqlite3')
    parser.add_argument('--domains', default='domains.json')
    parser.add_argument('--max-new', type=int, default=0,
                        help='0: import all eligible new jobs')
    parser.add_argument('--max-priority', type=int, default=0,
                        help='0: no Discovery priority lane ceiling')
    parser.add_argument('--max-pending', type=int, default=0,
                        help='0: no global pending count ceiling')
    parser.add_argument('--allow-priority-over-pending-ceiling', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--acknowledge-queue-write', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = drain(args)
    except (OSError, QueueImportError, sqlite3.DatabaseError, ValueError) as exc:
        result = {
            'ok': False, 'mode': 'error',
            'error': str(exc) if isinstance(exc, QueueImportError) else type(exc).__name__,
            'db_writes': 'unknown_if_partial_import', 'alerts_sent': 0,
        }
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
