"""Read-only sampling and honest evaluation regression tests."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from abuse_eval_pack import (open_database_ro, sample_database, save_new_private_json,
                             evaluate_labeled, read_labels, predicted_positive)


class EvalPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'prod.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT, status TEXT, risk TEXT, updated_at TEXT)')
            for i in range(1, 81):
                if i <= 20:
                    status, risk = 'alerted', 'high_risk'
                elif i <= 60:
                    status, risk = 'complete', 'low_risk'
                elif i <= 70:
                    status, risk = 'inconclusive', None
                else:
                    status, risk = 'retry_http', None
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                           (i, f'https://secret{i}.id/path?t=key', status, risk, '2026-11-21'))
            db.commit()

    def test_reservoir_includes_all_strata(self):
        with closing(open_database_ro(self.db)) as db:
            manifest = sample_database(db,seed=5, alert=5,complete=5,uncertain=5)
        self.assertEqual(manifest['eligible_counts'], {'alert':20,'complete':40,'uncertain':20})
        self.assertEqual(manifest['sample_counts'],{'alert':5,'complete':5,'uncertain':5})
        self.assertTrue(all('url' not in s for s in manifest['samples']))

    def test_repeat_selection_is_deterministic(self):
        with closing(open_database_ro(self.db)) as db:
            a = sample_database(db,seed=17,alert=8,complete=8,uncertain=8)
            b = sample_database(db,seed=17,alert=8,complete=8,uncertain=8)
        self.assertEqual([x['job_id'] for x in a['samples']],
                         [x['job_id'] for x in b['samples']])

    def test_db_is_still_unchanged(self):
        before = self.db.read_bytes()
        with closing(open_database_ro(self.db)) as db:
            sample_database(db, seed=1, alert=3,complete=3,uncertain=3)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE jobs SET status='pending'")
        self.assertEqual(before,self.db.read_bytes())

    def test_missing_db_must_not_create(self):
        path = Path(self.temp.name)/'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):
            open_database_ro(path)
        self.assertFalse(path.exists())

    def test_safe_output_exclusive_and_private(self):
        path = Path(self.temp.name)/'sample.json'
        save_new_private_json(path,{'samples':[]})
        self.assertEqual(json.loads(path.read_text()), {'samples':[]})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            save_new_private_json(path,{'samples':[]})

    def test_no_negative_assumption_for_missing_risk(self):
        self.assertIsNone(predicted_positive('complete', None))
        self.assertIsNone(predicted_positive('retry_http','high_risk'))
        self.assertIs(predicted_positive('complete','low_risk'),False)

    def test_partial_human_review_metrics_labeled_only(self):
        with closing(open_database_ro(self.db)) as db:
            data = sample_database(db, seed=2,alert=3,complete=3,uncertain=3)
        rows=data['samples']
        pos=next(s for s in rows if s['predicted_gambling'] is True)
        neg=next(s for s in rows if s['predicted_gambling'] is False)
        unc=next(s for s in rows if s['predicted_gambling'] is None)
        report=evaluate_labeled(data,{pos['job_id']:'gambling',
                                      neg['job_id']:'gambling',
                                      unc['job_id']:'uncertain'})
        self.assertEqual(report['confusion_matrix_on_scorable_reviews_only']['tp'], 1)
        self.assertEqual(report['confusion_matrix_on_scorable_reviews_only']['fn'], 1)
        self.assertEqual(report['not_scorable'],1)
        self.assertEqual(report['unreviewed'],len(rows)-3)

    def test_unknown_case_labels_rejected(self):
        with closing(open_database_ro(self.db)) as db:
            data=sample_database(db, alert=1,complete=1,uncertain=1)
        with self.assertRaises(ValueError):
            evaluate_labeled(data,{100000:'gambling'})

    def test_no_phishing_model_metric_claim(self):
        with closing(open_database_ro(self.db)) as db:
            data = sample_database(db,alert=1,complete=1,uncertain=1)
        self.assertEqual(data['classification_scope'],'legacy_illegal_gambling')

    def test_duplicate_labels_rejected(self):
        path=Path(self.temp.name)/'labels.csv'
        path.write_text('job_id,label\n1,gambling\n1,non_gambling\n')
        with self.assertRaises(ValueError):
            read_labels(path)


if __name__ == '__main__':
    unittest.main()
