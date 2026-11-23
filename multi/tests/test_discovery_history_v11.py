"""BIMA Discovery durable provenance v11; synthetic SQLite only, no network."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery_history_v11 import (
    EVENT_TABLE, SOURCE_TABLE, TRIGGERS, cli, migrate, disable_triggers, summary,
    recent_outcomes, open_existing_database,
)


SCHEMA = """
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 url TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 http_ms INTEGER,
 browser_ms INTEGER,
 http_status INTEGER,
 reason TEXT,
 risk TEXT,
 classifier_json TEXT,
 final_url TEXT,
 evidence_path TEXT,
 alert_confidence REAL,
 error TEXT,
 updated_at TEXT,
 route_reason TEXT,
 error_category TEXT,
 classification_ms INTEGER,
 next_retry_at TEXT,
 bert_ms INTEGER,
 dns_result TEXT,
 dns_errno INTEGER,
 dns_host TEXT
);
CREATE TABLE discovery_job_priority (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 provider TEXT NOT NULL CHECK(provider IN ('google','urlscan','brave')),
 discovered_at TEXT NOT NULL
);
"""


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.db = Path(td.name) / 'prod.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)

    def connect(self):
        return sqlite3.connect(self.db)

    @staticmethod
    def job(conn, id_, status='pending', prioritized=True, provider='google'):
        conn.execute('INSERT INTO jobs(id,url,status,attempts,updated_at,reason,risk,classifier_json) '
                     'VALUES (?,?,?,?,?,?,?,?)',
                     (id_, f'https://example{id_}.co.id/', status, 1,
                      '2026-11-23T09:40:00Z', 'classified', 'low', '{"indobert":true}'))
        if prioritized:
            conn.execute('INSERT INTO discovery_job_priority(job_id,provider,discovered_at) '
                         'VALUES (?,?,?)', (id_,provider,'2026-11-23T09:20:00Z'))


class MigrationTests(HistoryFixture):
    def test_uninstalled_summary_is_read_only_and_refuses_missing_database(self):
        with closing(self.connect()) as conn:
            data = summary(conn)
            self.assertFalse(data['installed'])
            self.assertEqual(data['source_records'],0)
        missing = self.db.parent / 'no.sqlite3'
        with self.assertRaises(ValueError):
            open_existing_database(missing)
        self.assertFalse(missing.exists())

    def test_backfill_current_43_and_15_terminal_idempotent(self):
        with closing(self.connect()) as conn:
            statuses = (['complete'] * 11 + ['inconclusive'] * 4 +
                        ['browser_queued'] * 7 + ['http_inflight'] * 3 +
                        ['pending'] * 17 + ['retry_http'])
            self.assertEqual(len(statuses),43)
            for i, status in enumerate(statuses,1):
                self.job(conn, i, status)
            conn.commit()
            report = migrate(conn)
            self.assertTrue(report['installed'])
            self.assertTrue(report['triggers_ready'])
            self.assertEqual(report['source_records'],43)
            self.assertEqual(report['outcome_records'],15)
            self.assertEqual(report['missing_source_from_priority'],0)
            self.assertEqual(migrate(conn)['outcome_records'],15)
            self.assertEqual(summary(conn)['current_statuses']['pending'],17)

    def test_refuses_missing_priority_without_creating_tables(self):
        with closing(self.connect()) as conn:
            conn.execute('DROP TABLE discovery_job_priority')
            conn.commit()
            with self.assertRaisesRegex(ValueError, 'priority'):
                migrate(conn)
            self.assertFalse(summary(conn)['installed'])

    def test_refuses_incompatible_job_schema_without_mutation(self):
        with closing(self.connect()) as conn:
            conn.execute('ALTER TABLE jobs RENAME TO old_jobs')
            conn.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT,status TEXT)')
            conn.commit()
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                migrate(conn)
            self.assertFalse(summary(conn)['installed'])


class HistoryTriggerTests(HistoryFixture):
    def test_after_install_import_status_and_cleanup_are_auditable(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            self.job(conn,2,prioritized=False)
            conn.commit()
            self.assertEqual(summary(conn)['source_records'],1)
            conn.execute('UPDATE jobs SET status=?,risk=?,reason=?,classifier_json=?,updated_at=?,bert_ms=? '
                         'WHERE id=1', ('complete','low','http_classification_complete',
                                        '{"model":"IndoBERT"}',
                                        '2026-11-23T10:00:00Z',123))
            conn.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=2',
                         ('complete','2026-11-23T10:00:00Z'))
            conn.commit()
            events = recent_outcomes(conn)
            self.assertEqual(len(events),1)  # Routine job not captured.
            self.assertEqual(events[0]['recorded_status'],'complete')
            self.assertEqual(events[0]['reason'],'http_classification_complete')
            self.assertEqual(events[0]['bert_ms'],123)
            self.assertEqual(events[0]['classifier_saved'],1)
            conn.execute('DELETE FROM discovery_job_priority WHERE job_id=1')
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],1)  # No duplicate.
            self.assertEqual(summary(conn)['active_priority_records'],0)
            conn.execute("UPDATE jobs SET status='pending',classifier_json=NULL,reason=NULL WHERE id=1")
            conn.commit()
            self.assertEqual(recent_outcomes(conn)[0]['recorded_status'],'complete')
            self.assertEqual(recent_outcomes(conn)[0]['current_status'],'pending')
            self.assertEqual(conn.execute(f'SELECT classifier_json FROM {EVENT_TABLE}').fetchone()[0],
                             '{"model":"IndoBERT"}')

    def test_terminal_existing_at_install_saved_before_cleanup(self):
        with closing(self.connect()) as conn:
            self.job(conn,1,'inconclusive')
            self.job(conn,2,'browser_queued')
            conn.commit()
            migrate(conn)
            self.assertEqual(summary(conn)['outcome_records'],1)
            conn.execute('DELETE FROM discovery_job_priority')
            conn.commit()
            self.assertEqual(summary(conn)['source_records'],2)
            self.assertEqual(summary(conn)['outcome_records'],1)

    def test_pending_alert_is_not_converted_to_confirmed_alert(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            conn.execute("UPDATE jobs SET status='pending_alert',reason='delivery_ambiguous' WHERE id=1")
            conn.commit()
            self.assertEqual(recent_outcomes(conn)[0]['recorded_status'],'pending_alert')
            self.assertNotIn('alerted', [e['recorded_status'] for e in recent_outcomes(conn)])

    def test_rediscovery_keeps_first_provenance_updates_last(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1,provider='google')
            conn.execute('DELETE FROM discovery_job_priority WHERE job_id=1')
            conn.execute("UPDATE jobs SET status='pending' WHERE id=1")
            conn.execute("INSERT INTO discovery_job_priority(job_id,provider,discovered_at) "
                         "VALUES (1,'urlscan','2026-11-24T20:30:00Z')")
            conn.commit()
            row = conn.execute(f'SELECT first_provider,last_provider,last_discovered_at '
                               f'FROM {SOURCE_TABLE} WHERE job_id=1').fetchone()
            self.assertEqual(row, ('google','urlscan','2026-11-24T20:30:00Z'))

    def test_emergency_disable_preserves_history_and_reenable(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            conn.execute("UPDATE jobs SET status='complete' WHERE id=1")
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],1)
            stopped = disable_triggers(conn)
            self.assertFalse(stopped['triggers_ready'])
            self.assertEqual(stopped['outcome_records'],1)
            conn.execute("UPDATE jobs SET status='pending' WHERE id=1")
            conn.execute("UPDATE jobs SET status='inconclusive' WHERE id=1")
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],1)
            self.assertTrue(migrate(conn)['triggers_ready'])
            self.assertEqual(summary(conn)['outcome_records'],2)  # Backfill active last verdict.

    def test_repeat_result_at_same_timestamp_is_retained(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            conn.execute("UPDATE jobs SET status='inconclusive' WHERE id=1")
            conn.execute("UPDATE jobs SET status='pending' WHERE id=1")
            conn.execute("UPDATE jobs SET status='inconclusive' WHERE id=1")
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],2)
            conn.execute('DELETE FROM discovery_job_priority WHERE job_id=1')
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],2)

    def test_registry_snapshot_trigger_remains_functional(self):
        with closing(self.connect()) as conn:
            conn.executescript("""
                CREATE TABLE registry_status_audit(job_id INTEGER, value TEXT);
                CREATE TRIGGER trg_existing_registry_status AFTER UPDATE OF status ON jobs
                BEGIN INSERT INTO registry_status_audit(job_id,value)
                      VALUES(NEW.id,NEW.status); END;
            """)
            migrate(conn)
            self.job(conn,1)
            conn.execute("UPDATE jobs SET status='complete' WHERE id=1")
            conn.commit()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM registry_status_audit').fetchone()[0],1)
            self.assertEqual(summary(conn)['outcome_records'],1)

    def test_nonfinal_status_transitions_do_not_grow_outcome_table(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            for status in ('http_inflight','browser_queued','retry_http','pending'):
                conn.execute('UPDATE jobs SET status=? WHERE id=1', (status,))
            conn.commit()
            self.assertEqual(summary(conn)['outcome_records'],0)

    def test_cli_output_only_metadata_and_disallows_unapproved_migration(self):
        with closing(self.connect()) as conn:
            migrate(conn)
            self.job(conn,1)
            conn.execute("UPDATE jobs SET status='complete',classifier_json='secret payload' WHERE id=1")
            conn.commit()
        from io import StringIO
        from contextlib import redirect_stdout,redirect_stderr
        with redirect_stdout(StringIO()) as capture:
            code = cli(['recent','--db',str(self.db)])
        self.assertEqual(code,0)
        self.assertNotIn('secret payload',capture.getvalue())
        self.assertNotIn('https://',capture.getvalue())
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                cli(['migrate','--db',str(self.db)])


if __name__ == '__main__':
    unittest.main()
