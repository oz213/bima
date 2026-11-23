"""Regression checks for snapshot-scoped metrics and one-backup retention."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))
sys.path.insert(0, str(ROOT / 'tools'))

from registry_active_metrics import current_counts
import re
_rollover = (ROOT / 'tools/bima_registry_rollover_v2.py').read_text()
MARK_CODE = re.search(r"MARK_CODE = r'''(.*?)'''", _rollover, re.S).group(1)
PRUNE_CODE = re.search(r"PRUNE_CODE = r'''(.*?)'''", _rollover, re.S).group(1)


class TestSnapshotMetrics(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,attempts INTEGER);
            INSERT INTO jobs VALUES(1,'alerted',1),(2,'pending',0),(3,'complete',1);
        """)

    def tearDown(self):
        self.db.close()

    def activate(self):
        self.db.executescript("""
            CREATE TABLE registry_snapshot_runs_v2(cycle_id TEXT,url_count INTEGER);
            CREATE TABLE registry_snapshot_active_v2(singleton INTEGER,cycle_id TEXT,ready INTEGER);
            CREATE TABLE registry_snapshot_queue_v2(job_id INTEGER PRIMARY KEY,blocked INTEGER NOT NULL DEFAULT 0);
            INSERT INTO registry_snapshot_runs_v2 VALUES('cycle-b',2);
            INSERT INTO registry_snapshot_active_v2 VALUES(1,'cycle-b',1);
            INSERT INTO registry_snapshot_queue_v2 VALUES(2,0),(3,0);
        """)

    def test_legacy_counts(self):
        counts,total,attempted,cycle = current_counts(self.db)
        self.assertEqual((total,attempted,cycle),(3,2,None))

    def test_new_dataset_excludes_old_jobs(self):
        self.activate()
        counts,total,attempted,cycle = current_counts(self.db)
        self.assertEqual((total,attempted,cycle),(2,1,'cycle-b'))
        self.assertEqual(counts,{'pending':1,'complete':1})

    def test_old_protected_alert_does_not_restart_new_progress(self):
        self.activate()
        self.db.execute("UPDATE registry_snapshot_queue_v2 SET blocked=1 WHERE job_id=3")
        counts,total,attempted,cycle=current_counts(self.db)
        self.assertEqual((total,attempted,cycle),(2,0,'cycle-b'))
        self.assertEqual(counts,{'pending':1,'protected_previous':1})

    def test_unpublished_cycle_fails_closed(self):
        self.activate()
        self.db.execute('UPDATE registry_snapshot_active_v2 SET ready=0')
        with self.assertRaises(RuntimeError):
            current_counts(self.db)

    def test_missing_snapshot_entry_fails_closed(self):
        self.activate()
        self.db.execute('UPDATE registry_snapshot_runs_v2 SET url_count=3')
        with self.assertRaises(RuntimeError):
            current_counts(self.db)


class TestRolloverRetention(unittest.TestCase):
    def test_keep_latest_and_ignore_unmanaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('registry-old','registry-new','manual'):
                folder=root/name
                folder.mkdir()
                (folder/'crawler_production.sqlite3').write_bytes(b'fixture')
            for name in ('registry-old','registry-new'):
                subprocess.run([sys.executable,'-c',MARK_CODE,str(root/name),name],check=True)
            subprocess.run([sys.executable,'-c',PRUNE_CODE,str(root),'registry-new'],check=True)
            self.assertFalse((root/'registry-old').exists())
            self.assertTrue((root/'registry-new').is_dir())
            self.assertTrue((root/'manual').is_dir())


if __name__ == '__main__':
    unittest.main()
