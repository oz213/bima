"""Offline tests for earliest-due BIMA retries and read-only DNS triage."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

MULTI = Path(__file__).resolve().parents[1]
ROOT = MULTI.parent
sys.path.insert(0, str(MULTI))

from network_retry_queue import RetryPacer, choose_next_job, find_due_network_retry

NOW = '2026-11-22T11:00:00Z'
INDEX_SQL = (
    'CREATE INDEX IF NOT EXISTS idx_jobs_due_network_retry_v1 '
    'ON jobs(next_retry_at,id) WHERE status=\'retry_http\' '
    "AND error_category IN ('dns_error','timeout') "
    'AND next_retry_at IS NOT NULL'
)


def db_memory():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, url TEXT, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, error_category TEXT,
            dns_result TEXT, next_retry_at TEXT
        );
        CREATE INDEX idx_jobs_status ON jobs(status,id);
    ''')
    db.execute(INDEX_SQL)
    return db


def add(db, ident, status='retry_http', when=NOW, category='dns_error', attempts=1, dns='temporary_failure'):
    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
               (ident, f'https://test{ident}.id', status, attempts, category, dns, when))


class EarliestDueTests(unittest.TestCase):
    def setUp(self):
        self.db = db_memory()

    def tearDown(self):
        self.db.close()

    def test_oldest_due_first_not_oldest_id(self):
        add(self.db, 1, when='2026-11-22T10:59:59Z')
        add(self.db, 100, when='2026-11-21T00:00:00Z')
        self.assertEqual(find_due_network_retry(self.db, NOW, 3)[0], 100)

    def test_same_deadline_uses_lowest_id(self):
        add(self.db, 30)
        add(self.db, 2)
        self.assertEqual(find_due_network_retry(self.db, NOW, 3)[0], 2)

    def test_future_and_null_deadlines_never_selected(self):
        add(self.db, 1, when='2026-11-23T00:00:00Z')
        add(self.db, 2, when=None)
        self.assertIsNone(find_due_network_retry(self.db, NOW, 3))

    def test_terminal_and_unknown_categories_not_selected(self):
        add(self.db, 1, status='pending_alert')
        add(self.db, 2, category='other_network_error')
        add(self.db, 3, attempts=3)
        add(self.db, 4, attempts=0)
        self.assertIsNone(find_due_network_retry(self.db, NOW, 3))

    def test_timeout_can_be_selected(self):
        add(self.db, 1, category='timeout', dns='none')
        self.assertEqual(find_due_network_retry(self.db, NOW, 3)[0], 1)

    def test_old_fixture_without_index_fails_open_for_eligible_retries_only(self):
        # Existing unit-test SQLite fixtures have no partial index yet.
        self.db.execute('DROP INDEX idx_jobs_due_network_retry_v1')
        add(self.db, 1, when='2026-11-22T10:55:00Z')
        add(self.db, 2, when='2026-11-22T10:30:00Z')
        self.assertEqual(find_due_network_retry(self.db, NOW, 3)[0], 2)

    def test_partial_index_created_and_used(self):
        sql = self.db.execute('SELECT sql FROM sqlite_master WHERE name=?',
                              ('idx_jobs_due_network_retry_v1',)).fetchone()[0]
        self.assertIn('next_retry_at', sql)
        add(self.db, 2)
        # Check the actual query planner, not just that the index was created.
        details = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT id,url FROM jobs INDEXED BY idx_jobs_due_network_retry_v1 "
            "WHERE status='retry_http' AND error_category IN ('dns_error','timeout') "
            "AND attempts>=1 AND attempts<? "
            "AND next_retry_at IS NOT NULL AND next_retry_at<=? "
            "ORDER BY next_retry_at,id LIMIT 1", (3, NOW)
        ).fetchall()
        plan = ' '.join(str(row[3]) for row in details)
        self.assertIn('idx_jobs_due_network_retry_v1', plan)

    def test_retry_pacing_unchanged(self):
        add(self.db, 50)
        add(self.db, 1, status='pending', attempts=0, when=None)
        pacer = RetryPacer(every=5, max_per_minute=1)
        for moment in range(4):
            pacer.record('routine', float(moment))
        job, kind = choose_next_job(self.db, 0, interactive=True,
                                    priority=None, pacer=pacer, now_utc=NOW,
                                    now_monotonic=5., max_attempts=3)
        self.assertEqual((job[0], kind), (50, 'retry'))
        pacer.record(kind, 5.)
        self.assertFalse(pacer.can_retry(30., routine_available=False))
        self.assertTrue(pacer.can_retry(66., routine_available=False))

    def test_telegram_still_priority(self):
        add(self.db, 50)
        pacer = RetryPacer()
        job, kind = choose_next_job(self.db, 0, interactive=True,
                                    priority=(900, 'https://review.id'), pacer=pacer,
                                    now_utc=NOW, now_monotonic=3., max_attempts=3)
        self.assertEqual((job[0], kind), (900, 'telegram'))


class TriageReadOnlyTests(unittest.TestCase):
    def test_snapshot_has_categories_without_network_or_writes(self):
        script = ROOT / 'tools' / 'bima_dns_retry_triage.py'
        spec = importlib.util.spec_from_file_location('bima_dns_retry_triage_test', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname) / 'sample.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, '
                           'attempts INTEGER, error_category TEXT, '
                           'dns_result TEXT, next_retry_at TEXT)')
                db.executemany(
                    'INSERT INTO jobs VALUES (?,?,?,?,?,?)', [
                        (1, 'pending', 0, None, None, None),
                        (2, 'retry_http', 1, 'dns_error', 'no_address', NOW),
                        (3, 'retry_http', 1, 'timeout', 'none', None),
                        (4, 'retry_http', 2, 'dns_error', 'temporary_failure', '2026-11-23T00:00:00Z')
                    ])
                db.commit()
            report = module.snapshot(str(path), now_utc=NOW)
            self.assertEqual(report['statuses']['pending'], 1)
            self.assertEqual(report['statuses']['retry_http'], 3)
            by_key = {(r['category'], r['dns']): r for r in report['retries']}
            self.assertEqual(by_key['dns_error', 'no_address']['due_now'], 1)
            self.assertEqual(by_key['timeout', 'none']['no_deadline'], 1)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 4)


if __name__ == '__main__':
    unittest.main()
