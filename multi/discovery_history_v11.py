"""Durable BIMA Discovery provenance and outcome history (SQLite v11).

Priority metadata is transient, but evidence of where a job came from and its
completed observations must survive discovery_priority cleanup and Registry
snapshot requeues. This module performs no network calls or model inference.

Install once with the operator CLI. The small triggers observe *only* jobs
currently tagged in discovery_job_priority, never ordinary Registry jobs.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

SOURCE_TABLE = 'discovery_history_sources_v11'
EVENT_TABLE = 'discovery_history_outcomes_v11'
TRIGGERS = (
    'trg_bima_discovery_provenance_insert_v11',
    'trg_bima_discovery_outcome_status_v11',
    'trg_bima_discovery_outcome_cleanup_v11',
)

# These states are not final verdicts. pending_alert is included in the
# history as an observation, not represented as a confirmed BIMA alert.
NON_RESULT_STATES = ('pending', 'http_inflight', 'browser_queued', 'retry_http')
NON_RESULT_SQL = ','.join("'" + value + "'" for value in NON_RESULT_STATES)

CREATE_SOURCES = f"""
CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} (
  job_id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  first_provider TEXT NOT NULL CHECK(first_provider IN ('google','urlscan','brave')),
  first_discovered_at TEXT NOT NULL,
  last_provider TEXT NOT NULL CHECK(last_provider IN ('google','urlscan','brave')),
  last_discovered_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
)
"""
CREATE_OUTCOMES = f"""
CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  status TEXT NOT NULL,
  risk TEXT,
  reason TEXT,
  classifier_json TEXT,
  evidence_path TEXT,
  final_url TEXT,
  http_status INTEGER,
  bert_ms INTEGER,
  alert_confidence REAL,
  attempts INTEGER NOT NULL
)
"""
CREATE_OUTCOME_INDEX = (f'CREATE INDEX IF NOT EXISTS idx_discovery_outcomes_job_v11 '
                        f'ON {EVENT_TABLE}(job_id, id)')

# All references to NEW/OLD are in CREATE TRIGGER statements only.
CREATE_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGERS[0]}
AFTER INSERT ON discovery_job_priority
BEGIN
  INSERT OR IGNORE INTO {SOURCE_TABLE}
    (job_id,url,first_provider,first_discovered_at,
     last_provider,last_discovered_at,recorded_at)
  SELECT NEW.job_id,j.url,NEW.provider,NEW.discovered_at,
         NEW.provider,NEW.discovered_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM jobs j WHERE j.id=NEW.job_id;
  UPDATE {SOURCE_TABLE}
  SET last_provider=NEW.provider,last_discovered_at=NEW.discovered_at
  WHERE job_id=NEW.job_id;
END
"""

OUTCOME_COLUMNS = ('job_id,observed_at,status,risk,reason,classifier_json,'
                   'evidence_path,final_url,http_status,bert_ms,alert_confidence,attempts')
JOB_TO_OUTCOME = ('j.id,COALESCE(NULLIF(j.updated_at,\'\'),strftime(\'%Y-%m-%dT%H:%M:%fZ\',\'now\')),'
                  'j.status,j.risk,j.reason,j.classifier_json,j.evidence_path,j.final_url,'
                  'j.http_status,j.bert_ms,j.alert_confidence,j.attempts')
# Avoid duplicates from backfill or cleanup, while allowing genuine repeat
# status transitions to be stored even when they happen within one second.
BACKFILL_DEDUPE = f"""NOT EXISTS (
 SELECT 1 FROM {EVENT_TABLE} e WHERE e.job_id=j.id AND e.status=j.status
 AND e.attempts=j.attempts AND e.observed_at=COALESCE(
   NULLIF(j.updated_at,''),strftime('%Y-%m-%dT%H:%M:%fZ','now')))
"""

NEW_TO_OUTCOME = ('NEW.id,COALESCE(NULLIF(NEW.updated_at,\'\'),strftime(\'%Y-%m-%dT%H:%M:%fZ\',\'now\')),'
                  'NEW.status,NEW.risk,NEW.reason,NEW.classifier_json,NEW.evidence_path,'
                  'NEW.final_url,NEW.http_status,NEW.bert_ms,NEW.alert_confidence,NEW.attempts')

