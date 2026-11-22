"""No-network tests for optional Telegram investigation worker."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location('telegram_investigation', SRC / 'telegram_investigation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TelegramInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {'12345'}
        self.clean = patch.dict(module.os.environ, {'TELEGRAM_REVIEW_USER_IDS': '12345'})
        self.clean.start()
        self.addCleanup(self.clean.stop)

    @staticmethod
    def fake_report(job=None):
        return {'target': 'https://sorothariini.id/',
                'saved_job': job,
                'observation': {'http_status': 200, 'final_url': 'https://sorothariini.id/',
                                'category': 'hidden_gambling_seo_spam',
                                'assessment': 'strong_indicator_review_required',
                                'reason': 'hidden_external_links_with_gambling_promotion_terms',
                                'hidden_gambling_links_count': 9,
                                'outbound_promotion_links_count': 0,
                                'hidden_gambling_links_sample': [{'anchor_text': 'slot gacor',
                                    'destination_host': 'unrelated.example',
                                    'hidden_method': 'style:display:none'}],
                                'redirect_chain': ['https://sorothariini.id/']}}

    def test_private_reviewer_only(self):
        calls = []
        s = module.InvestigationService('/tmp/no-db', runner=lambda *args: calls.append(args))
        self.assertEqual(s.handle('/investigasi sorothariini.id', '-123', '12345', {'-123'},
                                  chat_type='group'), module.DENIED)
        self.assertEqual(s.handle('/investigasi sorothariini.id', '12345', '678', self.allowed,
                                  chat_type='private'), module.DENIED)
        self.assertEqual(calls, [])
        s.close()

    def test_invalid_url_and_scheme_rejected(self):
        s = module.InvestigationService('/tmp/no-db', runner=lambda *args: self.fail('unexpected run'))
        for value in ('http://127.0.0.1/', 'https://foo.id:8080/',
                      'http://foo.com/', 'file:///etc/passwd',
                      'http://foo.id\\@127.0.0.1', 'http://user:pwd@foo.id/',
                      'http://foo.id/a b'):
            with self.subTest(value=value):
                self.assertTrue(any(message in s.handle('/investigasi ' + value,
                                 '12345', '12345', self.allowed, chat_type='private')
                                 for message in ('Target tidak valid', 'Gunakan /investigasi')))
        s.close()

    def test_async_report_no_poll_block(self):
        import threading
        started = threading.Event()
        resume = threading.Event()
        def runner(*args):
            started.set()
            resume.wait(1)
            return self.fake_report()
        s = module.InvestigationService('/tmp/no-db', runner=runner)
        self.assertEqual(s.handle('/investigasi sorothariini.id', '12345', '12345',
                                  self.allowed, chat_type='private'), module.ACCEPTED)
        self.assertTrue(started.wait(1))
        self.assertEqual(s.handle('/investigasi jebol.id', '12345', '12345',
                                  self.allowed, chat_type='private'), module.BUSY)
        resume.set()
        for _ in range(100):
            if s._ready:
                break
            time.sleep(0.01)
        output = []
        self.assertEqual(s.deliver(lambda chat, message: output.append((chat, message)),
                                   self.allowed), 1)
        self.assertEqual(output[0][0], '12345')
        self.assertIn('Tautan judi tersembunyi: 9', output[0][1])
        self.assertEqual(s.deliver(lambda *args: None, self.allowed), 0)
        s.close()

    def test_delivery_failure_retries(self):
        s = module.InvestigationService('/tmp/no-db')
        s._ready.append(('12345', '12345', 'a'))
        with self.assertRaises(OSError):
            s.deliver(lambda *_: (_ for _ in ()).throw(OSError('failed')), self.allowed)
        self.assertEqual(len(s._ready), 1)
        out = []
        self.assertEqual(s.deliver(lambda a, b: out.append(b), self.allowed), 1)
        self.assertEqual(out, ['a'])
        s.close()

    def test_job_id_is_read_only_and_lookup_exact(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'jobs.sqlite'
            con = sqlite3.connect(db)
            con.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT)')
            con.execute('INSERT INTO jobs VALUES (?,?)', (47483, 'https://jebol.id/'))
            con.commit()
            con.close()
            target, job = module._parse_target('/investigasi #47483', db)
            self.assertEqual(target, 'https://jebol.id/')
            self.assertEqual(job, 47483)
            with self.assertRaises(module.InvestigationError):
                module._parse_target('/investigasi #1234', db)

    def test_model_disagreement_and_no_html_leak(self):
        job = {'job_id': 47483, 'risk': 'high_risk', 'model_disagreement': True,
               'ml_pred': 'gambling', 'indobert_pred': 'non-gambling'}
        report = self.fake_report(job)
        report['observation']['raw_html'] = '<script>secret</script>'
        text = module.format_report(report)
        self.assertIn('PERBEDAAN MODEL', text)
        self.assertNotIn('secret', text)
        self.assertLess(len(text), 3401)

    def test_inconclusive_never_safe(self):
        report = {'target': 'https://foo.id/', 'observation': {
            'assessment': 'inconclusive', 'category': 'not_determined',
            'reason': 'dns_resolution_failed'}}
        text = module.format_report(report)
        self.assertIn('inconclusive', text)
        self.assertIn('bukan bukti website aman', text)

    def test_cli_uses_bounded_non_shell_subprocess(self):
        out = json.dumps(self.fake_report())
        fake = type('P', (), {'returncode': 0, 'stdout': out, 'stderr': ''})()
        with patch.object(module.subprocess, 'run', return_value=fake) as runner:
            report = module._run_cli('https://sorothariini.id/', 'fake.sqlite3', None, 30)
        self.assertEqual(report['observation']['hidden_gambling_links_count'], 9)
        argv = runner.call_args.args[0]
        self.assertNotIn('--job-id', argv)
        self.assertNotIn('--out', argv)
        self.assertTrue(runner.call_args.kwargs['timeout'] <= 30)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', runner.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
