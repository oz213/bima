#!/usr/bin/env python3
"""Read-only-by-default cooldown scheduler for previously clean BIMA jobs.

This does *not* crawl or classify. It may change ONLY confirmed low-risk
'complete' jobs back to pending when explicitly authorized by an operator.
BIMA's existing IndoBERT/browser pipeline does the next inspection. Never
requeue alerted/pending_alert/in-flight jobs from this scheduler.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from proactive_discovery import (
    evidence_destination, load_seeds, normalize_url, open_jobs_db, pending_count,
    utcnow,
)

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_recrawl_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  previous_status TEXT NOT NULL,
  previous_risk TEXT,
  previous_final_url TEXT,
  previous_updated_at TEXT NOT NULL,
  previous_attempts INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  queued_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proactive_recrawl_job
  ON proactive_recrawl_audit(job_id, queued_at);
"""


@dataclass(frozen=True)
class DueJob:
    id: int
    url: str
    updated_at: str
    attempts: int
    final_url: str | None
    trigger: str


def is_in_seed_inventory(url: str, seeds: set[str]) -> bool:
    """Require valid .id URL and a matching Registry seed or child hostname."""
    cleaned = normalize_url(url)
    if not cleaned:
        return False
    parts = urlsplit(url)
    # Never requeue URL carrying query/fragment tokens from legacy state.
    if parts.query or parts.fragment:
        return False
    labels = (urlsplit(cleaned).hostname or '').split('.')
    for index in range(len(labels) - 1):
        if '.'.join(labels[index:]) in seeds:
            return True
    return False


