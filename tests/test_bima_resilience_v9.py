"""Offline regression tests for BIMA phase 1B-1D (no production DB/NFS)."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'bima_resilience_v9.py'
SPEC = importlib.util.spec_from_file_location('bima_resilience_v9', SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'crawler_production.sqlite3'
    learning = root / 'bima_learning_v450.sqlite3'
    dataset = root / 'urls.json'
    raw = json.dumps({'urls': ['https://a.id', 'https://b.id']}).encode()
    dataset.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript('''
        CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT,status TEXT,attempts INTEGER);
        INSERT INTO jobs VALUES(1,'https://a.id','complete',1);
        INSERT INTO jobs VALUES(2,'https://b.id','pending',0);
        CREATE TABLE registry_snapshot_runs_v2(cycle_id TEXT PRIMARY KEY,source_sha256 TEXT,url_count INTEGER);
        CREATE TABLE registry_snapshot_active_v2(singleton INTEGER PRIMARY KEY,cycle_id TEXT,ready INTEGER);
        CREATE TABLE registry_snapshot_queue_v2(job_id INTEGER PRIMARY KEY,ordinal INTEGER,pending INTEGER,blocked INTEGER);
        INSERT INTO registry_snapshot_active_v2 VALUES(1,'test-cycle',1);
        INSERT INTO registry_snapshot_queue_v2 VALUES(1,1,0,0),(2,2,1,0);
        ''')
        con.execute('INSERT INTO registry_snapshot_runs_v2 VALUES(?,?,2)', ('test-cycle', digest))
    with closing(sqlite3.connect(learning)) as con, con:
        con.execute('CREATE TABLE learning(id INTEGER PRIMARY KEY)')
        con.execute('INSERT INTO learning(id) VALUES (1)')
    return db, learning, dataset


class RecoveryV9(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.state = self.base / 'state'
        self.db, self.learning, self.dataset = fixture(self.state)
        self.root = self.base / 'verified-backups'
        self.work = self.base / 'work'
        self.work.mkdir()
        self.record = self.base / 'last-rehearsal.json'
        self.original_db_sha = M.sha256(self.db)
        self.nfs_patch = patch.object(M, 'mounted_fstype', return_value='ext4')
        self.nfs_patch.start()

    def tearDown(self):
        self.nfs_patch.stop()
        self.tmp.cleanup()

    def backup(self, *, retain=0):
        return M.backup_once(db=self.db, learning=self.learning, dataset=self.dataset,
                             root=self.root, enforce_nfs=False, retain=retain)

    def test_backup_verify_and_full_rehearsal_does_not_modify_source(self):
        saved = self.backup()
        folder = Path(saved['backup_path'])
        self.assertTrue(folder.is_dir())
        self.assertEqual(saved['total'], 2)
        self.assertEqual(M.verify_backup(folder, self.root, full=True)['integrity'], 'full')
        drill = M.rehearse(backup=folder, root=self.root, work_root=self.work,
                            record=self.record, nfs=self.base / 'fake-nfs')
        self.assertTrue(drill['ok'])
        self.assertEqual(drill['integrity'], 'full')
        self.assertTrue(self.record.exists())
        self.assertFalse(list(self.work.iterdir()))
        self.assertEqual(self.original_db_sha, M.sha256(self.db))
        self.assertEqual(len(list(self.root.glob('verified-*'))), 1)

    def test_restore_copy_cannot_overwrite_and_never_touches_live_db(self):
        folder = Path(self.backup()['backup_path'])
        target = self.work / 'isolated-copy'
        restored = M.restore_to(backup=folder, root=self.root, destination=target,
                                nfs=self.base / 'fake-nfs')
        self.assertEqual(restored['total'], 2)
        self.assertTrue((target / 'crawler_production.sqlite3').exists())
        with self.assertRaises(M.SafetyError):
            M.restore_to(backup=folder, root=self.root, destination=target,
                         nfs=self.base / 'fake-nfs')
        self.assertEqual(self.original_db_sha, M.sha256(self.db))

    def test_mismatch_source_dataset_blocks_finalization(self):
        self.dataset.write_text('{"urls":["https://wrong.id"]}')
        with self.assertRaisesRegex(M.SafetyError, 'snapshot_mismatch'):
            self.backup()
        self.assertFalse(list(self.root.iterdir()))

    def test_not_ready_snapshot_blocks_backup(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE registry_snapshot_active_v2 SET ready=0')
        with self.assertRaisesRegex(M.SafetyError, 'snapshot_not_ready'):
            self.backup()

    def test_corruption_detected_by_checksum(self):
        folder = Path(self.backup()['backup_path'])
        with (folder / 'urls.json').open('ab') as f:
            f.write(b'garbage')
        with self.assertRaisesRegex(M.SafetyError, 'hash_or_size_mismatch'):
            M.verify_backup(folder, self.root)

    def test_unknown_backup_dirs_never_deleted_by_retention(self):
        first = self.backup()
        second = self.backup()
        manual = self.root / 'manual-archive'
        manual.mkdir()
        (manual / 'important.txt').write_text('do not delete')
        third = self.backup(retain=2)
        self.assertFalse(Path(first['backup_path']).exists())
        self.assertTrue(Path(second['backup_path']).exists())
        self.assertTrue(Path(third['backup_path']).exists())
        self.assertTrue((manual / 'important.txt').exists())

    def test_local_lock_rejects_overlapping_backup_runs(self):
        local = self.base / 'local-health-state'
        local.mkdir()
        with patch.object(M, 'LOCK_ROOT', local):
            with M.local_lock('verified-backup'):
                with self.assertRaisesRegex(M.SafetyError, 'already_running'):
                    with M.local_lock('verified-backup'):
                        pass

    def test_unknown_or_symlinked_backup_rejected(self):
        good = Path(self.backup()['backup_path'])
        bogus = self.root / 'verified-20000101T000000Z-abcdefff'
        bogus.symlink_to(good, target_is_directory=True)
        with self.assertRaisesRegex(M.SafetyError, 'symlink'):
            M.verify_backup(bogus, self.root)
        untrusted = self.root / 'manual'
        untrusted.mkdir()
        with self.assertRaises(M.SafetyError):
            M.verify_backup(untrusted, self.root)

    def test_nfs_missing_fails_closed_for_production_backup(self):
        self.nfs_patch.stop()
        with patch.object(M, 'mounted_fstype', return_value='overlay'):
            with self.assertRaisesRegex(M.SafetyError, 'nfs_mount_missing'):
                M.backup_once(db=self.db, learning=self.learning, dataset=self.dataset,
                              root=self.root, nfs=self.base)
        self.nfs_patch.start()

    def test_storage_audit_flags_wal_and_nfs_risk(self):
        self.nfs_patch.stop()
        with patch.object(M, 'mounted_fstype', return_value='nfs4'):
            report = M.storage_audit(db=self.db, nfs=self.base,
                                      model=self.base / 'dummy-model', local_root=self.base)
        self.nfs_patch.start()
        self.assertIn('sqlite_on_nfs_locking_risk', report['warnings'])
        self.assertIn(report['status'], ('at_risk', 'blocked'))
        self.assertEqual(report['cutover'], 'not_performed_preserve_running_cycle')
        self.assertIn('indobert_model_config_missing', report['issues'])

    def test_storage_audit_no_fallback_if_mount_missing(self):
        self.nfs_patch.stop()
        with patch.object(M, 'mounted_fstype', return_value='ext4'):
            report = M.storage_audit(db=self.db, nfs=self.base)
        self.nfs_patch.start()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('nfs_mount_missing_no_fallback', report['issues'])

    def test_readiness_requires_fresh_backup_and_restore_drill(self):
        saved = self.backup()
        folder = Path(saved['backup_path'])
        M.rehearse(backup=folder, root=self.root, work_root=self.work,
                    record=self.record, nfs=self.base / 'fake-nfs')
        health = self.base / 'health.json'
        health.write_text(json.dumps({'status': 'healthy', 'issues': []}))
        with patch.object(M, 'storage_audit', return_value={
            'status': 'at_risk', 'issues': [], 'warnings': ['sqlite_on_nfs_locking_risk']
        }):
            report = M.readiness(backup_root=self.root, record=self.record,
                                  health_json=health)
            self.assertEqual(report['status'], 'ready_with_nfs_risk')
            late = M.readiness(backup_root=self.root, record=self.record,
                                health_json=health, now=M.utcnow() + timedelta(days=10))
            self.assertEqual(late['status'], 'not_ready')
            self.assertIn('verified_backup_too_old', late['failures'])
            self.assertIn('restore_drill_missing_or_stale', late['failures'])

    def test_readiness_fails_without_phase1a_health(self):
        self.backup()
        with patch.object(M, 'storage_audit', return_value={
            'status': 'at_risk', 'issues': [], 'warnings': ['sqlite_on_nfs_locking_risk']
        }):
            with patch.object(M, '__file__', str(self.base / 'resilience.py')):
                report = M.readiness(backup_root=self.root, record=self.record)
        self.assertEqual(report['status'], 'not_ready')
        self.assertIn('phase1a_health_monitor_not_deployed', report['failures'])

    def test_readiness_detects_degraded_health(self):
        self.backup()
        health = self.base / 'degraded.json'
        health.write_text('{"status":"degraded","issues":["nfs_mount_missing"]}')
        with patch.object(M, 'storage_audit', return_value={
            'status': 'at_risk', 'issues': [], 'warnings': ['sqlite_on_nfs_locking_risk']
        }):
            report = M.readiness(backup_root=self.root, record=self.record, health_json=health)
        self.assertIn('phase1a_health_degraded', report['failures'])

    def test_policy_no_active_migration_operations_in_code(self):
        source = SCRIPT.read_text('utf-8')
        self.assertNotIn("systemctl stop", source)
        self.assertNotIn('DROP TABLE', source)
        self.assertNotIn('PRAGMA journal_mode=DELETE', source)
        self.assertNotIn('git reset --hard', source)


if __name__ == '__main__':
    unittest.main()
