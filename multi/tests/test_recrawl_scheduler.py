"""Offline-only tests; no HTTP requests, Telegram, IndoBERT or BIMA calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recrawl_scheduler as scheduler

FIXED_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (FIXED_NOW - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


class ScopeTests(unittest.TestCase):
    def test_seed_root_and_subdomain(self):
        seeds = {'example.co.id'}
        self.assertTrue(scheduler.is_in_seed_inventory('https://example.co.id/', seeds))
        self.assertTrue(scheduler.is_in_seed_inventory('https://promo.example.co.id/a', seeds))

    def test_reject_offscope_and_trailing_host_spoof(self):
        seeds = {'example.co.id'}
        for url in ('https://something.com/', 'https://example.co.id.evil.id/',
                    'https://example.co.id/login?token=secret',
                    'http://127.0.0.1/', 'https://example.co.id:8080/'):
            with self.subTest(url=url):
                self.assertFalse(scheduler.is_in_seed_inventory(url, seeds))

    def test_idn_scope(self):
        seeds = {'xn--caf-dma.id'}
        self.assertTrue(scheduler.is_in_seed_inventory('https://café.id/a', seeds))


class RecrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'crawler.sqlite3'
        self.input = Path(self.temp.name) / 'urls.json'
        self.input.write_text(json.dumps({'urls': ['example.id', 'example.co.id']}))
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT, risk TEXT,
                final_url TEXT, http_ms INTEGER, browser_ms INTEGER,
                http_status INTEGER, reason TEXT, classifier_json TEXT,
                evidence_path TEXT, alert_confidence REAL, error TEXT,
                route_reason TEXT, error_category TEXT, classification_ms INTEGER,
                next_retry_at TEXT, bert_ms INTEGER, dns_result TEXT,
                dns_errno INTEGER, dns_host TEXT
            );
            CREATE TABLE proactive_redirect_observations (
                seed_hostname TEXT NOT NULL, source_url TEXT NOT NULL,
                destination_url TEXT NOT NULL, method TEXT NOT NULL,
                http_status INTEGER NOT NULL DEFAULT 0, external INTEGER NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'new',
                PRIMARY KEY(source_url,destination_url,method)
            );
        ''')
        self.conn.executemany('''INSERT INTO jobs
            (id,url,status,attempts,updated_at,risk,final_url,classifier_json)
            VALUES (?,?,?,?,?,?,?,?)''', [
            (1, 'https://example.id/', 'complete', 2, ago(200), 'low_risk',
             'https://www.example.id/?token=secret', '{"status":"low_risk"}'),
            (2, 'https://example.id/promo', 'complete', 1, ago(30), 'low_risk',
             'https://example.id/promo', '{}'),
            (3, 'https://example.id/recent', 'complete', 1, ago(1), 'low_risk', None, '{}'),
            (4, 'https://example.id/alerted', 'alerted', 1, ago(2000), 'high_risk', None, '{}'),
            (5, 'https://example.id/wait', 'pending_alert', 1, ago(2000), 'high_risk', None, '{}'),
            (6, 'https://example.com/', 'complete', 1, ago(2000), 'low_risk', None, '{}'),
            (7, 'https://promo.example.co.id/a', 'complete', 1, ago(200), 'low_risk', None, '{}'),
            (8, 'https://example.id/inflight', 'http_inflight', 1, ago(2000), 'low_risk', None, '{}'),
            (9, 'https://example.id/legacy', 'complete', 1, ago(2000), 'medium_risk', None, '{}'),
            (10, 'https://example.id/unrelated', 'complete', 1, ago(30), 'low_risk', None, '{}'),
        ])
        self.conn.execute('''INSERT INTO proactive_redirect_observations
            (seed_hostname,source_url,destination_url,method,http_status,external,first_seen,last_seen)
            VALUES (?,?,?,?,?,?,?,?)''', (
                'example.id', 'https://example.id/promo', 'https://external.example/slot',
                'http', 302, 1, ago(3), ago(3),
            ))
        self.conn.commit()

    def due(self, **options):
        return scheduler.select_due_jobs(
            self.conn, seeds={'example.id', 'example.co.id'},
            now=FIXED_NOW, regular_hours=options.get('regular_hours', 168),
            redirect_hours=options.get('redirect_hours', 24),
            scan_limit=options.get('scan_limit', 100),
        )

    def test_select_and_prioritize_new_external_redirect(self):
        due = self.due()
        self.assertEqual([x.id for x in due], [2, 1, 7])
        self.assertEqual(due[0].trigger, 'new_external_redirect')
        self.assertEqual(due[1].trigger, 'scheduled_clean_recheck')

    def test_older_nonredirect_but_too_young_not_due(self):
        self.assertNotIn(10, [x.id for x in self.due()])

    def test_alert_and_inflight_never_due(self):
        self.assertTrue({4, 5, 8, 9}.isdisjoint({x.id for x in self.due()}))

    def test_backpressure_prevents_mutation(self):
        self.conn.execute("UPDATE jobs SET status='pending' WHERE id=10")
        self.conn.commit()
        changed, pending = scheduler.apply_rechecks(
            self.conn, due=self.due(), max_rechecks=5, max_pending=1)
        self.assertEqual((changed, pending), (0, 1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM proactive_recrawl_audit').fetchone()[0], 0)

    def test_apply_guard_and_preserve_audit(self):
        changed, pending = scheduler.apply_rechecks(
            self.conn, due=self.due(), max_rechecks=2, max_pending=10)
        self.assertEqual((changed, pending), (2, 2))
        rows = self.conn.execute('''SELECT id,status,attempts,risk,classifier_json
            FROM jobs WHERE id IN (1,2) ORDER BY id''').fetchall()
        self.assertEqual(rows, [(1, 'pending', 0, None, None),
                                (2, 'pending', 0, None, None)])
        audit = self.conn.execute('''SELECT job_id,trigger,previous_final_url
            FROM proactive_recrawl_audit ORDER BY id''').fetchall()
        self.assertEqual(audit[0][0:2], (2, 'new_external_redirect'))
        self.assertEqual(audit[1], (1, 'scheduled_clean_recheck',
                                    'https://www.example.id/'))
        changed2, _ = scheduler.apply_rechecks(
            self.conn, due=self.due(), max_rechecks=10, max_pending=10)
        self.assertEqual(changed2, 1)  # only id=7 remains due

    def test_stale_selection_cannot_overwrite_new_result(self):
        selected = self.due()
        self.conn.execute("UPDATE jobs SET updated_at=? WHERE id=2", (ago(1),))
        self.conn.commit()
        changed, _ = scheduler.apply_rechecks(
            self.conn, due=[selected[0]], max_rechecks=10, max_pending=10)
        self.assertEqual(changed, 0)
        self.assertEqual(self.conn.execute('SELECT status FROM jobs WHERE id=2').fetchone()[0], 'complete')

    def test_preview_readonly_does_not_create_tables(self):
        ro = scheduler.open_jobs_db(self.path, apply=False)
        try:
            self.assertEqual(scheduler.pending_count(ro), 0)
            self.assertFalse(ro.execute("SELECT 1 FROM sqlite_master WHERE name='proactive_recrawl_audit'").fetchone())
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("UPDATE jobs SET status='pending' WHERE id=1")
        finally:
            ro.close()

    def test_apply_requires_live_acknowledgment(self):
        args = scheduler.arg_parser().parse_args([
            '--db', str(self.path), '--input', str(self.input), '--apply'])
        with self.assertRaisesRegex(ValueError, 'allow-live-rechecks'):
            scheduler.run(args)

    def test_cli_preview_readonly(self):
        args = scheduler.arg_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertEqual(args.regular_hours, 168)
        self.assertEqual(args.redirect_hours, 24)

    def test_timestamp_parser_fails_closed(self):
        self.assertIsNone(scheduler._valid_timestamp('not-a-date'))
        self.assertIsNone(scheduler._valid_timestamp('2026-11-20T12:00:00'))


if __name__ == '__main__':
    unittest.main()
