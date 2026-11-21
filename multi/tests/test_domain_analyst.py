import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import domain_analyst as analyst


class DomainAnalystTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'production.sqlite3'
        with sqlite3.connect(self.db) as conn:
            conn.executescript('''
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
                    http_status INTEGER, reason TEXT, risk TEXT,
                    classifier_json TEXT, final_url TEXT, evidence_path TEXT,
                    alert_confidence REAL, error TEXT, updated_at TEXT,
                    route_reason TEXT, error_category TEXT, next_retry_at TEXT,
                    dns_result TEXT, dns_host TEXT
                );
                CREATE INDEX idx_jobs_status ON jobs(status,id);
            ''')

    def add(self, job=42, *, status='complete', risk='low_risk',
            classification=None, url=None, **extra):
        values = dict(id=job, url=url or f'https://site{job}.id/', status=status,
                      risk=risk, classifier_json=json.dumps(classification or {}),
                      updated_at='2026-11-21T13:45:40Z', **extra)
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO jobs (' + ','.join(values) + ') VALUES ('
                         + ','.join('?' for _ in values) + ')', tuple(values.values()))

    def answer(self, targets=('#42',), question='', history=(), **kwargs):
        return analyst.answer_question(self.db, targets, question, history,
                                       environ={'BIMA_ANALYST_PROVIDER': 'disabled'}, **kwargs)

    def test_risky_report_has_sources_and_pending_delivery_is_not_success(self):
        self.add(status='pending_alert', risk='high_risk', classification={
            'status': 'high_risk', 'reason': 'ml_gambling_detected',
            'ml_source': 'indobert', 'ml_pred': 'gambling', 'ml_conf': .93,
            'bert_used': True, 'indobert_pred': 'gambling', 'indobert_conf': .93,
        })
        result = self.answer()
        self.assertIn('[job#42]', result['text'])
        self.assertIn('pending_alert', result['text'])
        self.assertIn('belum terkonfirmasi', result['text'])
        self.assertIn('0.9300', result['text'])
        self.assertIn('pembaruan status', result['text'].lower())
        self.assertIn('belum diaktifkan', result['text'])
        self.assertEqual(result['status'], 'disabled')

    def test_failed_scan_stays_inconclusive_even_if_old_risk_is_low(self):
        self.add(status='retry_http', risk='low_risk', error_category='timeout',
                 reason='fetch_failed', classification={'status': 'inconclusive'})
        result = self.answer()
        self.assertIn('Belum dapat disimpulkan', result['text'])
        self.assertIn('timeout', result['text'])
        self.assertIn('bukan bukti aman', result['text'])

    def test_safe_rule_zero_is_not_presented_as_measured_probability(self):
        self.add(classification={'status': 'low_risk', 'reason': 'safe_news_or_factcheck_context',
                                 'bert_used': False, 'ml_conf': 0.0,
                                 'ml_pred': 'non-gambling'})
        text = self.answer()['text']
        self.assertIn('inferensi tidak tercatat', text)
        self.assertNotIn('skor=0.0000', text)
        self.assertNotIn('100% aman', text)

    def test_rule_and_indobert_disagreement_is_not_erased(self):
        self.add(status='alerted', risk='medium_risk', classification={
            'status': 'medium_risk', 'reason': 'contextual_gambling_phrase_detected',
            'ml_source': 'classic_ml', 'ml_pred': 'non-gambling', 'ml_conf': .04,
            'bert_used': True, 'indobert_pred': 'non-gambling', 'indobert_conf': .03,
        })
        text = self.answer()['text']
        self.assertIn('medium_risk', text)
        self.assertIn('non-gambling', text)
        self.assertIn('berbeda', text)
        self.assertIn('review', text.lower())
        self.assertIn('contextual_gambling_phrase_detected', text)

    def test_comparison_does_not_invent_a_before_after_history(self):
        self.add(classification={'status': 'low_risk', 'reason': 'low_signal'})
        self.add(43, status='alerted', risk='high_risk',
                 classification={'status': 'high_risk', 'reason': 'ml_gambling_detected'})
        result = self.answer(('#42', '#43'))
        self.assertIn('[job#42]', result['text'])
        self.assertIn('[job#43]', result['text'])
        self.assertIn('bukan riwayat', result['text'])
        self.assertEqual(result['context']['targets'], ['#42', '#43'])

    def test_empty_lookup_is_not_a_safe_or_never_crawled_verdict(self):
        with patch.object(analyst, 'generate_answer') as model:
            result = self.answer(('absent.id',))
        self.assertIn('Tidak ditemukan hasil pada pencarian ini', result['text'])
        self.assertIn('belum pernah diperiksa', result['text'])
        self.assertIn('bukan bukti', result['text'])
        model.assert_not_called()

    def test_analysis_preserves_database_and_does_not_create_learning_db(self):
        self.add()
        before = self.db.read_bytes()
        learning = self.db.parent / 'absent-learning.sqlite3'
        self.answer(learning_db=learning)
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(learning.exists())

    def test_context_is_bounded_redacted_and_uses_selected_ids(self):
        self.add(url='https://site42.id/VERYPRIVATE?token=SECRET')
        history = [{'question': 'old', 'answer': 'x' * 6000}] * 20
        result = self.answer(('https://site42.id/VERYPRIVATE?token=SECRET',),
                             'Jelaskan https://site42.id/VERYPRIVATE?token=SECRET', history)
        context = result['context']
        self.assertEqual(context['targets'], ['#42'])
        self.assertLessEqual(len(context['history']), 2)
        encoded = json.dumps(context)
        self.assertNotIn('VERYPRIVATE', encoded)
        self.assertNotIn('SECRET', encoded)
        self.assertLessEqual(len(encoded), 5000)

    def test_generated_answer_keeps_source_anchors_and_mode(self):
        self.add()
        with patch.object(analyst, 'generate_answer', return_value={
                'status': 'answered', 'text': 'Bukti masih terbatas [job#42].',
                'provider': 'ollama', 'model': 'chosen-model'}):
            result = self.answer(question='Mengapa?')
        self.assertIn('Penjelasan AI', result['text'])
        self.assertIn('Bukti masih terbatas [job#42].', result['text'])
        self.assertIn('complete', result['text'])
        self.assertIn('2026-11-21T13:45:40Z', result['text'])
        self.assertEqual(result['status'], 'answered')

    def test_provider_failure_returns_sourced_report_without_error_body(self):
        self.add()
        with patch.object(analyst, 'generate_answer', return_value={
                'status': 'unavailable', 'text': 'secret raw provider response',
                'provider': 'openai', 'model': 'configured'}):
            result = self.answer(question='Mengapa?')
        self.assertIn('[job#42]', result['text'])
        self.assertIn('penyedia model', result['text'])
        self.assertNotIn('secret raw', result['text'])
        self.assertEqual(result['status'], 'unavailable')

    def test_unexpected_provider_exception_is_scrubbed(self):
        self.add()
        with patch.object(analyst, 'generate_answer', side_effect=RuntimeError('TOKEN-secret')):
            with self.assertLogs('pandi.domain.analyst', level='WARNING') as captured:
                result = self.answer()
        self.assertNotIn('TOKEN-secret', result['text'] + ' '.join(captured.output))
        self.assertIn('[job#42]', result['text'])

    def test_maximum_record_report_fits_telegram_without_truncated_sources(self):
        for job in range(1, 6):
            self.add(job, url=f'https://long-domain.id/path{job}', classification={
                'status': 'high_risk', 'reason': 'contextual_gambling_phrase_detected',
                'review_excerpt': 'cuplikan panjang ' * 100,
            })
        result = self.answer(('long-domain.id',))
        self.assertLessEqual(len(result['text']), 3800)
        for job in range(1, 6):
            self.assertIn(f'[job#{job}]', result['text'])
        self.assertIn('batas', result['text'].lower())

    def test_invalid_or_oversized_input_never_reaches_reader_or_model(self):
        with patch.object(analyst, 'lookup_evidence') as reader, \
                patch.object(analyst, 'generate_answer') as model:
            for targets, question in [((), ''), (('#1',) * 6, ''), (('#1',), 'x' * 1001)]:
                result = self.answer(targets, question)
                self.assertEqual(result['status'], 'invalid_request')
        reader.assert_not_called()
        model.assert_not_called()

    def test_cli_evidence_only_overrides_configured_cloud_and_prints_json(self):
        self.add()
        stream = io.StringIO()
        with patch.dict('os.environ', {'BIMA_ANALYST_PROVIDER': 'openai',
                                      'BIMA_ANALYST_ALLOW_CLOUD': '1'}, clear=True), \
                contextlib.redirect_stdout(stream):
            status = analyst.main(['--db', str(self.db), '--target', '#42',
                                   '--evidence-only', '--json'])
        self.assertEqual(status, 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['status'], 'disabled')
        self.assertIn('[job#42]', result['text'])

    def test_empty_lookup_does_not_broaden_private_url_into_domain_context(self):
        self.add(url='https://site42.id/other')
        result = self.answer(('https://site42.id/private?token=not-found',))
        self.assertEqual(result['context']['targets'], [])
        self.assertEqual(result['evidence']['records'], [])

    def test_unknown_ai_citation_is_rejected_without_losing_real_sources(self):
        self.add()
        with patch.object(analyst, 'generate_answer', return_value={
                'status': 'answered', 'text': 'Klaim asing [job#999].', 'provider': 'ollama'}):
            result = self.answer()
        self.assertEqual(result['status'], 'invalid_response')
        self.assertIn('[job#42]', result['text'])
        self.assertNotIn('[job#999]', result['text'])

    def test_long_ai_explanation_falls_back_whole_with_all_selected_sources(self):
        for job in range(1, 6):
            self.add(job, url=f'https://multiple.id/{job}')
        with patch.object(analyst, 'generate_answer', return_value={
                'status': 'answered', 'text': 'a' * 2975 + ' [job#1].', 'provider': 'ollama'}):
            result = self.answer(('multiple.id',))
        self.assertEqual(result['status'], 'invalid_response')
        self.assertLessEqual(len(result['text']), 3800)
        for job in range(1, 6):
            self.assertIn(f'[job#{job}]', result['text'])

    def test_actual_private_conversation_reads_evidence_without_network_or_writes(self):
        from telegram_analyst import AnalystService
        self.add(status='inconclusive', reason='short_or_empty_text')
        before = self.db.read_bytes()
        service = AnalystService(self.db, environ={'BIMA_ANALYST_PROVIDER': 'disabled'})
        self.addCleanup(service.close)
        allowed = {'42'}
        replies = []
        with patch.dict('os.environ', {'TELEGRAM_REVIEW_USER_IDS': '42'}), \
                patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('Unexpected network')):
            for update, text in ((1, '/analisis #42'), (2, '/tanya Apa keterbatasannya?')):
                response = service.handle(text, '42', '42', allowed,
                                          update_id=update, chat_type='private')
                self.assertIn('diterima', response)
                delivered = threading.Event()

                def receive(chat, answer):
                    replies.append((chat, answer))
                    delivered.set()

                deadline = time.monotonic() + 3
                while not delivered.is_set() and time.monotonic() < deadline:
                    service.deliver(receive, allowed)
                    if not delivered.is_set():
                        delivered.wait(.005)
                self.assertTrue(delivered.is_set(), 'Real analyst pipeline did not deliver')
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(chat == '42' and '[job#42]' in text for chat, text in replies))
        self.assertEqual(before, self.db.read_bytes())


if __name__ == '__main__':
    unittest.main()
