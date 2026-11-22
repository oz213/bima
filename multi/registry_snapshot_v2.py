"""Safe, versioned Registry snapshots. Only the explicit operator CLI writes cycles.

The live crawler reads the active snapshot's pending queue; Telegram/Discovery
keep their existing paths. The per-job ID is never recycled or deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_snapshot_runs_v2 (
  cycle_id TEXT PRIMARY KEY,
  source_sha256 TEXT NOT NULL,
  url_count INTEGER NOT NULL,
  skipped_invalid INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_snapshot_active_v2 (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  cycle_id TEXT NOT NULL REFERENCES registry_snapshot_runs_v2(cycle_id),
  ready INTEGER NOT NULL DEFAULT 0 CHECK(ready IN (0,1))
);
CREATE TABLE IF NOT EXISTS registry_snapshot_queue_v2 (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  ordinal INTEGER NOT NULL UNIQUE,
  pending INTEGER NOT NULL CHECK(pending IN (0,1)),
  blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_registry_snapshot_pending_v2
  ON registry_snapshot_queue_v2(ordinal) WHERE pending=1;
CREATE TABLE IF NOT EXISTS registry_snapshot_job_history_v2 (
  cycle_id TEXT NOT NULL,
  job_id INTEGER NOT NULL,
  previous_status TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  risk TEXT,
  reason TEXT,
  classifier_json TEXT,
  updated_at TEXT,
  PRIMARY KEY(cycle_id,job_id)
);
CREATE TRIGGER IF NOT EXISTS trg_registry_snapshot_status_v2
  AFTER UPDATE OF status ON jobs
  BEGIN
    UPDATE registry_snapshot_queue_v2
    SET pending=CASE WHEN NEW.status='pending' AND blocked=0 THEN 1 ELSE 0 END
    WHERE job_id=NEW.id;
  END;
"""
# Terminal alerts and any job referenced by a Telegram/Pre-Alert are not reset.
RESETTABLE = ('complete', 'inconclusive', 'retry_http', 'failed',
              'http_inflight', 'browser_queued')


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def active_cycle(conn):
    if not has_table(conn, 'registry_snapshot_active_v2'):
        return None
    row = conn.execute('SELECT cycle_id FROM registry_snapshot_active_v2 WHERE singleton=1').fetchone()
    return row[0] if row else None


def active_ready(conn):
    if not has_table(conn, 'registry_snapshot_active_v2'):
        return False
    row = conn.execute('SELECT ready FROM registry_snapshot_active_v2 WHERE singleton=1').fetchone()
    return bool(row and row[0] == 1)


def mark_ready(conn, cycle_id, source_sha256):
    """Publish the cutover only AFTER urls.json was atomically replaced."""
    with conn:
        row = conn.execute('SELECT a.cycle_id,r.source_sha256 '
                           'FROM registry_snapshot_active_v2 a JOIN registry_snapshot_runs_v2 r '
                           'ON r.cycle_id=a.cycle_id WHERE a.singleton=1').fetchone()
        if not row or row != (cycle_id, source_sha256):
            raise ValueError('Active cycle/hash differs; refusing to enable Registry queue')
        conn.execute('UPDATE registry_snapshot_active_v2 SET ready=1 WHERE singleton=1')


def find_pending_registry(conn, cursor, *, wrap):
    """Return (id,url) or None if an active snapshot has no eligible pending job.

    None is also returned when the table is absent; the caller checks active_cycle
    to decide if the legacy queue fallback is allowed.
    """
    if active_cycle(conn) is None or not active_ready(conn):
        return None
    exclusions = ''
    if has_table(conn, 'discovery_job_priority'):
        exclusions = (' AND NOT EXISTS (SELECT 1 FROM discovery_job_priority p '
                      'WHERE p.job_id=q.job_id)')
    sql = ('SELECT j.id,j.url FROM registry_snapshot_queue_v2 q '
           'INDEXED BY idx_registry_snapshot_pending_v2 '
           'CROSS JOIN jobs j ON j.id=q.job_id '
           'WHERE q.pending=1 AND q.ordinal>? AND j.status=\'pending\''
           + exclusions + ' ORDER BY q.ordinal LIMIT 1')
    row = conn.execute(sql, (cursor,)).fetchone()
    if row is None and wrap:
        row = conn.execute(sql.replace('q.ordinal>?', 'q.ordinal<=?'), (cursor,)).fetchone()
    return row


def cursor_for_job(conn, job_id):
    if active_cycle(conn) is None:
        return job_id
    row = conn.execute('SELECT ordinal FROM registry_snapshot_queue_v2 WHERE job_id=?',
                       (job_id,)).fetchone()
    if not row:
        raise ValueError('Routine job is not part of the active Registry snapshot')
    return row[0]


