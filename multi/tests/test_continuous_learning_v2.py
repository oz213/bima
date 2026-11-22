"""Human-verdict provenance and offline candidate tests; no network or production writes."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from learning import continuous_v2 as v2
from learning import core


class LearningV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.production = self.root / 'production.sqlite3'
        self.learning = self.root / 'learning.sqlite3'
        with closing(sqlite3.connect(self.production)) as db, db:
            db.executescript('''
                CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT, status TEXT);
                CREATE TABLE bima_prealerts (id INTEGER PRIMARY KEY, job_id INTEGER,
                 url TEXT, feedback_label TEXT,reviewer_id TEXT,reviewed_at TEXT);
            ''')
            db.execute('INSERT INTO jobs VALUES (1,?,?)', ('https://judol.id', 'review_approved'))
            db.execute('INSERT INTO jobs VALUES (2,?,?)', ('https://aman.id', 'review_rejected'))
            db.execute('INSERT INTO bima_prealerts VALUES(1,1,?,?,?,?)',
                       ('https://judol.id', 'gambling', '12', '2026-11-22T01:00:00Z'))
            db.execute('INSERT INTO bima_prealerts VALUES(2,2,?,?,?,?)',
                       ('https://aman.id', 'non_gambling', '12', '2026-11-22T01:00:00Z'))

    def test_import_site_only_and_idempotent(self):
        with closing(sqlite3.connect(self.production)) as db:
            before = db.execute('SELECT * FROM jobs').fetchall()
        self.assertEqual(v2.sync_prealerts(self.production, self.learning)['imported'], 2)
        result = v2.stats(self.learning)
        self.assertEqual(result['v2_text_labels'], {'unlabeled': 2})
        self.assertEqual(v2.sync_prealerts(self.production, self.learning)['already_present'], 2)
        uid = result['v2_cases'][0]['source_uid']
        with core.learning_connection(self.learning) as db:
            self.assertIsNone(db.execute('SELECT text_label FROM learning_observations_v2 '
                                         'WHERE source_uid=?', (uid,)).fetchone()[0])
        with closing(sqlite3.connect(self.production)) as db:
            self.assertEqual(db.execute('SELECT * FROM jobs').fetchall(), before)

    def test_explicit_text_gate_and_immutable_review(self):
        v2.sync_prealerts(self.production, self.learning)
        uid = v2.stats(self.learning)['v2_cases'][0]['source_uid']
        with self.assertRaises(ValueError):
            v2.label_text(self.learning, uid, 'gambling', 'reviewer', 'Verified')
        self.assertEqual(v2.attach_text(self.learning, uid, 'Promo kasino taruhan lengkap. '*5,
                                         'reviewer')['status'], 'attached')
        self.assertEqual(v2.label_text(self.learning, uid, 'gambling', 'reviewer',
                                       'Verified text')['status'], 'text_labeled')
        self.assertEqual(v2.label_text(self.learning, uid, 'gambling', 'reviewer',
                                       'Verified text')['status'], 'already_labeled')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            v2.label_text(self.learning, uid, 'non_gambling', 'reviewer', 'New label')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            v2.attach_text(self.learning, uid, 'Replaced evidence text.' * 10, 'reviewer')

    def test_new_cycle_manual_observation_is_distinct(self):
        v2.sync_prealerts(self.production, self.learning)
        before = len(v2.stats(self.learning)['v2_cases'])
        res = v2.add_manual(self.production, self.learning,
                            'legacy-before-snapshots', 1, 'true_positive',
                            'Kasino taruhan deposit dana permainan. ' * 3,
                            'gambling', 'reviewer', 'Reviewed updated content')
        self.assertEqual(res['status'], 'reviewed')
        self.assertEqual(len(v2.stats(self.learning)['v2_cases']), before + 1)
        self.assertEqual(v2.add_manual(self.production, self.learning,
                                       'legacy-before-snapshots', 1, 'true_positive',
                                       'Kasino taruhan deposit dana permainan. ' * 3,
                                       'gambling', 'reviewer', 'Reviewed updated content')['status'],
                         'already_present')

    def test_learner_waits_for_verified_labels(self):
        v2.sync_prealerts(self.production, self.learning)
        result = v2.learn(self.learning, self.root / 'runs')
        self.assertEqual(result['status'], 'waiting_for_labels')
        self.assertEqual(result['unique_verified'], 0)

    def test_candidate_is_offline_and_never_activates(self):
        with core.learning_connection(self.learning) as db, db:
            db.executescript(v2.V2_SCHEMA)
            for i in range(1, 201):
                label = 'gambling' if i % 2 else 'non_gambling'
                text = ('promosi taruhan kasino hadiah ' if i % 2 else
                        'artikel sekolah guru belajar ') * 5 + str(i)
                db.execute('INSERT INTO learning_observations_v2 '
                           '(source_uid,cycle_id,source_kind,source_job_id,hostname,domain_group,'
                           'site_label,text_label,evidence_text,evidence_sha256,evidence_origin,'
                           'reviewer,reviewed_at,text_reviewer,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (str(i).zfill(64), 'cycle-001', 'manual', i, 'host%d.id'%i,
                            'host%d.id'%i, 'true_positive' if i%2 else 'false_positive',
                            label, text, core.sha256_text(text), 'manual_capture',
                            'analyst', '2026-11-22T00:00:00Z', 'analyst', 'now', 'now'))
        report = v2.learn(self.learning, self.root / 'runs')
        self.assertEqual(report['status'], 'candidate_ready')
        self.assertFalse(report['production_activated'])
        self.assertTrue((Path(report['run']) / 'model.json').exists())
        self.assertEqual(v2.learn(self.learning, self.root / 'runs')['status'], 'up_to_date')

    def test_conflicting_evidence_labels_do_not_train(self):
        with core.learning_connection(self.learning) as db, db:
            db.executescript(v2.V2_SCHEMA)
            for i,label in enumerate(('gambling','non_gambling')):
                text = 'sama bukti satu halaman ' * 5
                db.execute('INSERT INTO learning_observations_v2 '
                           '(source_uid,cycle_id,source_kind,source_job_id,hostname,domain_group,'
                           'site_label,text_label,evidence_text,evidence_sha256,evidence_origin,'
                           'reviewer,reviewed_at,text_reviewer,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (str(i).zfill(64), 'cycle-001', 'manual', i, 'x.id', 'x.id',
                            'true_positive', label, text, core.sha256_text(text),
                            'manual_capture', 'reviewer', 'now', 'reviewer', 'now', 'now'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            v2.learn(self.learning, self.root / 'runs')


if __name__ == '__main__':
    unittest.main()