CREATE_STATUS_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGERS[1]}
AFTER UPDATE OF status ON jobs
WHEN OLD.status IS NOT NEW.status
 AND NEW.status NOT IN ({NON_RESULT_SQL})
 AND EXISTS (SELECT 1 FROM discovery_job_priority WHERE job_id=NEW.id)
BEGIN
  INSERT INTO {EVENT_TABLE} ({OUTCOME_COLUMNS})
  VALUES ({NEW_TO_OUTCOME});
END
"""

CREATE_CLEANUP_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGERS[2]}
BEFORE DELETE ON discovery_job_priority
WHEN EXISTS (SELECT 1 FROM jobs j WHERE j.id=OLD.job_id
             AND j.status NOT IN ({NON_RESULT_SQL}))
BEGIN
  INSERT INTO {EVENT_TABLE} ({OUTCOME_COLUMNS})
  SELECT {JOB_TO_OUTCOME} FROM jobs j
  WHERE j.id=OLD.job_id AND {BACKFILL_DEDUPE};
END
"""

BACKFILL_SOURCES = f"""
INSERT OR IGNORE INTO {SOURCE_TABLE}
(job_id,url,first_provider,first_discovered_at,last_provider,last_discovered_at,recorded_at)
SELECT p.job_id,j.url,p.provider,p.discovered_at,p.provider,p.discovered_at,
       strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM discovery_job_priority p JOIN jobs j ON j.id=p.job_id
"""
BACKFILL_OUTCOMES = f"""
INSERT INTO {EVENT_TABLE} ({OUTCOME_COLUMNS})
SELECT {JOB_TO_OUTCOME}
FROM discovery_job_priority p JOIN jobs j ON j.id=p.job_id
WHERE j.status NOT IN ({NON_RESULT_SQL}) AND {BACKFILL_DEDUPE}
"""


def _require_schema(conn):
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('jobs','discovery_job_priority')"
    )}
    if names != {'jobs', 'discovery_job_priority'}:
        raise ValueError('Required production jobs/priority tables are missing; migration refused')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
    required = {'id','url','status','updated_at','risk','reason','classifier_json',
                'evidence_path','final_url','http_status','bert_ms','alert_confidence','attempts'}
    if not required.issubset(columns):
        raise ValueError('Production jobs schema is incompatible; migration refused')


def migrate(conn):
    """One short, idempotent transaction; no crawler stop or queue mutation.

    The caller must explicitly authorize a production write and verify an
    independent recent backup. Avoid conn.executescript: it commits pending
    transactions implicitly and would break migration atomicity.
    """
    _require_schema(conn)
    if str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower() == 'wal':
        raise ValueError('WAL mode requires separate storage migration review; refused')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for ddl in (CREATE_SOURCES, CREATE_OUTCOMES, CREATE_OUTCOME_INDEX,
                    CREATE_INSERT_TRIGGER, CREATE_STATUS_TRIGGER,
                    CREATE_CLEANUP_TRIGGER):
            conn.execute(ddl)
        # Capture active rows already in the live queue, including finished
        # items which the next auto-import would otherwise purge.
        conn.execute(BACKFILL_SOURCES)
        conn.execute(BACKFILL_OUTCOMES)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return summary(conn)


