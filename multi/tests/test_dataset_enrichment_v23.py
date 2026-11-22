"""Offline BIMA evidence enrichment tests; no actual network or production writes."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from learning import dataset_enrichment_v23 as e


class EvidenceEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack = self.root / 'pack'
        self.pack.mkdir()
        self.db = self.root / 'crawler.sqlite3'
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY,url TEXT NOT NULL)')
        rows = [(1, 'http://a.id'), (2, 'https://b.id/news'),
                (3, 'https://c.id'), (4, 'http://d.id')]
        conn.executemany('INSERT INTO jobs VALUES (?,?)', rows)
        conn.commit()
        conn.close()
        self.cases = [
            {'job_id': 1, 'hostname': 'a.id', 'stratum': 'reviewed_prealert',
             'observation_uid': '1', 'site_label': 'true_positive',
             'evidence_available': False, 'training_approved': False},
            {'job_id': 2, 'hostname': 'b.id', 'stratum': 'complete',
             'observation_uid': '2', 'site_label': None,
             'evidence_available': False, 'training_approved': False},
            {'job_id': 3, 'hostname': 'c.id', 'stratum': 'alert',
             'observation_uid': '3', 'site_label': None,
             'evidence_available': False, 'training_approved': False},
            {'job_id': 4, 'hostname': 'd.id', 'stratum': 'uncertain',
             'observation_uid': '4', 'site_label': None,
             'evidence_available': False, 'training_approved': False},
        ]
        (self.pack / 'manifest.json').write_text(json.dumps(
            {'version': 'bima-dataset-collection-v2.2'}))
        self._save_candidates()

    def _save_candidates(self):
        (self.pack / 'review_candidates.jsonl').write_text(''.join(
            json.dumps(x) + '\n' for x in self.cases))

    def test_round_robin_covers_each_stratum(self):
        cases, _ = e._read_candidates(self.pack)
        chosen = e.choose_cases(cases, 4)
        self.assertEqual({x['stratum'] for x in chosen},
                         {'reviewed_prealert', 'alert', 'complete', 'uncertain'})
        self.assertEqual(chosen[0]['job_id'], 1)

    def test_offset_batches_are_disjoint(self):
        one = e.choose_cases(self.cases, 2, offset=0)
        two = e.choose_cases(self.cases, 2, offset=2)
        self.assertTrue(one and two)
        self.assertFalse({c['job_id'] for c in one} & {c['job_id'] for c in two})

    def test_duplicate_hostname_skipped(self):
        self.cases.append({'job_id': 5, 'hostname': 'b.id', 'stratum': 'complete',
                           'observation_uid': '5', 'evidence_available': False})
        self._save_candidates()
        chosen = e.choose_cases(self.cases, 5)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(len({r['hostname'] for r in chosen}), 4)

    def test_excerpts_not_fetched_twice(self):
        self.cases[1]['evidence_available'] = True
        self._save_candidates()
        self.assertNotIn(2, [c['job_id'] for c in e.choose_cases(self.cases, 4)])

    def test_duplicate_job_id_rejected(self):
        self.cases.append(dict(self.cases[0]))
        self._save_candidates()
        with self.assertRaisesRegex(ValueError, 'Duplicate job_id'):
            e._read_candidates(self.pack)

    def test_safe_fetch_reuses_no_alert_bima_http_path(self):
        fake = SimpleNamespace(fetch_http=Mock(return_value={
            'ok': True, 'status_code': 200, 'final_url': 'https://a.id/',
            'text': ('Berita edukasi masyarakat terkait risiko perjudian daring '
                     'dan pencegahannya; tidak ada promosi atau tautan taruhan.')}))
        with patch.dict(sys.modules, {'crawler_multi': fake}):
            result = e.safe_fetch_evidence('https://a.id')
        self.assertEqual(result['result'], 'evidence_captured')
        self.assertEqual(result['final_host'], 'a.id')
        self.assertEqual(fake.fetch_http.call_count, 1)
        args = fake.fetch_http.call_args.args[2]
        self.assertEqual(args.http_max_bytes, 512 * 1024)
        self.assertEqual(args.http_connect_timeout, 5)

    def test_size_limits(self):
        with self.assertRaises(ValueError):
            e.choose_cases(self.cases, 251)
        with self.assertRaises(ValueError):
            e.choose_cases(self.cases, 0)

    def test_evidence_is_live_and_not_training_approved(self):
        with patch.object(e, 'safe_fetch_evidence') as dummy:
            dummy.return_value = {'excerpt': 'Evidence this page is currently hosting a gambling advertisement.',
                                  'http_status': 200, 'result': 'evidence_captured'}
            output = self.root / 'enriched'
            before = self.db.read_bytes()
            report = e.enrich(self.pack, self.db, output, limit=2, fetcher=dummy)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(report['captured'], 2)
        self.assertEqual(report['attempted'], 2)
        result = [json.loads(x) for x in (output / 'enriched_review_candidates.jsonl').read_text().splitlines()]
        self.assertEqual(len(result), 4)
        a = result[0]
        self.assertEqual(a['site_label'], 'true_positive')
        self.assertIsNone(a['text_label'])
        self.assertIs(a['training_approved'], False)
        self.assertEqual(a['evidence_source'], 'live_http_refetch_v23')
        self.assertIs(a['historical_evidence_equivalence'], False)
        self.assertEqual((output.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((output/'enriched_review_candidates.jsonl').stat().st_mode & 0o777), 0o600)

    def test_source_mismatch_refuses_to_fetch(self):
        self.cases[0]['hostname'] = 'changed.id'
        self._save_candidates()
        dummy = Mock(side_effect=AssertionError('must not fetch altered hostname'))
        report = e.enrich(self.pack, self.db, self.root / 'mismatch', limit=1, fetcher=dummy)
        self.assertEqual(report['attempted'], 0)
        self.assertEqual(report['skipped'], 1)
        dummy.assert_not_called()

    def test_fetch_exception_keeps_unverified_candidate(self):
        dummy = Mock(side_effect=RuntimeError('simulated timeout'))
        report = e.enrich(self.pack, self.db, self.root / 'failed', limit=1, fetcher=dummy)
        self.assertEqual(report['failed'], 1)
        result = json.loads((self.root/'failed'/'enriched_review_candidates.jsonl').read_text().splitlines()[0])
        self.assertFalse(result['evidence_available'])
        self.assertEqual(result['enrichment_status'], 'fetch_exception')
        self.assertIsNone(result['text_label'])

    def test_output_never_overwritten(self):
        dest = self.root / 'existing'
        dest.mkdir()
        with self.assertRaises(ValueError):
            e.enrich(self.pack, self.db, dest, fetcher=Mock())

    def test_pack_must_be_correct_version(self):
        (self.pack/'manifest.json').write_text(json.dumps({'version':'unknown'}))
        with self.assertRaises(ValueError):
            e._read_candidates(self.pack)

    def test_text_parser_ignores_scripts(self):
        parser = e._TextOnlyEngine()
        result = parser.extract_text_from_html('<title>Hello</title><style>secret</style>'
                                               '<script>bad_token</script><p>Safe paragraph text</p>',
                                               'https://a.id')
        self.assertEqual(result[1], 'Hello')
        self.assertNotIn('bad_token', result[0])
        self.assertNotIn('secret', result[0])
        self.assertIn('Safe paragraph text', result[0])

    def test_cli_without_explicit_fetch_never_touches_network(self):
        path = Path(__file__).resolve().parents[2] / 'tools' / 'bima_dataset_enrich_v23.py'
        spec = importlib.util.spec_from_file_location('enrich_cli', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(e, 'safe_fetch_evidence', side_effect=AssertionError('network')):
            status = module.main(['--pack', str(self.pack), '--production-db', str(self.db),
                                  '--out', str(self.root/'preview'), '--limit', '3'])
        self.assertEqual(status, 0)
        self.assertFalse((self.root/'preview').exists())

    def test_case_hostname_must_be_id(self):
        self.cases[0]['hostname'] = 'localhost'
        self._save_candidates()
        self.assertNotIn(1, [x['job_id'] for x in e.choose_cases(self.cases, 4)])


if __name__ == '__main__':
    unittest.main()
