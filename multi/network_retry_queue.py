"""Bounded automatic retry selection for the long-lived BIMA multi-worker.

This module does not perform network requests or update SQLite. The caller
claims the returned row inside its existing BEGIN IMMEDIATE transaction.
Jobs with an unknown/NULL deadline are deliberately excluded (fail closed).
"""
from __future__ import annotations

from collections import deque

from routine_queue import find_next_pending_job
from discovery_priority import find_next_discovery_job, find_next_registry_job


class RetryPacer:
    """Reserve at most 1 in ``every`` non-Telegram slots for due retries.

    Discovery and Registry dispatches both contribute to retry pacing.
    The rolling one-minute cap also applies when no routine job exists.
    Interactive Telegram work is never counted against the routine quota.
    """

    def __init__(self, every: int = 5, max_per_minute: int = 10):
        if every < 2:
            raise ValueError('auto-retry-every must be >= 2')
        if max_per_minute < 1:
            raise ValueError('auto-retry-max-per-minute must be >= 1')
        self.every = every
        self.max_per_minute = max_per_minute
        self.routines_since_retry = 0
        self.retry_times = deque()
        self.retries_dispatched = 0

    def can_retry(self, now: float, *, routine_available: bool) -> bool:
        while self.retry_times and now - self.retry_times[0] >= 60:
            self.retry_times.popleft()
        if len(self.retry_times) >= self.max_per_minute:
            return False
        return not routine_available or self.routines_since_retry >= self.every - 1

    def record(self, kind: str, now: float) -> None:
        """Record a successfully claimed job. ``telegram`` does not affect quota."""
        if kind in ('routine', 'discovery'):
            self.routines_since_retry += 1
        elif kind == 'retry':
            self.retry_times.append(now)
            self.retries_dispatched += 1
            self.routines_since_retry = 0
        elif kind not in ('telegram', 'none'):
            raise ValueError(f'unknown queue kind: {kind}')


def find_due_network_retry(conn, now_utc: str, max_attempts: int):
    """Find one due DNS/timeout retry. Existing (status,id) index is sufficient.

    ``attempts`` is incremented when the crawler claims the job. A row with
    attempts = max_attempts - 1 gets one final chance; exhausting that attempt
    transitions to inconclusive via the existing plan_http_outcome() logic.
    """
    return conn.execute(
        "SELECT id,url FROM jobs "
        "WHERE status='retry_http' "
        "AND error_category IN ('dns_error','timeout') "
        "AND attempts>=1 AND attempts<? "
        "AND next_retry_at IS NOT NULL AND next_retry_at<=? "
        "ORDER BY id LIMIT 1",
        (max_attempts, now_utc),
    ).fetchone()


def choose_next_job(conn, last_id: int, *, interactive: bool, priority,
                    pacer: RetryPacer | None, now_utc: str, now_monotonic: float,
                    max_attempts: int, discovery_pacer=None):
    """Choose (job, kind) without changing SQLite or the pending cursor.

    Telegram is first. Discovery precedes Registry within a fair 4:1 burst
    limit; due retries keep their bounded pacing (typically 1 in 5 dispatches).
    """
    if priority is not None:
        return priority, 'telegram'
    routine = find_next_registry_job(conn, last_id, wrap=interactive)
    discovery = find_next_discovery_job(conn)
    # Preserve BIMA's existing rate-limited due-retry cadence even during a
    # Discovery burst. Telegram is always above both paths.
    if pacer is not None and pacer.can_retry(
            now_monotonic, routine_available=(routine is not None or discovery is not None)):
        due = find_due_network_retry(conn, now_utc, max_attempts)
        if due is not None:
            return due, 'retry'
    if discovery is not None and (discovery_pacer is None or
                                  discovery_pacer.allow(routine_available=routine is not None)):
        return discovery, 'discovery'
    if routine is not None:
        return routine, 'routine'
    if discovery is not None:
        return discovery, 'discovery'
    return None, 'none'
