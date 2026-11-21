"""Offline SQLite/pacer checks for BIMA v4.1; no browser, BIMA API or DNS."""
from __future__ import annotations

import ast
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from network_retry_queue import RetryPacer, choose_next_job, find_due_network_retry

NOW = '2026-11-21T01:00:00Z'


def database():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
       CREATE TABLE jobs (
           id INTEGER PRIMARY KEY, url TEXT, status TEXT NOT NULL,
           attempts INTEGER NOT NULL DEFAULT 0, error_category TEXT,
           next_retry_at TEXT
       );
       CREATE INDEX idx_jobs_status ON jobs(status,id);
    ''')
    return conn


def job(conn, id_, status, attempts=0, category=None, when=None):
    conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                 (id_, f'https://example{id_}.id', status, attempts, category, when))


class SelectDueTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def test_due_dns_retry(self):
        job(self.conn, 1, 'retry_http', 1, 'dns_error', '2026-11-20T23:00:00Z')
        self.assertEqual(find_due_network_retry(self.conn, NOW, 3)[0], 1)

    def test_due_timeout_retry(self):
        job(self.conn, 1, 'retry_http', 2, 'timeout', NOW)
        self.assertEqual(find_due_network_retry(self.conn, NOW, 3)[0], 1)

    def test_future_not_due(self):
        job(self.conn, 1, 'retry_http', 1, 'timeout', '2026-11-22T00:00:00Z')
        self.assertIsNone(find_due_network_retry(self.conn, NOW, 3))

    def test_null_deadline_fail_closed(self):
        job(self.conn, 1, 'retry_http', 1, 'timeout', None)
        self.assertIsNone(find_due_network_retry(self.conn, NOW, 3))

    def test_attempt_budget_and_unclassified_errors(self):
        job(self.conn, 1, 'retry_http', 3, 'dns_error', NOW)
        job(self.conn, 2, 'retry_http', 0, 'timeout', NOW)
        job(self.conn, 3, 'retry_http', 1, 'unknown', NOW)
        job(self.conn, 4, 'pending_alert', 1, 'timeout', NOW)
        self.assertIsNone(find_due_network_retry(self.conn, NOW, 3))

    def test_only_retry_http_and_lowest_eligible_id(self):
        job(self.conn, 1, 'pending', 1, 'timeout', NOW)
        job(self.conn, 5, 'retry_http', 1, 'timeout', NOW)
        job(self.conn, 3, 'retry_http', 1, 'dns_error', NOW)
        self.assertEqual(find_due_network_retry(self.conn, NOW, 3)[0], 3)

    def test_claimed_row_not_selected_again(self):
        job(self.conn, 1, 'retry_http', 1, 'dns_error', NOW)
        self.conn.commit()
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            selection = find_due_network_retry(self.conn, NOW, 3)
            self.conn.execute("UPDATE jobs SET status='http_inflight',attempts=attempts+1 WHERE id=?", (selection[0],))
        self.assertIsNone(find_due_network_retry(self.conn, NOW, 3))
        self.assertEqual(self.conn.execute('SELECT attempts FROM jobs WHERE id=1').fetchone()[0], 2)


class PacerTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            RetryPacer(every=1)
        with self.assertRaises(ValueError):
            RetryPacer(max_per_minute=0)

    def test_four_routine_then_one_retry(self):
        conn = database()
        try:
            for id_ in range(1, 13):
                job(conn, id_, 'pending')
            job(conn, 90, 'retry_http', 1, 'timeout', NOW)
            pacer = RetryPacer(every=5, max_per_minute=10)
            last_id = 0
            kinds = []
            for clock in range(5):
                selected, kind = choose_next_job(
                    conn, last_id, interactive=True, priority=None, pacer=pacer,
                    now_utc=NOW, now_monotonic=float(clock), max_attempts=3)
                kinds.append(kind)
                if kind == 'routine':
                    last_id = selected[0]
                pacer.record(kind, float(clock))
                conn.execute("UPDATE jobs SET status='http_inflight' WHERE id=?", (selected[0],))
            self.assertEqual(kinds, ['routine'] * 4 + ['retry'])
            self.assertEqual(last_id, 4)
            self.assertEqual(pacer.retries_dispatched, 1)
        finally:
            conn.close()

    def test_telegram_preempts_retry_without_affecting_quota(self):
        conn = database()
        try:
            job(conn, 10, 'pending')
            job(conn, 12, 'retry_http', 1, 'timeout', NOW)
            pacer = RetryPacer()
            for i in range(4):
                pacer.record('routine', float(i))
            chosen, kind = choose_next_job(conn, 0, interactive=True, priority=(77, 'https://telegram.id'),
                                           pacer=pacer, now_utc=NOW, now_monotonic=5.0, max_attempts=3)
            self.assertEqual((chosen[0], kind), (77, 'telegram'))
            pacer.record(kind, 5.0)
            selected, kind = choose_next_job(conn, 0, interactive=True, priority=None,
                                             pacer=pacer, now_utc=NOW, now_monotonic=6.0, max_attempts=3)
            self.assertEqual((selected[0], kind), (12, 'retry'))
        finally:
            conn.close()

    def test_rolling_cap_and_expiry(self):
        pacer = RetryPacer(every=5, max_per_minute=2)
        pacer.record('retry', 0.0)
        pacer.record('retry', 1.0)
        self.assertFalse(pacer.can_retry(59.0, routine_available=False))
        self.assertTrue(pacer.can_retry(60.0, routine_available=False))

    def test_idle_routines_do_not_block_due_retry(self):
        conn = database()
        try:
            job(conn, 5, 'retry_http', 1, 'dns_error', NOW)
            pacer = RetryPacer()
            selected, kind = choose_next_job(conn, 0, interactive=True, priority=None,
                                             pacer=pacer, now_utc=NOW, now_monotonic=3.0, max_attempts=3)
            self.assertEqual((selected[0], kind), (5, 'retry'))
        finally:
            conn.close()

    def test_default_off_preserves_normal_queue(self):
        conn = database()
        try:
            job(conn, 1, 'pending')
            job(conn, 2, 'retry_http', 1, 'timeout', NOW)
            selected, kind = choose_next_job(conn, 0, interactive=True, priority=None,
                                             pacer=None, now_utc=NOW, now_monotonic=3.0, max_attempts=3)
            self.assertEqual((selected[0], kind), (1, 'routine'))
        finally:
            conn.close()

    def test_retry_skips_pending_cursor(self):
        conn = database()
        try:
            job(conn, 1, 'pending')
            job(conn, 2, 'retry_http', 1, 'dns_error', NOW)
            pacer = RetryPacer()
            for i in range(4):
                pacer.record('routine', float(i))
            selected, kind = choose_next_job(conn, 0, interactive=True, priority=None,
                                             pacer=pacer, now_utc=NOW, now_monotonic=4.0, max_attempts=3)
            self.assertEqual((selected[0], kind), (2, 'retry'))
        finally:
            conn.close()


class IntegrationGuardTests(unittest.TestCase):
    def test_crawler_is_wired_and_parseable(self):
        path = Path(__file__).resolve().parents[1] / 'crawler_multi.py'
        if not path.exists() or path.stat().st_size < 20000:
            self.skipTest('Full crawler source is not present in this isolated test fixture')
        source = path.read_text('utf-8')
        ast.parse(source)
        for token in ('--auto-network-retry', 'choose_next_job(', 'retry_pacer.record(',
                      'auto_retry_dispatched='):
            self.assertIn(token, source)