def _valid_timestamp(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def select_due_jobs(conn: sqlite3.Connection, *, seeds: set[str],
                    now: datetime, regular_hours: float,
                    redirect_hours: float, scan_limit: int) -> list[DueJob]:
    """Prioritize fresh external redirect indicators, then routine stale clean jobs.

    Only SQLite metadata are read. A redirect is a *signal*, not proof of abuse.
    """
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    now = now.astimezone(timezone.utc)
    fast_cutoff = (now - timedelta(hours=redirect_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    regular_cutoff = (now - timedelta(hours=regular_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    redirect_table_exists = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='proactive_redirect_observations'"
    ).fetchone())
    results: dict[int, DueJob] = {}

    def append(rows, trigger: str, hours: float):
        for job_id, url, previous, attempts, final_url in rows:
            stamp = _valid_timestamp(previous)
            if stamp is None or now - stamp < timedelta(hours=hours):
                continue
            if not is_in_seed_inventory(url, seeds) or job_id in results:
                continue
            results[job_id] = DueJob(
                id=int(job_id), url=url, updated_at=previous,
                attempts=int(attempts or 0), final_url=final_url, trigger=trigger,
            )

    if redirect_table_exists:
        # Only newly observed external redirects since the last clean crawl.
        rows = conn.execute("""
            SELECT j.id,j.url,j.updated_at,j.attempts,j.final_url
            FROM jobs j
            WHERE j.status='complete' AND j.risk='low_risk'
              AND j.updated_at IS NOT NULL AND j.updated_at<=?
              AND EXISTS (
                SELECT 1 FROM proactive_redirect_observations r
                WHERE r.source_url=j.url AND r.external=1
                  AND r.last_seen>j.updated_at
              )
            ORDER BY j.updated_at ASC,j.id ASC LIMIT ?
        """, (fast_cutoff, scan_limit)).fetchall()
        append(rows, 'new_external_redirect', redirect_hours)

    regular = conn.execute("""
        SELECT id,url,updated_at,attempts,final_url FROM jobs
        WHERE status='complete' AND risk='low_risk'
          AND updated_at IS NOT NULL AND updated_at<=?
        ORDER BY updated_at ASC,id ASC LIMIT ?
    """, (regular_cutoff, scan_limit)).fetchall()
    append(regular, 'scheduled_clean_recheck', regular_hours)
    return sorted(results.values(), key=lambda x: (
        0 if x.trigger == 'new_external_redirect' else 1, x.updated_at, x.id,
    ))


def apply_rechecks(conn: sqlite3.Connection, *, due: list[DueJob],
                   max_rechecks: int, max_pending: int) -> tuple[int, int]:
    """Queue under BEGIN IMMEDIATE, record audit snapshot before loss of old state.

    The guarded update only affects a row that remains 'complete/low_risk' with
    its original timestamp. In-flight, alerted, or pending_alert rows are safe.
    """
    conn.executescript(AUDIT_SCHEMA)
    changed = 0
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        pending = pending_count(conn)
        for item in due:
            if changed >= max_rechecks or pending >= max_pending:
                break
            current = conn.execute("""
                SELECT status,risk,updated_at,attempts,final_url
                FROM jobs WHERE id=?
            """, (item.id,)).fetchone()
            if not current or current[0] != 'complete' or current[1] != 'low_risk':
                continue
            if current[2] != item.updated_at or current[3] != item.attempts:
                continue
            timestamp = utcnow()
            updated = conn.execute("""
                UPDATE jobs SET status='pending', updated_at=?, attempts=0,
                    http_ms=NULL, browser_ms=NULL, http_status=NULL,
                    reason=NULL, risk=NULL, classifier_json=NULL, final_url=NULL,
                    evidence_path=NULL, alert_confidence=NULL, error=NULL,
                    route_reason=NULL, error_category=NULL,
                    classification_ms=NULL, next_retry_at=NULL,
                    bert_ms=NULL, dns_result=NULL, dns_errno=NULL, dns_host=NULL
                WHERE id=? AND status='complete' AND risk='low_risk'
                  AND updated_at=? AND attempts=?
            """, (timestamp, item.id, item.updated_at, item.attempts)).rowcount
            if updated != 1:
                continue
            safe_final = (evidence_destination(current[4], base_url=item.url)
                          if current[4] else None)
            conn.execute("""
                INSERT INTO proactive_recrawl_audit
                  (job_id,url,previous_status,previous_risk,previous_final_url,
                   previous_updated_at,previous_attempts,trigger,queued_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (item.id, normalize_url(item.url) or item.url,
                  current[0], current[1], safe_final, current[2], current[3],
                  item.trigger, timestamp))
            changed += 1
            pending += 1
    return changed, pending


def run(args) -> dict:
    seeds, digest, invalid = load_seeds(Path(args.input))
    if not seeds:
        raise ValueError('No valid .id seeds in Registry urls.json')
    if args.apply and not args.allow_live_rechecks:
        raise ValueError('Live requeue needs --apply --allow-live-rechecks')
    conn = open_jobs_db(Path(args.db), apply=args.apply)
    if conn is None:
        raise FileNotFoundError('Existing BIMA jobs SQLite database is required')
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
        required = {'id', 'url', 'status', 'risk', 'updated_at', 'attempts', 'final_url'}
        if not required.issubset(columns):
            raise ValueError('BIMA jobs schema lacks required columns')
        pending_before = pending_count(conn)
        due = select_due_jobs(
            conn, seeds=set(seeds), now=datetime.now(timezone.utc),
            regular_hours=args.regular_hours,
            redirect_hours=args.redirect_hours, scan_limit=args.scan_limit,
        )
        changed = 0
        pending_after = pending_before
        if args.apply:
            changed, pending_after = apply_rechecks(
                conn, due=due, max_rechecks=args.max_rechecks,
                max_pending=args.max_pending,
            )
        return {
            'mode': 'apply' if args.apply else 'preview',
            'seeds_total': len(seeds), 'invalid_seed_entries': invalid,
            'due_eligible_scanned': len(due),
            'redirect_priority_due': sum(1 for job in due if job.trigger == 'new_external_redirect'),
            'new_pending_rechecks': changed,
            'pending_before': pending_before, 'pending_after': pending_after,
            'rechecks': [
                {'url': normalize_url(job.url), 'trigger': job.trigger,
                 'previous_checked_at': job.updated_at}
                for job in due[:args.preview_samples]
            ],
        }
    finally:
        conn.close()


def arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='BIMA .id recrawl scheduler (PREVIEW by default)')
    parser.add_argument('--input', default='urls.json', help='Registry domain baseline (read-only)')
    parser.add_argument('--db', default='multi/state/crawler_production.sqlite3')
    parser.add_argument('--apply', action='store_true', help='Requeue previously clean jobs')
    parser.add_argument('--allow-live-rechecks', action='store_true',
                        help='Explicit permission for classifier/BIMA alert side effects')
    parser.add_argument('--max-rechecks', type=int, default=10)
    parser.add_argument('--max-pending', type=int, default=100)
    parser.add_argument('--regular-hours', type=float, default=168,
                        help='Minimum age for ordinary low-risk recheck (default 7 days)')
    parser.add_argument('--redirect-hours', type=float, default=24,
                        help='Minimum age for new external-redirect recheck (default 24h)')
    parser.add_argument('--scan-limit', type=int, default=2000)
    parser.add_argument('--preview-samples', type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = arg_parser().parse_args(argv)
    if not 1 <= args.max_rechecks <= 50 or args.max_pending < 1:
        raise SystemExit('max-rechecks must be 1..50 and max-pending positive')
    if args.regular_hours < 24 or args.redirect_hours < 6:
        raise SystemExit('regular-hours must be >=24 and redirect-hours >=6')
    if not 1 <= args.scan_limit <= 10000 or args.preview_samples < 0:
        raise SystemExit('scan-limit must be 1..10000 and preview-samples >=0')
    try:
        result = run(args)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)[:300]}))
        return 1
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
