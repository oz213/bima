"""BIMA v2.1 workbench: synthetic DBs, no crawler/model/Telegram/network calls."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from learning import continuous_v2 as v2, core
from learning import review_workbench_v21 as wb


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.learning = self.root / 'learning.sqlite3'
        self.production = self.root / 'production.sqlite3'
        with core.learning_connection(self.learning) as db:
            v2._init(db)
        with closing(sqlite3.connect(self.production)) as db, db:
            db.executescript('''
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, url TEXT, status TEXT,
                    risk TEXT, route_reason TEXT, updated_at TEXT
                );
                CREATE INDEX idx_jobs_status ON jobs(status,id);
            ''')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', [
                (1,'http://safe-school.my.id/path?access=secret','complete','low','low_signal','x'),
                (2,'http://unknown.id/stuff','inconclusive','medium','captcha','x'),
                (5,'http://alarm.id/','alerted','high','risky_http','x'),
                (9,'http://news.web.id/story','complete','low','safe_news_or_factcheck_context','x'),
                (15,'http://other.biz.id/','inconclusive','medium','low_signal','x'),
            ])

    def v1_case(self, ident, *, site=None, text_label=None, evidence=None, group=None):
        host = group or ('case%d.my.id' % ident)
        with core.learning_connection(self.learning) as conn, conn:
            conn.execute(
                'INSERT INTO review_cases(id,source_job_id,hostname,domain_group,\n'
                'classifier_status,candidate_kind,model_metadata_json,snapshot_sha256,\n'
                'label,text_label,reviewer,created_at,updated_at)\n'
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident, ident, host, host, 'medium_risk', 'model_rule_disagreement',
                 '{}', 'hash', site, text_label, 'analyst' if site else None, 'x', 'x'))
            if evidence:
                conn.execute('INSERT INTO case_evidence VALUES (?,?,?,?,?)',
                             (ident, evidence, core.sha256_text(evidence), 'manual_capture', 'x'))

    def v2_case(self, uid, *, site='true_positive', text_label=None, evidence=None, group=None):
        host = group or ('v2-%s.my.id' % uid)
        with core.learning_connection(self.learning) as conn, conn:
            v2._init(conn)
            conn.execute(
                'INSERT INTO learning_observations_v2 '
                '(source_uid,cycle_id,source_kind,source_job_id,hostname,domain_group,site_label,'
                'text_label,evidence_text,evidence_sha256,reviewer,reviewed_at,text_reviewer,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid, 'legacy-before-snapshots', 'prealert', 100, host, host, site,
                 text_label, evidence, core.sha256_text(evidence) if evidence else None,
                 'analyst', 'x', 'analyst' if text_label else None, 'x', 'x'))

    def test_missing_learning_db_does_not_get_created(self):
        absent = self.root / 'absent.sqlite3'
        with self.assertRaises(FileNotFoundError):
            wb.status(absent)
        self.assertFalse(absent.exists())

    def test_symlink_db_refused(self):
        link = self.root / 'alias.sqlite3'
        link.symlink_to(self.learning)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            wb.status(link)

    def test_status_exact_readiness_counts(self):
        self.v1_case(1, site='true_positive', text_label='gambling',
                     evidence='casino bonus taruhan untuk permainan daring di situs ini 123')
        self.v1_case(2, site='false_positive', text_label='non_gambling',
                     evidence='Sekolah memberikan informasi kelas pendidikan dan murid tahun ini')
        self.v1_case(3, evidence='Belum diputuskan oleh manusia untuk label teks yang sah 123')
        self.v2_case('one', site='true_positive')
        data = wb.status(self.learning)
        self.assertEqual(data['readiness']['unique_verified_texts'], 2)
        self.assertEqual(data['readiness']['gambling'], 1)
        self.assertEqual(data['readiness']['non_gambling'], 1)
        self.assertEqual(data['readiness']['remaining']['verified_texts'], 98)
        self.assertEqual(data['v2']['text_labels']['unlabeled'], 1)
        self.assertEqual(data['v1']['text_labels']['unlabeled'], 1)
        self.assertEqual(data['readiness']['status'], 'not_ready')

    def test_worklist_prioritizes_ready_to_review_and_hides_text(self):
        secret = 'INTERNAL_EVIDENCE_MUST_NOT_LEAVE_DB ' * 5
        self.v1_case(1, evidence=secret)
        self.v1_case(2)
        self.v2_case('evidence', evidence=secret, group='unique-v2.id')
        self.v2_case('empty')
        queue = wb.queue(self.learning, limit=10)
        self.assertEqual([x['next_action'] for x in queue['review_items']],
                         ['label-text','review-site-and-text','attach-text','attach-evidence'])
        self.assertNotIn(secret, json.dumps(queue))
        self.assertNotIn('/path?', json.dumps(queue))
        self.assertEqual(queue['review_items'][0]['source_uid'], 'evidence')
        self.assertTrue(queue['review_items'][0]['has_evidence'])

    def test_queue_focus_filters_correctly(self):
        self.v2_case('evidence', evidence='Sudah diarsipkan dalam sistem'*4)
        self.v2_case('missing')
        self.v1_case(4, evidence='Sampel sumber adalah teks dari pemantauan'*4)
        self.assertEqual(len(wb.queue(self.learning, focus='needs-evidence')['review_items']), 1)
        self.assertEqual(len(wb.queue(self.learning, focus='needs-text-label')['review_items']), 1)
        self.assertEqual(len(wb.queue(self.learning, focus='needs-site-review')['review_items']), 1)
        with self.assertRaises(ValueError):
            wb.queue(self.learning, focus='unknown')
        with self.assertRaises(ValueError):
            wb.queue(self.learning, limit=101)

    def test_no_worklist_for_approved_training(self):
        self.v1_case(1, site='true_positive', text_label='gambling',
                     evidence='kasino resmi dalam contoh simulasi data latih yang terverifikasi')
        self.v2_case('done', text_label='non_gambling',
                     evidence='sekolah mengajar siswa di kelas dalam program pendidikan')
        self.assertEqual(wb.queue(self.learning)['review_items'], [])

    def test_dataset_integrity_fails_closed(self):
        self.v1_case(1, site='true_positive', text_label='gambling',
                     evidence='kasino promo bonus dengan bukti teks asli yang tidak disamarkan')
        with core.learning_connection(self.learning) as db, db:
            db.execute('UPDATE case_evidence SET text_sha256=? WHERE case_id=1', ('bad-hash',))
        result = wb.status(self.learning)['readiness']
        self.assertEqual(result['status'], 'not_ready')
        self.assertTrue(result['integrity_issues'])
        self.assertEqual(result['unique_verified_texts'], 0)

    def test_duplicate_evidence_deduplicated_like_learner(self):
        txt='contoh yang sama untuk sumber yang sama dan kelas sama'*4
        self.v1_case(1, site='true_positive', text_label='gambling', evidence=txt, group='same.id')
        self.v2_case('copy', text_label='gambling', evidence=txt, group='same.id')
        report=wb.status(self.learning)['readiness']
        self.assertEqual(report['unique_verified_texts'], 1)
        self.assertEqual(report['gambling'], 1)

    def test_cross_group_same_text_blocks_ready(self):
        txt='contoh dari domain berbeda perlu pengelompokan konsisten'*4
        self.v1_case(1, site='true_positive', text_label='gambling', evidence=txt, group='other.id')
        self.v2_case('copy', text_label='gambling', evidence=txt, group='another.id')
        report=wb.status(self.learning)['readiness']
        self.assertEqual(report['status'], 'not_ready')
        self.assertTrue(report['integrity_issues'])

    def test_systematic_sample_metadata_only_and_both_states(self):
        result = wb.sample_production(self.production, per_status=5)
        statuses = {item['current_status'] for item in result['items']}
        self.assertEqual(statuses, {'complete','inconclusive'})
        self.assertNotIn('access=secret',json.dumps(result))
        self.assertTrue(all(item['human_label'] is None for item in result['items']))
        self.assertIn('NOT representative', result['sampling'])
        with self.assertRaises(ValueError):
            wb.sample_production(self.production, per_status=0)

    def test_read_only_commands_do_not_modify_rows(self):
        self.v1_case(1, evidence='teks tanpa label asli panjang dan perlu diverifikasi')
        before = sqlite3.connect(self.learning).execute('SELECT COUNT(*) FROM review_cases').fetchone()[0]
        wb.status(self.learning)
        wb.queue(self.learning)
        wb.sample_production(self.production)
        with closing(sqlite3.connect(self.learning)) as db:
            after=db.execute('SELECT COUNT(*) FROM review_cases').fetchone()[0]
        self.assertEqual(before, after)
        with closing(sqlite3.connect(self.learning)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_events_v2').fetchone()[0], 0)

    def test_quality_gate_split_both_classes(self):
        # 200 unique domain groups spread deterministically over split buckets.
        for i in range(1,201):
            gambling = i%2 == 1
            txt=('casino judi bonus menang sesi '+str(i) if gambling
                 else 'sekolah edukasi kelas belajar sesi '+str(i)) + ' x' * 40
            self.v1_case(i, site='true_positive' if gambling else 'false_positive',
                         text_label='gambling' if gambling else 'non_gambling', evidence=txt)
        report=wb.status(self.learning)['readiness']
        self.assertEqual(report['unique_verified_texts'], 200)
        self.assertTrue(report['splits_have_both_classes'])
        self.assertEqual(report['status'], 'candidate_eligible')

    def test_report_can_only_be_saved_to_new_private_file(self):
        from importlib.util import spec_from_file_location, module_from_spec
        script = Path(__file__).resolve().parents[2] / 'tools/bima_learning_workbench_v21.py'
        spec = spec_from_file_location('learning_ops_cli_v21',script)
        mod = module_from_spec(spec)
        spec.loader.exec_module(mod)
        target = self.root / 'audit.json'
        report = {'safe': True}
        mod.output_report(report,target)
        self.assertEqual(json.loads(target.read_text()), report)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            mod.output_report(report,target)

    def test_cli_status_is_read_only_and_no_secret(self):
        from importlib.util import spec_from_file_location, module_from_spec
        script = Path(__file__).resolve().parents[2] / 'tools/bima_learning_workbench_v21.py'
        spec=spec_from_file_location('workbench_cli_v21',script)
        mod=module_from_spec(spec)
        spec.loader.exec_module(mod)
        captured=io.StringIO()
        with redirect_stdout(captured):
            result=mod.main(['--learning-db',str(self.learning),'status'])
        self.assertEqual(result,0)
        self.assertIn('production_model_unchanged',captured.getvalue())
        self.assertNotIn('text_value',captured.getvalue())


if __name__ == '__main__':
    unittest.main()
