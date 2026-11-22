"""Offline data collector tests: only temporary SQLite, never production HTTP."""
import csv
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from learning import dataset_collection_v22 as c


class DatasetCollectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.prod = self.base / 'production.sqlite3'
        self.learn = self.base / 'learning.sqlite3'
        db = sqlite3.connect(self.prod)
        db.executescript('''
            CREATE TABLE jobs(id INTEGER PRIMARY KEY, url TEXT, final_url TEXT,
              status TEXT, risk TEXT, route_reason TEXT, reason TEXT,
              updated_at TEXT, classifier_json TEXT);
            CREATE INDEX idx_test_jobs_status ON jobs(status,id);
            CREATE TABLE bima_prealerts(job_id INTEGER,feedback_label TEXT,
              reviewer_id TEXT, reviewed_at TEXT);
            CREATE TABLE registry_snapshot_active_v2(singleton INTEGER, cycle_id TEXT);
            INSERT INTO registry_snapshot_active_v2 VALUES(1,'registry-example');
        ''')
        for i in range(1, 10):
            status = ('alerted', 'complete', 'inconclusive')[(i-1) % 3]
            meta = {'status': 'high_risk' if status == 'alerted' else 'low_risk',
                    'indobert_pred': 'non-gambling',
                    'review_excerpt': 'Situs ini berisi contoh konten bermakna untuk review ' + str(i)}
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                       (i, 'http://site%d.id/' % i, None, status, None, None, None,
                        '2026-11-22T10:00:00Z', json.dumps(meta)))
        db.execute('INSERT INTO bima_prealerts VALUES(?,?,?,?)',
                   (3,'gambling','analis','2026-11-22T10:30:00Z'))
        db.commit()
        db.close()
        ldb = sqlite3.connect(self.learn)
        ldb.executescript('''
            CREATE TABLE review_cases(id INTEGER PRIMARY KEY,hostname TEXT,
                domain_group TEXT,text_label TEXT,label TEXT,reviewer TEXT);
            CREATE TABLE case_evidence(case_id INTEGER,text_value TEXT,text_sha256 TEXT);
            CREATE TABLE learning_observations_v2(source_uid TEXT, hostname TEXT,
                domain_group TEXT, text_label TEXT,evidence_text TEXT,
                evidence_sha256 TEXT,text_reviewer TEXT);
        ''')
        txt = 'Promo taruhan dan bonus, contoh ulasan analis yang sudah terverifikasi.'
        ldb.execute('INSERT INTO review_cases VALUES(?,?,?,?,?,?)',
                    (1,'site1.id','site1.id','gambling','true_positive','analis'))
        ldb.execute('INSERT INTO case_evidence VALUES(?,?,?)',(1,txt,c.sha(txt)))
        ldb.execute('INSERT INTO learning_observations_v2 VALUES(?,?,?,?,?,?,?)',
                    ('2','site2.id','site2.id',None,txt,c.sha(txt),None))
        ldb.commit()
        ldb.close()

    def test_collect_registry_keeps_predictions_unlabeled(self):
        records, stats = c.collect_registry(self.prod, alert=2, complete=2, uncertain=2)
        self.assertEqual(stats['active_cycle'], 'registry-example')
        self.assertEqual(stats['reviewed_prealert_included'], 1)
        self.assertTrue(all(x['training_approved'] is False for x in records))
        self.assertTrue(all(x['text_label'] is None for x in records))
        pre = next(x for x in records if x['job_id']==3)
        self.assertEqual(pre['site_label'], 'true_positive')
        self.assertEqual(pre['site_label_source'], 'telegram_human_site')
        self.assertEqual(pre['model_metadata']['indobert_pred'], 'non-gambling')
        self.assertTrue(pre['evidence_available'])

    def test_internal_sources_are_readonly(self):
        before_prod = self.prod.read_bytes()
        before_learn = self.learn.read_bytes()
        c.collect_registry(self.prod)
        c.collect_verified(self.learn)
        self.assertEqual(before_prod, self.prod.read_bytes())
        self.assertEqual(before_learn, self.learn.read_bytes())

    def test_existing_human_labels_are_verifiable(self):
        out, status = c.collect_verified(self.learn)
        self.assertEqual(status['verified_deduplicated'], 1)
        self.assertEqual(out[0]['label'], 'gambling')
        self.assertTrue(out[0]['training_approved'])

    def test_conflicting_human_labels_get_quarantined(self):
        db = sqlite3.connect(self.learn)
        text = db.execute('SELECT text_value FROM case_evidence').fetchone()[0]
        db.execute("INSERT INTO learning_observations_v2 VALUES(?,?,?,?,?,?,?)",
                   ('3','site3.id','site3.id','non_gambling',text,c.sha(text),'analis'))
        db.commit()
        db.close()
        out, stats = c.collect_verified(self.learn)
        self.assertEqual(out, [])
        self.assertEqual(stats['verified_conflicts'], 1)

    def test_unlabeled_learning_case_not_counted(self):
        rows, _ = c.collect_verified(self.learn)
        self.assertEqual(len(rows), 1)

    def test_redaction_masks_common_identifiers(self):
        raw = 'Email admin@example.id, telepon 081234567890 dan https://site.id/a?key=secret @person'
        txt = c.redact(raw)
        for needle in ('admin@example.id','081234567890','https://site.id','@person'):
            self.assertNotIn(needle, txt)

    def test_external_csv_is_auxiliary_not_ground_truth(self):
        records = [{'id': '1','text_caption':'Promosi taruhan daring contoh narasi penelitian bahasa Indonesia','platform':'X','label':'1.0'},
                   {'id': '2','text_caption':'Pelatihan literasi digital untuk guru dan siswa sekolah','platform':'Facebook','label':'0.0'}]
        import io
        buff = io.StringIO()
        writer = csv.DictWriter(buff,fieldnames=['id','text_caption','platform','label'])
        writer.writeheader(); writer.writerows(records)
        rows, stats = c.collect_external_csv(buff.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats['public_labels'], {'gambling': 1, 'non_gambling': 1})
        self.assertTrue(all(r['training_approved'] is False for r in rows))
        self.assertTrue(all(r['evaluation_approved'] is False for r in rows))

    def test_external_conflicting_duplicate_removed(self):
        import io
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=['id','text_caption','platform','label'])
        w.writeheader()
        text = 'Contoh iklan penawaran bonus taruhan olahraga di Indonesia'
        w.writerows([{'id':'1','text_caption':text,'platform':'X','label':'1'},
                     {'id':'2','text_caption':text,'platform':'X','label':'0'}])
        rows, stats = c.collect_external_csv(s.getvalue())
        self.assertEqual(rows, [])
        self.assertEqual(stats['public_rows_skipped'], 1)

    def test_publish_to_new_private_directory(self):
        out = self.base / 'packs' / 'demo'
        out.parent.mkdir()
        verified, _ = c.collect_verified(self.learn)
        manifest = c.publish_pack(out, candidates=[], verified=verified, external=[], summary={})
        self.assertEqual(manifest['counts']['verified_bima_examples'], 1)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / 'verified_bima.jsonl').stat().st_mode & 0o777, 0o600)
        self.assertEqual(len((out / 'verified_bima.jsonl').read_text().splitlines()), 1)
        with self.assertRaises(ValueError):
            c.publish_pack(out,candidates=[],verified=[],external=[],summary={})

    def test_does_not_fetch_public_without_opt_in(self):
        with patch.object(c, 'public_csv_from_web', side_effect=AssertionError('network')):
            candidates, _ = c.collect_registry(self.prod)
            verified, _ = c.collect_verified(self.learn)
            self.assertTrue(candidates and verified)


if __name__ == '__main__':
    unittest.main()

class DatasetCliTests(unittest.TestCase):
    def test_cli_requires_existing_sources_and_writes_nothing_on_failure(self):
        import importlib.util
        path = Path(__file__).resolve().parents[2] / 'tools' / 'bima_dataset_collect_v22.py'
        spec = importlib.util.spec_from_file_location('bima_dataset_collect_v22', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'newpack'
            status = module.main([
                '--production-db', str(Path(temp) / 'missing.sqlite3'),
                '--learning-db', str(Path(temp) / 'learning.sqlite3'),
                '--out', str(out)])
            self.assertEqual(status, 1)
            self.assertFalse(out.exists())
