"""Offline Registry snapshot lifecycle tests; synthetic SQLite only, no production."""
import json
import importlib.util
import contextlib
import io
from unittest import mock
import sqlite3
import tempfile
import unittest
from pathlib import Path

from registry_snapshot_v2 import (active_cycle, active_ready, apply_snapshot,
                                  cursor_for_job, find_pending_registry, mark_ready,
                                  read_snapshot)
from discovery_priority import find_next_registry_job
from network_retry_queue import find_due_network_retry


def db_memory():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY,url TEXT NOT NULL UNIQUE,status TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0,http_ms INTEGER,browser_ms INTEGER,http_status INTEGER,
          reason TEXT,risk TEXT,classifier_json TEXT,final_url TEXT,evidence_path TEXT,
          alert_confidence REAL,error TEXT,route_reason TEXT,error_category TEXT,
          classification_ms INTEGER,next_retry_at TEXT,bert_ms INTEGER,
          dns_result TEXT,dns_errno INTEGER,dns_host TEXT,updated_at TEXT);
        CREATE TABLE telegram_checks(job_id INTEGER);
        CREATE TABLE bima_prealerts(job_id INTEGER);
        CREATE TABLE discovery_job_priority(job_id INTEGER PRIMARY KEY,discovered_at TEXT);
        CREATE INDEX idx_jobs_status ON jobs(status,id);
    ''')
    return db


def snap(urls, digest='hash1'):
    return {'sha256': digest, 'urls': tuple(urls), 'invalid': 0}


def insert(db, ident, status='pending', *, conf='old'):
    db.execute('INSERT INTO jobs(id,url,status,attempts,classifier_json) VALUES(?,?,?,?,?)',
               (ident, 'http://domain%s.id' % ident, status, 2, conf))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.db = db_memory()
        for ident, status in [(1, 'complete'), (2, 'inconclusive'), (3, 'alerted'),
                              (4, 'pending_alert'), (5, 'retry_http'), (6, 'pending'),
                              (7, 'complete')]:
            insert(self.db, ident, status)
        self.db.execute('INSERT INTO bima_prealerts VALUES (7)')
        self.db.execute('INSERT INTO telegram_checks VALUES (2)')
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_apply_resets_only_unprotected_and_preserves_job_ids(self):
        urls = ['http://domain3.id', 'http://domain1.id', 'http://domain2.id',
                'http://domain4.id', 'http://domain5.id', 'http://domain6.id',
                'http://domain7.id', 'http://new.id']
        result = apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap(urls))
        self.assertEqual(result['requeued'], 2)
        self.assertEqual(result['active_pending'], 4)  # 1,5,6,new
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0], 'pending')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=2').fetchone()[0], 'inconclusive')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=3').fetchone()[0], 'alerted')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=4').fetchone()[0], 'pending_alert')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=7').fetchone()[0], 'complete')
        self.assertEqual(self.db.execute('SELECT classifier_json FROM jobs WHERE id=1').fetchone()[0], None)
        self.assertEqual(self.db.execute('SELECT classifier_json FROM registry_snapshot_job_history_v2 '
                                         'WHERE cycle_id=? AND job_id=?', ('cycle-001', 1)).fetchone()[0], 'old')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0], 1)

    def test_follows_snapshot_order_and_status_trigger(self):
        apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap([
            'http://domain6.id', 'http://domain1.id', 'http://domain5.id']))
        self.assertFalse(active_ready(self.db))
        self.assertIsNone(find_next_registry_job(self.db, 0, wrap=True))
        mark_ready(self.db, 'cycle-001', 'hash1')
        picked = find_next_registry_job(self.db, 0, wrap=True)
        self.assertEqual(picked[0], 6)
        self.assertEqual(cursor_for_job(self.db, 6), 1)
        self.db.execute("UPDATE jobs SET status='http_inflight' WHERE id=6")
        self.assertEqual(find_next_registry_job(self.db, 0, wrap=True)[0], 1)
        self.assertEqual(find_next_registry_job(self.db, 2, wrap=True)[0], 5)
        self.assertEqual(find_next_registry_job(self.db, 3, wrap=True)[0], 1)
        self.db.execute("UPDATE jobs SET status='pending' WHERE id=6")
        self.assertEqual(find_next_registry_job(self.db, 3, wrap=True)[0], 6)

    def test_new_cycle_replaces_active_membership_not_history(self):
        apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap([
            'http://domain1.id', 'http://domain5.id']))
        self.db.execute("UPDATE jobs SET status='complete' WHERE id=1")
        self.db.commit()
        apply_snapshot(self.db, cycle_id='cycle-002', snapshot=snap([
            'http://domain2.id', 'http://domain6.id'], 'hash2'))
        self.assertEqual(active_cycle(self.db), 'cycle-002')
        self.assertNotIn(1, [r[0] for r in self.db.execute(
            'SELECT job_id FROM registry_snapshot_queue_v2')])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM registry_snapshot_runs_v2').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM registry_snapshot_job_history_v2').fetchone()[0], 2)

    def test_same_cycle_is_noop_conflict_rejected(self):
        s = snap(['http://domain1.id'])
        apply_snapshot(self.db, cycle_id='cycle-001', snapshot=s)
        self.assertEqual(apply_snapshot(self.db, cycle_id='cycle-001', snapshot=s)['status'],
                         'already_active')
        with self.assertRaisesRegex(ValueError, 'previously used'):
            apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap(['http://new.id'], 'changed'))
        self.assertEqual(active_cycle(self.db), 'cycle-001')

    def test_legacy_fallback_before_first_cycle(self):
        self.assertIsNone(active_cycle(self.db))
        self.assertEqual(find_next_registry_job(self.db, 0, wrap=True)[0], 6)
        self.assertEqual(cursor_for_job(self.db, 5), 5)

    def test_invalid_cycle_rolls_back(self):
        with self.assertRaises(ValueError):
            apply_snapshot(self.db, cycle_id='../outside', snapshot=snap(['http://domain1.id']))
        self.assertIsNone(active_cycle(self.db))

    def test_discovery_stays_separate(self):
        self.db.execute('INSERT INTO discovery_job_priority VALUES(6,?)', ('2026-11-22T00:00:00Z',))
        self.db.commit()
        apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap([
            'http://domain6.id', 'http://domain1.id']))
        mark_ready(self.db, 'cycle-001', 'hash1')
        self.assertEqual(find_next_registry_job(self.db, 0, wrap=True)[0], 1)

    def test_cross_scheme_alert_hostname_not_recrawled_automatically(self):
        self.db.execute('INSERT INTO jobs(url,status) VALUES (?,?)',
                        ('https://same.id', 'alerted'))
        self.db.commit()
        result = apply_snapshot(self.db, cycle_id='cycle-001',
                                snapshot=snap(['http://same.id', 'http://domain1.id']))
        self.assertEqual(result['protected_hosts'], 1)
        row = self.db.execute('SELECT j.status,q.pending,q.blocked FROM jobs j '
                              'JOIN registry_snapshot_queue_v2 q ON q.job_id=j.id '
                              'WHERE j.url=?', ('http://same.id',)).fetchone()
        self.assertEqual(row, ('pending', 0, 1))
        self.db.execute("UPDATE jobs SET status='pending' WHERE url='http://same.id'")
        self.assertEqual(self.db.execute('SELECT pending FROM registry_snapshot_queue_v2 q '
                                         'JOIN jobs j ON j.id=q.job_id WHERE j.url=?',
                                         ('http://same.id',)).fetchone()[0], 0)
        mark_ready(self.db, 'cycle-001', 'hash1')
        self.assertEqual(find_next_registry_job(self.db, 0, wrap=True)[0], 1)

    def test_old_cycle_retries_do_not_consume_new_cycle_quota(self):
        apply_snapshot(self.db, cycle_id='cycle-001',
                       snapshot=snap(['http://domain1.id']))
        self.db.execute("UPDATE jobs SET status='retry_http',attempts=1, "
                        "error_category='dns_error',next_retry_at='2026-11-20T00:00:00Z' "
                        "WHERE id=5")  # stale Registry retry outside latest snapshot
        self.db.execute("UPDATE jobs SET status='retry_http',attempts=1, "
                        "error_category='dns_error',next_retry_at='2026-11-22T00:00:00Z' "
                        "WHERE id=1")
        now = '2026-11-22T12:00:00Z'
        self.assertIsNone(find_due_network_retry(self.db, now, 3))  # not published
        mark_ready(self.db, 'cycle-001', 'hash1')
        self.assertEqual(find_due_network_retry(self.db, now, 3)[0], 1)
        self.db.execute("UPDATE jobs SET status='complete' WHERE id=1")
        self.assertIsNone(find_due_network_retry(self.db, now, 3))

    def test_discovery_and_telegram_retries_survive_snapshot_cutover(self):
        apply_snapshot(self.db, cycle_id='cycle-001',
                       snapshot=snap(['http://domain1.id']))
        self.db.execute('INSERT INTO discovery_job_priority VALUES(5,?)',
                        ('2026-11-22T00:00:00Z',))
        self.db.execute("UPDATE jobs SET status='retry_http',attempts=1, "
                        "error_category='timeout',next_retry_at='2026-11-21T00:00:00Z' "
                        "WHERE id IN (2,5)")
        now = '2026-11-22T12:00:00Z'
        self.assertEqual(find_due_network_retry(self.db, now, 3)[0], 2)
        self.db.execute("UPDATE jobs SET status='complete' WHERE id=2")
        self.assertEqual(find_due_network_retry(self.db, now, 3)[0], 5)

    def test_rejects_accidental_major_snapshot_shrink(self):
        first = ['http://domain%s.id' % i for i in range(1, 8)]
        apply_snapshot(self.db, cycle_id='cycle-001', snapshot=snap(first))
        with self.assertRaisesRegex(ValueError, 'over 50%'):
            apply_snapshot(self.db, cycle_id='cycle-002',
                           snapshot=snap(['http://domain1.id'], digest='newhash'))
        self.assertEqual(active_cycle(self.db), 'cycle-001')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2').fetchone()[0], 7)

    def test_mid_apply_failure_rolls_back_all_status_changes(self):
        self.db.executescript('''
            CREATE TRIGGER block_new_pending BEFORE UPDATE OF status ON jobs
            WHEN NEW.status='pending' AND OLD.status='complete'
            BEGIN SELECT RAISE(FAIL, 'synthetic_failure'); END;
        ''')
        before = self.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            apply_snapshot(self.db, cycle_id='cycle-001',
                           snapshot=snap(['http://domain1.id']))
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0], before)
        self.assertIsNone(active_cycle(self.db))

    def test_registry_queue_uses_partial_index_not_full_jobs_scan(self):
        apply_snapshot(self.db, cycle_id='cycle-001',
                       snapshot=snap(['http://domain1.id', 'http://domain6.id']))
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT j.id,j.url FROM registry_snapshot_queue_v2 q "
            "INDEXED BY idx_registry_snapshot_pending_v2 "
            "CROSS JOIN jobs j ON j.id=q.job_id "
            "WHERE q.pending=1 AND q.ordinal>? AND j.status='pending' "
            "ORDER BY q.ordinal LIMIT 1", (0,)).fetchall()
        description = ' '.join(r[3] for r in plan)
        self.assertIn('idx_registry_snapshot_pending_v2', description)
        self.assertNotIn('TEMP B-TREE', description)

    def test_cli_file_swap_failure_blocks_dispatch_until_retried(self):
        script = Path(__file__).resolve().parents[2] / 'tools' / 'bima_registry_snapshot_v2.py'
        if not script.is_file():
            self.skipTest('Operator script missing in lightweight fixture')
        spec = importlib.util.spec_from_file_location('bima_registry_snapshot_tool_test', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = root / 'prod.sqlite3'
            dest = root / 'urls.json'
            staged = root / 'urls.json.next'
            dest.write_text(json.dumps({'urls': ['http://old.id']}))
            staged.write_text(json.dumps({'urls': ['http://domain1.id', 'http://domain6.id']}))
            with sqlite3.connect(db_path) as file_db:
                file_db.executescript('\n'.join(self.db.iterdump()))
            args = ['apply', '--input', str(dest), '--source', str(staged),
                    '--db', str(db_path), '--cycle', 'cycle-001', '--min-urls', '1']
            with mock.patch.object(module, 'require_stopped'):
                with mock.patch.object(module, 'atomic_copy', side_effect=OSError('disk swap failed')):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(module.main(args), 1)
                with sqlite3.connect(db_path) as file_db:
                    self.assertEqual(active_cycle(file_db), 'cycle-001')
                    self.assertFalse(active_ready(file_db))
                    self.assertIsNone(find_next_registry_job(file_db, 0, wrap=True))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(args), 0)  # same cycle finishes publication
                with sqlite3.connect(db_path) as file_db:
                    self.assertTrue(active_ready(file_db))
                    self.assertEqual(find_next_registry_job(file_db, 0, wrap=True)[0], 1)
            self.assertEqual(json.loads(dest.read_text())['urls'],
                             ['http://domain1.id', 'http://domain6.id'])

    def test_input_read_matches_crawler_normalizer(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'new.json'
            p.write_text(json.dumps({'urls': ['http://alpha.id', 'http://alpha.id',
                                              'http://beta.my.id', 'https://invalid.com']}))
            data = read_snapshot(p)
            self.assertEqual(data['urls'], ('http://alpha.id', 'http://beta.my.id'))
            self.assertEqual(data['invalid'], 1)


if __name__ == '__main__':
    unittest.main()
