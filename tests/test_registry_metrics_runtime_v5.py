"""Regression: Telegram metrics after dataset rollover and NFS path resolution."""
import argparse
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
sys.path.insert(0, str(ROOT / 'tools'))

import telegram_monitor
import telegram_periodic
from bima_registry_rollover_v2 import Rollover


class TestTelegramCurrentCycle(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, url TEXT, status TEXT,
                attempts INTEGER, reason TEXT, risk TEXT, dns_result TEXT,
                updated_at TEXT
            );
            INSERT INTO jobs VALUES
              (1,'https://old.id','alerted',1,'old alert','high','none','2026-01-01'),
              (2,'https://new.id','pending',0,NULL,NULL,NULL,'2026-11-23'),
              (3,'https://done.id','complete',1,'ok','low','none','2026-11-23'),
              (4,'https://retired.id','retry_http',2,'DNS','unknown','not_found','2026-01-01');
            CREATE TABLE registry_snapshot_runs_v2 (cycle_id TEXT, url_count INTEGER);
            CREATE TABLE registry_snapshot_active_v2 (
                singleton INTEGER, cycle_id TEXT, ready INTEGER
            );
            CREATE TABLE registry_snapshot_queue_v2 (
                job_id INTEGER PRIMARY KEY, blocked INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO registry_snapshot_runs_v2 VALUES('cycle-new', 3);
            INSERT INTO registry_snapshot_active_v2 VALUES(1,'cycle-new',1);
            INSERT INTO registry_snapshot_queue_v2 VALUES(1,1),(2,0),(3,0);
        """)

    def tearDown(self):
        self.conn.close()

    def test_status_starts_fresh_and_only_counts_new_cycle(self):
        report = telegram_monitor.status_report(self.conn)
        self.assertIn('Total URL       : 3', report)
        self.assertIn('Sudah dicoba    : 1', report)
        self.assertIn('Selesai         : 1', report)
        self.assertIn('Retry HTTP/DNS  : 0', report)
        self.assertIn('Alert BIMA OK   : 0', report)
        self.assertIn('Dilindungi lama : 1', report)

    def test_inconclusive_and_dns_do_not_leak_old_jobs(self):
        self.assertIn('0 URL', telegram_monitor.inconclusive_report(self.conn))
        self.assertIn('Tidak ada URL berstatus retry_http',
                      telegram_monitor.dns_report(self.conn))

    def test_periodic_report_scoped_to_new_cycle(self):
        report, processed = telegram_periodic.render_report(self.conn, previous=None)
        self.assertEqual(processed, 1)
        self.assertIn('1 / 3', report)
        self.assertIn('Dilindungi riwayat: 1', report)
        self.assertIn('Retry: 0', report)
        self.assertNotIn('retired.id', report)
        self.assertNotIn('old.id', report)


class TestNfsResolution(unittest.TestCase):
    def test_rollover_defaults_to_nfs_dataset_target(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            app = root / 'app'
            data = root / 'datasets'
            app.mkdir()
            data.mkdir()
            (data / 'urls.json').write_text('[]')
            (app / 'urls.json').symlink_to(data / 'urls.json')
            args = argparse.Namespace(
                app=app, db=None, learning_db=None, source=None,
                input=None, backup_root=root / 'backups'
            )
            rollover = Rollover(args)
            self.assertEqual(rollover.target.resolve(), (data / 'urls.json').resolve())
            self.assertEqual(rollover.source.resolve(), (data / 'urls.json.next').resolve())


if __name__ == '__main__':
    unittest.main()