def _utcnow():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_snapshot(path):
    """Validate source with the *same normalizer* used by the existing crawler.

    No DNS/HTTP requests; source order is preserved. This is intentionally a
    bounded on-disk operator operation, not run in the HTTP worker hot path.
    """
    from telegram_checks import normalize_input_url

    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError('Snapshot source symlink is not allowed')
    source = candidate.resolve(strict=True)
    if not source.is_file():
        raise ValueError('Source must be a regular JSON file, not a symlink')
    raw = source.read_bytes()
    data = json.loads(raw)
    items = data.get('urls') if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError('Snapshot must contain a non-empty urls array')
    urls = {}
    bad = 0
    for item in items:
        clean = normalize_input_url(item)
        if clean:
            urls.setdefault(clean, None)
        else:
            bad += 1
    if not urls or bad > max(2, len(items) // 100):
        raise ValueError('Snapshot empty or too many invalid URLs; inspect PDNS source')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'urls': tuple(urls),
            'invalid': bad, 'source': str(source)}


def _safe_cycle_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,79}', value or ''):
        raise ValueError('Cycle ID must be 3..80 safe ASCII characters')
    return value


def _table_exclusion(conn):
    parts = []
    if has_table(conn, 'telegram_checks'):
        parts.append('NOT EXISTS (SELECT 1 FROM telegram_checks t WHERE t.job_id=j.id)')
    if has_table(conn, 'bima_prealerts'):
        parts.append('NOT EXISTS (SELECT 1 FROM bima_prealerts b WHERE b.job_id=j.id)')
    return (' AND ' + ' AND '.join(parts)) if parts else ''


def _reset_where(conn):
    statuses = ','.join("'" + st + "'" for st in RESETTABLE)
    return ("j.status IN (" + statuses + ") " + _table_exclusion(conn))


