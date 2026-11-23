"""Regression: every URL is rescanned on new and existing BIMA cycles."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'multi'))

from registry_snapshot_v2 import (apply_snapshot, mark_ready,
                                  requeue_blocked_current_cycle)


def build_db():
    db = sqlite3.connect(':memory:')
    db.executescript("""
        CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT, http_ms INTEGER, browser_ms INTEGER,
            http_status INTEGER, reason TEXT, risk TEXT, classifier_json TEXT,
            final_url TEXT, evidence_path TEXT, alert_confidence REAL,
            error TEXT, route_reason TEXT, error_category TEXT,
            classification_ms INTEGER, next_retry_at TEXT, bert_ms INTEGER,
            dns_result TEXT, dns_errno INTEGER, dns_host TEXT
        );
        INSERT INTO jobs (id,url,status,attempts,reason) VALUES
            (1,'https://alert.id','alerted',2,'bima_alert_sent'),
            (2,'https://pending-alert.id','pending_alert',1,'send_unknown'),
            (3,'https://review.id','review_approved',1,'accepted'),
            (4,'https://completed.id','complete',1,'clean'),
            (5,'https://new.id','pending',0,NULL);
        CREATE TABLE telegram_checks (
            id INTEGER PRIMARY KEY,job_id INTEGER, result TEXT
        );
        INSERT INTO telegram_checks VALUES(1,3,'review_approved');
        CREATE TABLE bima_prealerts (
            id INTEGER PRIMARY KEY,job_id INTEGER,delivered INTEGER
        );
        INSERT INTO bima_prealerts VALUES(1,1,1);
    """)
    return db


def snapshot(cycle):
    return {'urls': ('https://alert.id','https://pending-alert.id',
                     'https://review.id','https://completed.id',
                     'https://new.id'), 'sha256': cycle, 'invalid': 0}


class RecrawlAll(unittest.TestCase):
    def test_next_rollover_requeues_all_and_preserves_history(self):
        db = build_db()
        try:
            result = apply_snapshot(db, cycle_id='new-cycle', snapshot=snapshot('sha-1'))
            self.assertEqual(result['active_pending'], 5)
            self.assertEqual(result['protected_hosts'], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2 WHERE blocked=1').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2 WHERE pending=1').fetchone()[0],5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending' AND attempts=0").fetchone()[0],5)
            old = dict(db.execute('SELECT job_id,previous_status FROM registry_snapshot_job_history_v2'))
            self.assertEqual(old,{1:'alerted',2:'pending_alert',3:'review_approved',4:'complete'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_checks').fetchone()[0],1)
            mark_ready(db,'new-cycle','sha-1')
            self.assertEqual(db.execute('SELECT ready FROM registry_snapshot_active_v2').fetchone()[0],1)
        finally:
            db.close()

    def test_live_blocked_626_can_be_requeued_without_replacing_dataset(self):
        db = build_db()
        try:
            apply_snapshot(db, cycle_id='current-cycle', snapshot=snapshot('sha-2'))
            mark_ready(db,'current-cycle','sha-2')
            db.execute("UPDATE jobs SET status='alerted',attempts=1 WHERE id=1")
            db.execute("UPDATE jobs SET status='review_approved',attempts=1 WHERE id=3")
            db.execute('UPDATE registry_snapshot_queue_v2 SET blocked=1,pending=0 WHERE job_id IN (1,3)')
            db.commit()
            result = requeue_blocked_current_cycle(db,expected_cycle='current-cycle')
            self.assertEqual(result['requeued'],2)
            self.assertEqual(result['remaining_blocked'],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registry_snapshot_queue_v2 WHERE pending=1').fetchone()[0],5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending' AND attempts=0").fetchone()[0],5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_checks').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0],1)
            self.assertEqual(requeue_blocked_current_cycle(db, expected_cycle='current-cycle')['requeued'],0)
        finally:
            db.close()

    def test_requeue_rejects_wrong_cycle_without_mutations(self):
        db = build_db()
        try:
            apply_snapshot(db, cycle_id='current-cycle', snapshot=snapshot('sha-3'))
            mark_ready(db,'current-cycle','sha-3')
            db.execute('UPDATE registry_snapshot_queue_v2 SET blocked=1,pending=0 WHERE job_id=1')
            db.commit()
            with self.assertRaises(ValueError):
                requeue_blocked_current_cycle(db, expected_cycle='old-cycle')
            self.assertEqual(db.execute('SELECT blocked FROM registry_snapshot_queue_v2 WHERE job_id=1').fetchone()[0],1)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
