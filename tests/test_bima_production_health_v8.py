"""Offline regression tests for read-only BIMA Phase-1 health checks."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'tools' / 'bima_production_health_v8.py'
spec = importlib.util.spec_from_file_location('bima_production_health_v8', FILE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class ProductionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.nfs = root / 'nfs'
        self.nfs.mkdir()
        self.db = self.nfs / 'crawler_production.sqlite3'
        self.dataset = self.nfs / 'urls.json'
        self.dataset.write_bytes(b'{"urls": ["https://example.id"]}')
        self.model = self.nfs / 'model'
        self.model.mkdir()
        (self.model / 'config.json').write_text('{}')
        self.mkdb()
        self.backups = self.nfs / 'backups'
        b = self.backups / 'cycle-a'
        b.mkdir(parents=True)
        (b / 'crawler_production.sqlite3').write_bytes(b'fixture')
        (b / '.bima-registry-rollover-v3.json').write_text(
            json.dumps({'format': 'bima-registry-rollover-v3', 'cycle': 'cycle-a'}))
        self.args = type('Config', (), {
            'db': str(self.db), 'input': str(self.dataset),
            'crawler_script': '/local/crawler_multi.py',
            'nfs_root': str(self.nfs), 'model': str(self.model),
            'backup_root': str(self.backups),
            'min_root_free_gib': 0, 'min_nfs_free_gib': 0,
            'verify_input_sha': False,
        })()

    def tearDown(self):
        self.temp.cleanup()

    def mkdb(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('''
            CREATE TABLE registry_snapshot_runs_v2 (
              cycle_id TEXT, url_count INTEGER, source_sha256 TEXT
            );
            CREATE TABLE registry_snapshot_active_v2 (
              singleton INTEGER,cycle_id TEXT,ready INTEGER
            );
            CREATE TABLE registry_snapshot_queue_v2 (
              job_id INTEGER,pending INTEGER,blocked INTEGER DEFAULT 0
            );
            CREATE INDEX idx_queue_pending ON registry_snapshot_queue_v2(pending);
            INSERT INTO registry_snapshot_runs_v2 VALUES ('cycle-a',3,'shaxy');
            INSERT INTO registry_snapshot_active_v2 VALUES (1,'cycle-a',1);
            INSERT INTO registry_snapshot_queue_v2 VALUES (1,1,0),(2,1,0),(3,0,0);
            ''')

    def collect(self):
        with mock.patch.object(h, 'check_mount', return_value='nfs4'), \
             mock.patch.object(h, 'service_state', return_value='active'), \
             mock.patch.object(h, 'free_gib', return_value=25), \
             mock.patch.object(h, 'crawler_pids', return_value=[123]):
            return h.collect(self.args)

    def test_health_readonly_current_cycle_and_no_sqlite_sidecar(self):
        before = self.db.stat().st_mtime_ns
        result = self.collect()
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['snapshot']['cycle'], 'cycle-a')
        self.assertEqual(result['snapshot']['total'], 3)
        self.assertEqual(result['snapshot']['pending'], 2)
        self.assertEqual(self.db.stat().st_mtime_ns, before)
        self.assertFalse((self.db.parent / (self.db.name + '-wal')).exists())

    def test_unmounted_nfs_fails_closed_before_sqlite_access(self):
        with mock.patch.object(h, 'check_mount', return_value='ext4'), \
             mock.patch.object(h, 'service_state', return_value='active'), \
             mock.patch.object(h, 'free_gib', return_value=25), \
             mock.patch.object(h, 'database_snapshot', side_effect=AssertionError('read DB')):
            result = h.collect(self.args)
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('nfs_mount_missing', result['issues'])
        self.assertIsNone(result['snapshot'])

    def test_duplicate_crawler_processes_reported(self):
        with mock.patch.object(h, 'crawler_pids', return_value=[101, 202]), \
             mock.patch.object(h, 'check_mount', return_value='nfs4'), \
             mock.patch.object(h, 'service_state', return_value='active'), \
             mock.patch.object(h, 'free_gib', return_value=25):
            result = h.collect(self.args)
        self.assertIn('duplicate_crawler_processes', result['issues'])

    def test_snapshot_not_ready(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE registry_snapshot_active_v2 SET ready=0')
        result = self.collect()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('snapshot_not_ready', result['issues'])

    def test_wal_on_nfs_is_detected(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.commit()
        result = self.collect()
        self.assertIn('sqlite_journal_not_delete', result['issues'])

    def test_rollover_backup_retention_guard(self):
        (self.backups / 'cycle-a' / '.bima-registry-rollover-v3.json').unlink()
        result = self.collect()
        self.assertIn('rollover_backup_missing', result['issues'])
        for cycle in ('cycle-a', 'cycle-b'):
            p = self.backups / cycle
            p.mkdir(exist_ok=True)
            (p / 'crawler_production.sqlite3').write_bytes(b'fixture')
            (p / '.bima-registry-rollover-v3.json').write_text(json.dumps(
                {'format': 'bima-registry-rollover-v3', 'cycle': cycle}))
        result = self.collect()
        self.assertIn('rollover_backup_retention_over_one', result['issues'])

    def test_model_missing_is_detected_not_silently_disabled(self):
        (self.model / 'config.json').unlink()
        result = self.collect()
        self.assertIn('indobert_model_files_missing', result['issues'])

    def test_verify_dataset_sha_opt_in(self):
        self.args.verify_input_sha = True
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE registry_snapshot_runs_v2 SET source_sha256=?',
                         (h.sha256_file(self.dataset),))
        self.assertTrue(self.collect()['storage']['dataset_sha_matches_cycle'])
        self.dataset.write_text('{"urls": ["https://changed.id"]}')
        self.assertIn('dataset_sha_mismatch', self.collect()['issues'])

    def test_detect_stall_then_reset_after_progress(self):
        report = {'services': {h.SERVICES[0]: 'active'},
                  'snapshot': {'cycle': 'cycle-a', 'ready': True,
                               'total': 3, 'pending': 2}, 'issues': []}
        state = {}
        self.assertFalse(h.advance_progress(state, report, 100, 1800))
        self.assertFalse(h.advance_progress(state, report, 500, 1800))
        self.assertTrue(h.advance_progress(state, report, 2000, 1800))
        report['snapshot']['pending'] = 1
        self.assertFalse(h.advance_progress(state, report, 2001, 1800))
        self.assertFalse(h.advance_progress(state, report, 3000, 1800))

    def test_cycle_change_and_long_monitor_gap_reset_stall_timer(self):
        report = {'services': {h.SERVICES[0]: 'active'},
                  'snapshot': {'cycle': 'c1', 'ready': True,
                               'total': 3, 'pending': 2}, 'issues': []}
        state = {}
        h.advance_progress(state, report, 1000, 1800)
        report['snapshot']['cycle'] = 'c2'
        self.assertFalse(h.advance_progress(state, report, 3000, 1800))
        self.assertFalse(h.advance_progress(state, report, 10000, 1800))

    def test_notification_dedup_and_one_recovery(self):
        state = {}
        self.assertEqual(h.notice_plan(state, ['db_unreadable'])[0], None)
        k, msg = h.notice_plan(state, ['db_unreadable'])
        self.assertTrue(k.startswith('degraded:'))
        sent = []
        h.notify_once(state, k, msg, lambda chat, text: sent.append(chat), ['111', '222'])
        self.assertEqual(sent, ['111', '222'])
        self.assertIsNone(h.notice_plan(state, ['db_unreadable'])[0])
        self.assertEqual(h.notice_plan(state, [])[0], None)
        k2, msg2 = h.notice_plan(state, [])
        self.assertEqual(k2, 'healthy')
        h.notify_once(state, k2, msg2, lambda chat, text: sent.append(chat), ['111', '222'])
        self.assertEqual(sent, ['111', '222', '111', '222'])
        self.assertIsNone(h.notice_plan(state, [])[0])

    def test_partial_notice_ack_persisted_per_recipient(self):
        state = {}
        saved = []
        h.notify_once(state, 'degraded:a', 'issue', lambda chat, msg: None,
                      ['111','222'], persist=lambda cur: saved.append(list(cur['notified_to'])))
        self.assertEqual(saved, [['111'], ['111', '222']])
        self.assertEqual(state['sent_notice'], 'degraded:a')

    def test_partial_telegram_delivery_retries_only_failed_chat(self):
        state = {}
        calls = []
        def flaky(chat, msg):
            calls.append(chat)
            if chat == '222' and calls.count('222') == 1:
                raise ConnectionError('simulate network')
        h.notify_once(state, 'degraded:x', 'x', flaky, ['111', '222'])
        self.assertEqual(state['notified_to'], ['111'])
        h.notify_once(state, 'degraded:x', 'x', flaky, ['111', '222'])
        self.assertEqual(calls, ['111', '222', '222'])
        self.assertEqual(state['sent_notice'], 'degraded:x')


if __name__ == '__main__':
    unittest.main()
