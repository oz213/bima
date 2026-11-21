#!/usr/bin/env python3
"""Guarded Mac-side installer for BIMA v4.3.7 Discovery priority dispatch.

The BIMA repository source is large and operators may have local changes.
Refuse unfamiliar crawler versions; never edit production server directly.
Default --check makes no changes; --apply atomically edits only the exact
supported dispatch anchors in the Mac repository's crawler_multi.py.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import tempfile


IMPORT_OLD = 'from network_retry_queue import RetryPacer, choose_next_job\n'
IMPORT_NEW = IMPORT_OLD + 'from discovery_priority import DiscoveryPacer\n'

INIT_OLD = '''        if retry_pacer is not None:
            log(f'Auto network retry enabled: every={retry_pacer.every}, '
                f'max_per_minute={retry_pacer.max_per_minute}')
        limit = args.limit or float('inf')
'''
INIT_NEW = INIT_OLD.replace(
    "        limit = args.limit or float('inf')\n",
    "        discovery_pacer = DiscoveryPacer(max_burst=4)\n"
    "        limit = args.limit or float('inf')\n",
)

CALL_OLD = '''                    pacer=retry_pacer, now_utc=utcnow(), now_monotonic=now_mono,
                    max_attempts=args.max_network_attempts)
'''
CALL_NEW = CALL_OLD.replace(
    '                    max_attempts=args.max_network_attempts)\n',
    '                    max_attempts=args.max_network_attempts,\n'
    '                    discovery_pacer=discovery_pacer)\n',
)

RECORD_OLD = '''                if retry_pacer is not None:
                    retry_pacer.record(kind, now_mono)
            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)
'''
RECORD_NEW = RECORD_OLD.replace(
    '            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)\n',
    '                discovery_pacer.record(kind)\n'
    '            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)\n',
)

PAIRS = (
    (IMPORT_OLD, IMPORT_NEW), (INIT_OLD, INIT_NEW),
    (CALL_OLD, CALL_NEW), (RECORD_OLD, RECORD_NEW),
)


def plan_change(source):
    """Return (modified source, status) or refuse half-upgraded/unrecognized code."""
    has_old = [source.count(old) for old, _ in PAIRS]
    has_new = [source.count(new) for _, new in PAIRS]
    if all(n == 1 for n in has_new) and has_old[1:] == [0, 0, 0]:
        # IMPORT_OLD is a substring of IMPORT_NEW and is deliberately special.
        compile(source, 'multi/crawler_multi.py', 'exec')
        return source, 'already upgraded'
    if not (all(n == 1 for n in has_old) and
            all(n == 0 for n in has_new)):
        raise ValueError('Unexpected/partial crawler dispatch version; no change')
    changed = source
    for old, new in PAIRS:
        changed = changed.replace(old, new, 1)
    compile(changed, 'multi/crawler_multi.py', 'exec')
    return changed, 'compatible version'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--path', type=Path, default=Path('multi/crawler_multi.py'))
    p.add_argument('--apply', action='store_true', help='Explicit Mac source edit')
    args = p.parse_args()
    path = args.path
    if not path.is_file() or path.stat().st_size < 20000:
        p.error('Expected full BIMA crawler_multi.py (>=20 KiB), no changes')
    original = path.read_bytes()
    if b'\0' in original:
        p.error('Unexpected binary source')
    crlf = b'\r\n' in original
    source = original.decode('utf-8').replace('\r\n', '\n')
    try:
        updated, status = plan_change(source)
    except (ValueError, SyntaxError) as exc:
        p.error(str(exc))
    print(f'{path}: {status}; changes_needed={updated != source}; syntax=OK')
    if not args.apply:
        print('CHECK ONLY. Run again with --apply on Mac after Git patch.')
        return
    if updated == source:
        print('Already upgraded; source unchanged.')
        return
    bits = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix='.bima-discovery-priority-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(updated.replace('\n', '\r\n').encode('utf-8') if crlf else updated.encode('utf-8'))
        os.chmod(name, bits)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print('Crawler Mac source updated. Test, review, commit and push; do not edit server source.')


if __name__ == '__main__':
    main()
