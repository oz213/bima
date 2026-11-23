"""BIMA v12 passive phishing signals + read-only reporting, offline fixtures."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phishing_signals import enrich_classification
from phishing_signals_v12 import extend_triage, MAX_HTML_CHARS
from phishing_shadow_report_v12 import report, ReportError


ENV = {'BIMA_PHISHING_SHADOW': '1', 'BIMA_PHISHING_V12': '1',
       'BIMA_PHISHING_SHADOW_RATE': '1'}


def scan(html, url='https://example.id/login', env=None):
    return enrich_classification(
        {'status': 'low_risk', 'reason': 'ordinary_site', 'ml_conf': 0.2},
        initial_url='https://example.id/login', current_url=url,
        page_html=html, environ=ENV if env is None else env,
    )


class PhishingV12Signals(unittest.TestCase):
    def test_v12_off_default_preserves_v1_and_original(self):
        original = {'status': 'low_risk', 'reason': 'safe'}
        old = enrich_classification(original, initial_url='https://a.id/',
                                    current_url='https://a.id/',
                                    page_html='<form><input type=password></form>',
                                    environ={'BIMA_PHISHING_SHADOW':'1',
                                             'BIMA_PHISHING_SHADOW_RATE':'1'})
        self.assertEqual(old['phishing_triage']['version'], 'phishing_shadow_v1')
        self.assertEqual(original, {'status': 'low_risk', 'reason': 'safe'})

    def test_otp_only_cross_origin_review_no_credential_leak(self):
        html = ('<form action="https://username:PRIVATE@external.tld/collect?secret=PRIVATE">'
                '<input autocomplete="one-time-code" name="otp" value="PRIVATE"></form>')
        got = scan(html)
        self.assertEqual(got['status'], 'low_risk')
        self.assertEqual(got['reason'], 'ordinary_site')
        triage = got['phishing_triage']
        self.assertEqual(triage['version'], 'phishing_shadow_v12')
        self.assertEqual(triage['status'], 'review_recommended')
        self.assertIn('otp_cross_origin_action', triage['signal_codes'])
        self.assertIn('credential_action_userinfo', triage['signal_codes'])
        self.assertEqual(triage['otp_only_forms_v12'], 1)
        self.assertTrue(triage['review_only'])
        self.assertNotIn('PRIVATE', str(triage))
        self.assertNotIn('external.tld', str(triage))

    def test_legitimate_otp_on_same_origin_not_phishing(self):
        triage = scan('<form action="/otp" method=post><input name=otp></form>')['phishing_triage']
        self.assertEqual(triage['status'], 'otp_form_only')
        self.assertNotIn('otp_cross_origin_action', triage['signal_codes'])

    def test_password_get_method_is_review_only(self):
        triage = scan('<form method="GET" action="/login">'
                      '<input type=password value=SECRET></form>')['phishing_triage']
        self.assertIn('credential_explicit_get_method', triage['signal_codes'])
        self.assertEqual(triage['status'], 'review_recommended')
        self.assertNotIn('SECRET', str(triage))

    def test_http_page_and_ip_literal_signal(self):
        triage = scan('<form method="POST" action="https://203.0.113.9:8888/">'
                      '<input type=password></form>',
                      url='http://example.id/login')['phishing_triage']
        self.assertIn('credential_page_http', triage['signal_codes'])
        self.assertIn('credential_action_ip_literal', triage['signal_codes'])
        self.assertIn('credential_action_nonstandard_port', triage['signal_codes'])
        self.assertNotIn('203.0.113.9', str(triage))

    def test_otp_non_http_action(self):
        triage = scan('<form action="javascript:submit()">'
                      '<input autocomplete="one-time-code"></form>')['phishing_triage']
        self.assertIn('otp_non_http_action', triage['signal_codes'])

    def test_base_and_formaction_override_honored(self):
        triage = scan('<base href="https://other.tld/">'
                      '<form action="/valid"><input type=password>'
                      '<button formaction="https://alice@other.tld/collect">GO</button>'
                      '</form>')['phishing_triage']
        self.assertIn('credential_action_userinfo', triage['signal_codes'])
        self.assertIn('cross_origin_credential_action', triage['signal_codes'])

    def test_malformed_port_no_url_in_metadata(self):
        triage = scan('<form action="https://external.tld:invalid/secret">'
                      '<input type=password></form>')['phishing_triage']
        self.assertIn('credential_action_malformed', triage['signal_codes'])
        self.assertNotIn('external.tld', str(triage))

    def test_ordinary_external_link_not_credential_exfil(self):
        triage = scan('<a href="https://external.tld/?token=PRIVATE">External</a>') ['phishing_triage']
        self.assertEqual(triage['status'], 'insufficient_signal')
        self.assertEqual(triage['signal_codes'], [])

    def test_non_id_initial_url_is_out_of_scope(self):
        result = enrich_classification({'status':'low_risk'},
             initial_url='https://other.tld/login', current_url='https://example.id/login',
             page_html='<form><input type=password></form>', environ=ENV)
        self.assertEqual(result['phishing_triage']['status'], 'out_of_scope')

    def test_bounded_html_and_forms(self):
        html = '<form method=GET><input type=password></form>' * 100 + 'A' * (MAX_HTML_CHARS + 100)
        got = scan(html)['phishing_triage']
        self.assertEqual(got['credential_forms_v12'], 64)
        self.assertTrue(got['truncated'])

    def test_offline_cli_v12_flag_no_network(self):
        import io
        from contextlib import redirect_stdout
        import phishing_signals
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fake.html'
            path.write_text('<form action="https://other.tld"><input name=otp></form>')
            output = io.StringIO()
            with redirect_stdout(output):
                code = phishing_signals.main([
                    '--html-file', str(path), '--url', 'https://example.id/', '--v12'
                ])
            self.assertEqual(code, 0)
            doc = json.loads(output.getvalue())
            self.assertEqual(doc['version'], 'phishing_shadow_v12')
            self.assertIn('otp_cross_origin_action', doc['signal_codes'])

    def test_shadow_rate_zero_never_reads_html(self):
        def fail():
            raise AssertionError('must not call')
        got = enrich_classification({'status':'low_risk'},
             initial_url='https://a.id/', current_url='https://a.id/', page_html=fail,
             environ={**ENV,'BIMA_PHISHING_SHADOW_RATE':'0'})
        self.assertNotIn('phishing_triage', got)

    def test_v12_scanner_error_keeps_v1_observation(self):
        with mock.patch('phishing_signals_v12.extend_triage', side_effect=RuntimeError('SECRET')):
            result = scan('<form action="https://other.tld/login"><input type=password></form>')
        t = result['phishing_triage']
        self.assertEqual(t['version'], 'phishing_shadow_v1')
        self.assertEqual(t['status'], 'review_recommended')
        self.assertEqual(t['v12_error'], 'scanner_error')
        self.assertNotIn('SECRET', str(result))


class ReadOnlyReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'production-synthetic.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''
                CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, classifier_json TEXT);
                CREATE TABLE discovery_history_outcomes_v11 (
                    id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT, classifier_json TEXT);
            ''')
            self._put(conn, 1, json.dumps({'status':'low_risk', 'phishing_triage':
                        {'version':'phishing_shadow_v12','status':'review_recommended',
                         'signal_codes':['otp_cross_origin_action']}}))
            self._put(conn, 2, json.dumps({'status':'high_risk', 'http_classification':
                        {'phishing_triage':{'version':'phishing_shadow_v1',
                                           'status':'login_form_only','signal_codes':[]}}}))
            self._put(conn, 3, '{invalid json SECRET')
            self._put(conn, 4, json.dumps({'status':'low_risk'}))
            conn.execute('INSERT INTO discovery_history_outcomes_v11 VALUES (?,?,?,?)',
                         (1,1,'complete', json.dumps({'phishing_triage':
                          {'version':'phishing_shadow_v12','status':'review_recommended',
                           'signal_codes':['otp_cross_origin_action']}})))
            conn.commit()

    @staticmethod
    def _put(conn, id_, text):
        conn.execute('INSERT INTO jobs VALUES (?,?,?)', (id_,'complete',text))

    def test_summary_and_job_ids_only(self):
        got = report(self.db, source='jobs', limit=10)
        self.assertEqual(got['rows_examined'], 4)
        self.assertEqual(got['with_shadow'], 2)
        self.assertEqual(got['v12_records'], 1)
        self.assertEqual(got['v1_records'], 1)
        self.assertEqual(got['malformed_classifier'], 1)
        self.assertEqual(got['review_job_ids'][0]['job_id'], 1)
        self.assertTrue(got['not_a_phishing_verdict'])
        self.assertNotIn('SECRET', str(got))

    def test_v11_history_is_read_only(self):
        before = self.db.read_bytes()
        got = report(self.db, source='history', limit=5)
        self.assertEqual(got['with_shadow'], 1)
        self.assertEqual(before, self.db.read_bytes())

    def test_nested_http_signals_override_browser_no_html(self):
        with closing(sqlite3.connect(self.db)) as conn:
            self._put(conn, 6, json.dumps({
                'phishing_triage': {'version':'phishing_shadow_v12',
                                    'status':'no_html','signal_codes':[]},
                'http_classification': {'phishing_triage': {
                    'version':'phishing_shadow_v12', 'status':'review_recommended',
                    'signal_codes':['credential_action_ip_literal']}}
            }))
            conn.commit()
        got = report(self.db)
        self.assertEqual(got['triage_statuses']['review_recommended'], 2)
        self.assertIn('credential_action_ip_literal', got['signal_codes'])

    def test_query_only_connection(self):
        import phishing_shadow_report_v12 as module
        with mock.patch.object(module.sqlite3, 'connect', wraps=sqlite3.connect) as conn:
            report(self.db)
        self.assertIn('mode=ro', conn.call_args.args[0])

    def test_limits_and_missing_db_fail_closed(self):
        for bad in (0,-1,2001):
            with self.assertRaises(ReportError):
                report(self.db, limit=bad)
        with self.assertRaises(ReportError):
            report(self.db.parent/'missing.db')

    def test_do_not_assume_unobserved_is_safe(self):
        got = report(self.db)
        self.assertEqual(got['without_shadow'], 1)
        self.assertNotIn('safe', got['triage_statuses'])

    def test_malicious_signal_code_sanitized(self):
        with closing(sqlite3.connect(self.db)) as conn:
            self._put(conn, 5, json.dumps({'phishing_triage':
              {'version':'phishing_shadow_v12', 'status':'review_recommended',
               'signal_codes':['good_code','https://secret.invalid/x', 'BAD']}}))
            conn.commit()
        got = report(self.db)
        self.assertEqual(got['review_job_ids'][0]['signal_codes'], ['good_code'])
        self.assertNotIn('secret.invalid', str(got))


if __name__ == '__main__':
    unittest.main()