def disable_triggers(conn):
    """Emergency rollback of observer hooks only; NEVER delete history."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        for name in TRIGGERS:
            conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return summary(conn)


def _has_table(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def summary(conn):
    """Read-only, aggregated report; never output URLs, model text, or secrets."""
    installed = _has_table(conn, SOURCE_TABLE) and _has_table(conn, EVENT_TABLE)
    present_triggers = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
        TRIGGERS)}
    active_priority = (conn.execute('SELECT COUNT(*) FROM discovery_job_priority').fetchone()[0]
                       if _has_table(conn, 'discovery_job_priority') else None)
    result = {
        'installed': installed, 'triggers_ready': len(present_triggers) == len(TRIGGERS),
        'active_priority_records': active_priority,
        'source_records': 0, 'outcome_records': 0, 'missing_source_from_priority': None,
        'current_statuses': {},
    }
    if not installed:
        return result
    result['source_records'] = int(conn.execute(
        f'SELECT COUNT(*) FROM {SOURCE_TABLE}').fetchone()[0])
    result['outcome_records'] = int(conn.execute(
        f'SELECT COUNT(*) FROM {EVENT_TABLE}').fetchone()[0])
    if active_priority is not None:
        result['missing_source_from_priority'] = int(conn.execute(f'''
            SELECT COUNT(*) FROM discovery_job_priority p
            LEFT JOIN {SOURCE_TABLE} s ON s.job_id=p.job_id WHERE s.job_id IS NULL
        ''').fetchone()[0])
    result['current_statuses'] = {
        str(status): int(count) for status, count in conn.execute(f'''
            SELECT j.status, COUNT(*) FROM {SOURCE_TABLE} s
            JOIN jobs j ON j.id=s.job_id GROUP BY j.status
        ''')
    }
    return result


def recent_outcomes(conn, limit=10, job_id=None):
    """Only minimal verified metadata; do not emit raw URL/evidence/model JSON."""
    if not _has_table(conn, EVENT_TABLE):
        return []
    if job_id is not None:
        query = f'''
          SELECT e.id,e.job_id,s.first_provider,e.observed_at,e.status,
                 e.risk,e.reason,e.attempts,e.bert_ms,
                 CASE WHEN e.classifier_json IS NOT NULL THEN 1 ELSE 0 END,
                 j.status
          FROM {EVENT_TABLE} e JOIN {SOURCE_TABLE} s ON s.job_id=e.job_id
          LEFT JOIN jobs j ON j.id=e.job_id
          WHERE e.job_id=? ORDER BY e.id DESC LIMIT ?
        '''
        rows = conn.execute(query, (job_id,limit)).fetchall()
    else:
        query = f'''
          SELECT e.id,e.job_id,s.first_provider,e.observed_at,e.status,
                 e.risk,e.reason,e.attempts,e.bert_ms,
                 CASE WHEN e.classifier_json IS NOT NULL THEN 1 ELSE 0 END,
                 j.status
          FROM {EVENT_TABLE} e JOIN {SOURCE_TABLE} s ON s.job_id=e.job_id
          LEFT JOIN jobs j ON j.id=e.job_id
          ORDER BY e.id DESC LIMIT ?
        '''
        rows = conn.execute(query, (limit,)).fetchall()
    keys = ('event_id','job_id','provider','observed_at','recorded_status',
            'risk','reason','attempts','bert_ms','classifier_saved','current_status')
    return [dict(zip(keys,row)) for row in rows]


def open_existing_database(path, *, writable=False):
    target = Path(path)
    if not target.is_file():
        raise ValueError('Existing BIMA SQLite database was not found')
    uri = target.resolve(strict=True).as_uri() + ('?mode=rw' if writable else '?mode=ro')
    conn = sqlite3.connect(uri, uri=True, timeout=12)
    conn.execute('PRAGMA busy_timeout=12000')
    if not writable:
        conn.execute('PRAGMA query_only=ON')
    return conn


def cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='BIMA Discovery durable history v11')
    parser.add_argument('action', choices=('summary','recent','migrate','disable-triggers'))
    parser.add_argument('--db', default='/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3')
    parser.add_argument('--acknowledge-production-write', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--job-id', type=int)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 100 or args.job_id is not None and args.job_id <= 0:
        parser.error('--limit must be 1..100, --job-id positive')
    if args.action in ('migrate','disable-triggers') and not args.acknowledge_production_write:
        parser.error('Database change requires --acknowledge-production-write')
    if args.action not in ('migrate','disable-triggers') and args.acknowledge_production_write:
        parser.error('Write acknowledgement is only valid for migration/disable')
    try:
        with closing(open_existing_database(args.db, writable=args.action in ('migrate','disable-triggers'))) as conn:
            if args.action == 'migrate':
                result = migrate(conn)
                result['migration'] = 'complete'
            elif args.action == 'disable-triggers':
                result = disable_triggers(conn)
                result['triggers'] = 'disabled_without_deleting_history'
            elif args.action == 'summary':
                result = summary(conn)
            else:
                result = {'recent': recent_outcomes(conn, args.limit, args.job_id)}
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        # Avoid leaking database contents, DB paths or external secrets.
        print(json.dumps({'ok': False, 'error': (str(error) if isinstance(error, ValueError)
                                                  else type(error).__name__)}),
                   file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
