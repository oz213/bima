"""Offline regression tests: no torch, model weights, network or BIMA DB."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'multi'))
from shadow_capture_v1 import build_event, capture, _sample, MAX_TEXT_CHARS

spec = importlib.util.spec_from_file_location('shadow_live', ROOT / 'tools' / 'bima_indobert_shadow_live_v1.py')
shadow_live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow_live)


class FakeScorer:
    def __init__(self):
        self.calls = 0

    def score(self, text):
        self.calls += 1
        return {**shadow_live.aggregate([0.999, 0.001]),
                'n_chunks': 2, 'chunk_lengths': [5, 5],
                'chunk_probabilities': [0.999, 0.001], 'unscored_tail': False}


summary_spec = importlib.util.spec_from_file_location('shadow_summary', ROOT / 'tools' / 'bima_shadow_summary_v1.py')
shadow_summary = importlib.util.module_from_spec(summary_spec)
summary_spec.loader.exec_module(shadow_summary)


class ShadowUnitTests(unittest.TestCase):
    def sample_event(self, txt='casino online bonus'):
        return build_event('https://casino.test.id/a?x=secret',
                           'https://casino.test.id/a?x=secret', txt,
                           'requests', {'status': 'medium_risk', 'bert_used': True,
                                        'indobert_conf': 0.7, 'indobert_pred': 'non-gambling'})

    def test_capture_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            self.assertFalse(capture('https://x.id', 'https://x.id', 'slot', 'requests', {'bert_used': True}))
            self.assertEqual(list(Path(d).glob('*')), [])

    def test_capture_skips_when_production_bert_not_used(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'BIMA_INDOBERT_SHADOW_CAPTURE': '1', 'BIMA_INDOBERT_SHADOW_RATE': '0.1',
            'BIMA_INDOBERT_SHADOW_SPOOL_DIR': d,
        }):
            self.assertFalse(capture('https://x.id', 'https://x.id', 'slot', 'requests', {'bert_used': False}))

    def test_capture_writes_private_sample_no_query_in_metadata(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'BIMA_INDOBERT_SHADOW_CAPTURE': '1', 'BIMA_INDOBERT_SHADOW_RATE': '0.1',
            'BIMA_INDOBERT_SHADOW_SPOOL_DIR': d,
        }), patch('shadow_capture_v1._sample', return_value=True):
            self.assertTrue(capture('https://casino.id/?password=abc',
                                    'https://casino.id/?password=abc',
                                    'Slot promo', 'requests',
                                    {'status': 'high_risk', 'bert_used': True,
                                     'indobert_conf': 0.99, 'indobert_pred': 'gambling'}))
            files = list(Path(d).glob('*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            row = json.loads(files[0].read_text())
            self.assertEqual(row['hostname'], 'casino.id')
            self.assertNotIn('password', str(row))

    def test_sample_is_stable(self):
        self.assertEqual(_sample('https://test.id', 0.01), _sample('https://test.id', 0.01))

    def test_invalid_rate_fails_open(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'BIMA_INDOBERT_SHADOW_CAPTURE': '1', 'BIMA_INDOBERT_SHADOW_RATE': '1.2',
            'BIMA_INDOBERT_SHADOW_SPOOL_DIR': d,
        }):
            self.assertFalse(capture('https://x.id', 'https://x.id', 'slot', 'requests', {'bert_used': True}))

    def test_event_text_cap_and_sha(self):
        s = 'G' * (MAX_TEXT_CHARS + 70)
        e = self.sample_event(s)
        self.assertTrue(e['text_truncated'])
        self.assertEqual(len(e['text']), MAX_TEXT_CHARS)
        self.assertEqual(len(e['original_text_sha256']), 64)

    def test_reject_host_without_id(self):
        self.assertIsNone(build_event('https://elsewhere.com', 'https://elsewhere.com',
                                      'test', 'requests', {'bert_used': True}))

    def test_chunk_repair_short_final(self):
        # Sentence boundary near end produces a micro-chunk.
        txt = 'A' * 998 + '. b'
        chunks, tail = shadow_live.chunk_v27(txt, 32)
        self.assertEqual(len(chunks), 1)
        self.assertFalse(tail)
        self.assertIn('. b', chunks[0])

    def test_chunk_long_and_bounded(self):
        chunks, tail = shadow_live.chunk_v27('word ' * 10000)
        self.assertLessEqual(len(chunks), 8)
        self.assertTrue(tail)

    def test_mean_masks_one_strong_chunk(self):
        v = shadow_live.aggregate([0.999, 0.0004])
        self.assertFalse(v['hit'])
        self.assertTrue(v['masked_strong_chunk'])
        self.assertAlmostEqual(v['confidence'], 0.4997)

    def test_positive_single_chunk(self):
        self.assertTrue(shadow_live.aggregate([0.98])['hit'])
        self.assertFalse(shadow_live.aggregate([0.85])['hit'])

    def test_positive_two_chunks(self):
        self.assertTrue(shadow_live.aggregate([0.98, 0.82])['hit'])

    def test_cannot_read_invalid_event_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ('a' * 32 + '.json')
            p.write_text(json.dumps({'version': 'other', 'text': 'slot', 'event_id': 'a' * 32}))
            with self.assertRaises(ValueError):
                shadow_live._load_event(p)

    def test_worker_consumes_without_saving_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            queue = base / 'queue'
            out = base / 'out'
            queue.mkdir(mode=0o700)
            out.mkdir(mode=0o700)
            event = self.sample_event('Unique private text ABC 123')
            p = queue / (event['event_id'] + '.json')
            p.write_text(json.dumps(event), encoding='utf-8')
            a = argparse.Namespace(spool=str(queue), out=str(out), once=True,
                                   max_events=10, poll_seconds=1, model='', device='cpu',
                                   batch_size=2, max_length=256,
                                   gambling_index=1, torch_threads=1, min_available_mib=0)
            f = FakeScorer()
            self.assertEqual(shadow_live.run_worker(a, scorer=f), 1)
            self.assertEqual(f.calls, 1)
            self.assertFalse(p.exists())
            report = out / 'shadow_comparisons_v1.jsonl'
            row = json.loads(report.read_text())
            self.assertTrue(row['candidate']['masked_strong_chunk'])
            self.assertFalse(row['training_approved'])
            self.assertNotIn('Unique private text', report.read_text())
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_summary_counts_unique_events_without_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audit.jsonl'
            first = {'event_id': '1', 'candidate': {'hit': True, 'masked_strong_chunk': True},
                     'model_disagreement': True, 'candidate_checkpoint_sha256': 'abc'}
            second = {'event_id': '2', 'candidate': {'hit': False, 'masked_strong_chunk': False},
                      'model_disagreement': False, 'candidate_checkpoint_sha256': 'abc'}
            path.write_text('\n'.join(json.dumps(x) for x in [first, first, second]) + '\n')
            counts = shadow_summary.summarize(path)
            self.assertEqual(counts['observations']['total'], 2)
            self.assertEqual(counts['observations']['model_disagreement'], 1)
            self.assertFalse(counts['accuracy_validated'])

    def test_worker_refuses_readable_to_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'world'
            p.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                shadow_live._private_dir(p)

    def test_bad_event_does_not_create_alert_or_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            q = base / 'queue'; o = base / 'out'
            q.mkdir(mode=0o700); o.mkdir(mode=0o700)
            (q / ('a'*32+'.json')).write_text('not json')
            a = argparse.Namespace(spool=str(q), out=str(o), once=True,
                                   max_events=10, poll_seconds=1, model='', device='cpu',
                                   batch_size=2, max_length=256,
                                   gambling_index=1, torch_threads=1, min_available_mib=0)
            self.assertEqual(shadow_live.run_worker(a, scorer=FakeScorer()), 0)
            self.assertFalse((o / 'shadow_comparisons_v1.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
