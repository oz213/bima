"""Conservative process-safe monthly Brave Search API request reservation.

The ledger stores only the UTC calendar month and count of *attempts reserved*
by this local BIMA deployment. It never stores Brave search results, URLs,
search response bodies, or API credentials. This is a local guard only; the
Brave account's dashboard limits remain the authoritative spend controls.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
import re
from pathlib import Path
import stat
import tempfile


class BraveQuotaError(ValueError):
    """Safe failure string without secrets or search content."""


def _flags(base):
    return base | getattr(os, 'O_NOFOLLOW', 0)


def _check_regular_private(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise BraveQuotaError('Brave quota file must be a private regular file (0600)')
    return st


def reserve_request(path, limit=900, now=None):
    """Atomically reserve one attempted Brave call; returns (allowed, used).

    Reserve BEFORE the network call so process crashes and concurrent manual
    invocations cannot exceed the local budget. Failed calls conservatively
    count as spent. A calendar-month rollover resets only this local ledger.
    """
    if not path or not 1 <= limit <= 1000:
        raise BraveQuotaError('Brave quota ledger and a limit of 1..1000 are required')
    target = Path(path)
    parent = target.parent
    if not parent.is_dir():
        raise BraveQuotaError('Brave quota directory is missing')
    if parent.stat().st_mode & 0o002:
        raise BraveQuotaError('Brave quota directory cannot be world-writable')
    if target.is_symlink():
        raise BraveQuotaError('Brave quota ledger cannot be a symlink')
    month = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y-%m')
    lock_path = Path(str(target) + '.lock')
    if lock_path.is_symlink():
        raise BraveQuotaError('Brave quota lock cannot be a symlink')
    descriptor = os.open(lock_path, _flags(os.O_CREAT | os.O_RDWR), 0o600)
    try:
        with os.fdopen(descriptor, 'r+b') as lock_handle:
            descriptor = -1
            _check_regular_private(lock_path)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            used = 0
            if target.exists():
                _check_regular_private(target)
                fd = os.open(target, _flags(os.O_RDONLY))
                try:
                    with os.fdopen(fd, 'rb') as source:
                        if os.fstat(source.fileno()).st_size > 4096:
                            raise BraveQuotaError('Brave quota ledger exceeds 4 KiB')
                        try:
                            record = json.load(source)
                        except (ValueError, UnicodeError):
                            raise BraveQuotaError('Brave quota ledger is malformed') from None
                except OSError:
                    raise BraveQuotaError('Brave quota ledger unreadable') from None
                if not isinstance(record, dict) or record.get('version') != 1:
                    raise BraveQuotaError('Brave quota ledger has invalid version')
                saved_month, saved_count = record.get('month_utc'), record.get('attempts')
                if (not isinstance(saved_month, str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', saved_month)
                        or not isinstance(saved_count, int) or isinstance(saved_count, bool)
                        or not 0 <= saved_count <= 1000):
                    raise BraveQuotaError('Brave quota ledger has invalid counters')
                if saved_month == month:
                    used = saved_count
            if used >= limit:
                return False, used
            used += 1
            fd, tmp = tempfile.mkstemp(prefix='.brave-quota-', dir=str(parent))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump({'version': 1, 'month_utc': month, 'attempts': used}, handle)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                if target.is_symlink():
                    raise BraveQuotaError('Brave quota ledger changed to a symlink')
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True, used
    finally:
        if descriptor >= 0:
            os.close(descriptor)