def apply_snapshot(conn, *, cycle_id, snapshot, allow_major_shrink=False):
    """Atomic DB cutover. Caller must stop BOTH Telegram and crawler services.

    Does not delete job IDs, review, alert, evidence, training or delivery state.
    A separate input-file atomic replacement is done by the operator CLI.
    """
    cycle_id = _safe_cycle_id(cycle_id)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('BEGIN IMMEDIATE')
    try:
        # sqlite3.executescript implicitly commits; execute each schema statement
        # through sqlite3.complete_statement to keep one uninterrupted transaction.
        statement = ''
        for line in SCHEMA.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    conn.execute(statement)
                statement = ''
        if statement.strip():
            raise ValueError('Invalid snapshot migration')

        previous = conn.execute(
            'SELECT source_sha256 FROM registry_snapshot_runs_v2 WHERE cycle_id=?',
            (cycle_id,)).fetchone()
        if previous:
            if previous[0] != snapshot['sha256']:
                raise ValueError('Cycle ID was previously used for a different snapshot')
            current = active_cycle(conn)
            if current != cycle_id:
                raise ValueError('Cycle ID already closed; use a new cycle ID')
            conn.commit()
            return {'status': 'already_active', 'cycle_id': cycle_id}

        active = active_cycle(conn)
        if active and not allow_major_shrink:
            old_count = conn.execute('SELECT url_count FROM registry_snapshot_runs_v2 WHERE cycle_id=?',
                                     (active,)).fetchone()[0]
            if len(snapshot['urls']) < old_count // 2:
                raise ValueError('Snapshot shrank by over 50%; check PDNS source before applying')
        # TEMP table avoids per-row Python SQL lookups on large PDNS snapshots.
        conn.execute('DROP TABLE IF EXISTS temp.registry_snapshot_source_v2')
        conn.execute('CREATE TEMP TABLE registry_snapshot_source_v2 '
                     '(ordinal INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, hostname TEXT NOT NULL)')
        conn.executemany('INSERT INTO registry_snapshot_source_v2 VALUES (?,?,?)',
                         ((seq, url, (urlsplit(url).hostname or '').lower().rstrip('.'))
                          for seq, url in enumerate(snapshot['urls'], start=1)))
        # Block cross-scheme duplicates: http://host and https://host are
        # different job IDs, but must not automatically send duplicate alerts.
        conn.execute('DROP TABLE IF EXISTS temp.registry_protected_hosts_v2')
        conn.execute('CREATE TEMP TABLE registry_protected_hosts_v2 '
                     '(hostname TEXT PRIMARY KEY)')
        protected_urls = [r[0] for r in conn.execute(
            "SELECT url FROM jobs WHERE status IN "
            "('alerted','pending_alert','prealert_send_unknown','review_approved',"
            "'review_rejected','awaiting_review','review_required')")]
        if has_table(conn, 'bima_prealerts'):
            protected_urls.extend(r[0] for r in conn.execute(
                'SELECT j.url FROM jobs j JOIN bima_prealerts b ON b.job_id=j.id'))
        if has_table(conn, 'telegram_checks'):
            protected_urls.extend(r[0] for r in conn.execute(
                'SELECT j.url FROM jobs j JOIN telegram_checks t ON t.job_id=j.id'))
        protected_hosts = set()
        for old_url in protected_urls:
            try:
                host = (urlsplit(old_url or '').hostname or '').lower().rstrip('.')
                if host:
                    protected_hosts.add(host)
            except (ValueError, AttributeError):
                continue
        conn.executemany('INSERT INTO registry_protected_hosts_v2 VALUES (?)',
                         ((host,) for host in protected_hosts))
        now = _utcnow()
        conn.execute('INSERT OR IGNORE INTO jobs(url,updated_at) '
                     'SELECT url,? FROM registry_snapshot_source_v2', (now,))
        exclusion = _table_exclusion(conn)
        statuses = ','.join("'" + st + "'" for st in RESETTABLE)
        where = ("j.status IN (" + statuses + ")" + exclusion
                 + ' AND NOT EXISTS (SELECT 1 FROM registry_protected_hosts_v2 h '
                   'WHERE h.hostname=s.hostname)')
        conn.execute('INSERT INTO registry_snapshot_job_history_v2 '
                     '(cycle_id,job_id,previous_status,attempts,risk,reason,classifier_json,updated_at) '
                     'SELECT ?,j.id,j.status,j.attempts,j.risk,j.reason,j.classifier_json,j.updated_at '
                     'FROM jobs j JOIN registry_snapshot_source_v2 s ON s.url=j.url '
                     'WHERE ' + where, (cycle_id,))
        archived = conn.execute('SELECT changes()').fetchone()[0]
        # Existing alert cases are intentionally NOT requeued, even if sites changed.
        conn.execute('DELETE FROM registry_snapshot_queue_v2')
        conn.execute("UPDATE jobs SET status='pending', attempts=0, http_ms=NULL, "
                     "browser_ms=NULL, http_status=NULL, reason=NULL,risk=NULL, "
                     "classifier_json=NULL,final_url=NULL,evidence_path=NULL, "
                     "alert_confidence=NULL,error=NULL,route_reason=NULL, "
                     "error_category=NULL,classification_ms=NULL,next_retry_at=NULL, "
                     "bert_ms=NULL,dns_result=NULL,dns_errno=NULL,dns_host=NULL,updated_at=? "
                     "WHERE id IN (SELECT job_id FROM registry_snapshot_job_history_v2 "
                     "WHERE cycle_id=?)", (now, cycle_id))
        conn.execute('INSERT INTO registry_snapshot_queue_v2(job_id,ordinal,pending,blocked) '
                     'SELECT j.id,s.ordinal, '
                     'CASE WHEN j.status=\'pending\' AND h.hostname IS NULL THEN 1 ELSE 0 END, '
                     'CASE WHEN h.hostname IS NULL THEN 0 ELSE 1 END '
                     'FROM registry_snapshot_source_v2 s JOIN jobs j ON j.url=s.url '
                     'LEFT JOIN registry_protected_hosts_v2 h ON h.hostname=s.hostname')
        conn.execute('INSERT INTO registry_snapshot_runs_v2 '
                     '(cycle_id,source_sha256,url_count,skipped_invalid,created_at) '
                     'VALUES(?,?,?,?,?)',
                     (cycle_id, snapshot['sha256'], len(snapshot['urls']), snapshot['invalid'], now))
        conn.execute('INSERT INTO registry_snapshot_active_v2(singleton,cycle_id,ready) '
                     'VALUES(1,?,0) ON CONFLICT(singleton) DO UPDATE '
                     'SET cycle_id=excluded.cycle_id,ready=0', (cycle_id,))
        pending = conn.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2 '
                               'WHERE pending=1').fetchone()[0]
        blocked = conn.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2 '
                               'WHERE blocked=1').fetchone()[0]
        conn.commit()
        return {'status': 'applied', 'cycle_id': cycle_id, 'urls': len(snapshot['urls']),
                'invalid': snapshot['invalid'], 'requeued': archived,
                'active_pending': pending, 'protected_hosts': blocked,
                'protected_or_terminal': len(snapshot['urls']) - pending}
    except BaseException:
        conn.rollback()
        raise
