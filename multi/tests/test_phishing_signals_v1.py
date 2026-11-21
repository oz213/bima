"""Passive detector regression tests. No requests, no secrets or model required."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phishing_signals import inspect_html, enrich_classification, MAX_HTML_CHARS


class PhishingSignalsTests(unittest.TestCase):
    def test_same_origin_password_form_not_a_phishing_verdict(self):
        data = inspect_html('<form action="/login"><input name="username">'
                            '<input type="password" name="password"></form>',
                            'https://safe.id/login')
        self.assertEqual(data['status'], 'login_form_only')
        self.assertEqual(data['password_forms'], 1)
        self.assertNotIn('cross_origin_credential_action', data['signal_codes'])

    def test_cross_origin_password_action_is_review_only(self):
        data = inspect_html('<form action="https://other.tld/collect?token=SECRET">'
                            '<input name="email" type="email"><input type="password" value="SECRET">'
                            '</form>', 'https://example.id/login')
        self.assertEqual(data['status'], 'review_recommended')
        self.assertIn('cross_origin_credential_action', data['signal_codes'])
        self.assertNotIn('SECRET', str(data))
        self.assertNotIn('other.tld', str(data))

    def test_base_href_can_change_relative_action(self):
        data = inspect_html('<base href="https://other.tld/">'
                            '<form action="collect"><input type=password></form>',
                            'https://example.id/')
        self.assertIn('cross_origin_credential_action', data['signal_codes'])

    def test_empty_action_ignores_base_href(self):
        data = inspect_html('<base href="https://other.tld/">'
                            '<form><input type=password></form>',
                            'https://example.id/')
        self.assertNotIn('cross_origin_credential_action', data['signal_codes'])

    def test_submit_button_formaction_override(self):
        data = inspect_html('<form action="/normal"><input type="password">'
                            '<button formaction="https://other.tld/collect">Continue</button>'
                            '</form>', 'https://example.id/login')
        self.assertIn('cross_origin_credential_action', data['signal_codes'])

    def test_input_formaction_override(self):
        data = inspect_html('<form action="/normal"><input type="password">'
                            '<input type="submit" formaction="https://other.tld/collect">'
                            '</form>', 'https://example.id/login')
        self.assertIn('cross_origin_credential_action', data['signal_codes'])

    def test_http_form_action_flagged(self):
        data = inspect_html('<form action="http://example.id/post">'
                            '<input type="password"></form>', 'https://example.id/login')
        self.assertIn('unencrypted_credential_action', data['signal_codes'])

    def test_javascript_form_action_flagged(self):
        data = inspect_html('<form action="javascript:collect()">'
                            '<input type="password"></form>', 'https://example.id/login')
        self.assertIn('non_http_credential_action', data['signal_codes'])

    def test_otp_without_password_not_strong(self):
        data = inspect_html('<form><input name="otp"></form>', 'https://example.id/')
        self.assertEqual(data['status'], 'insufficient_signal')

    def test_no_non_id_seed(self):
        data = inspect_html('<form action="https://other.tld/">'
                            '<input type=password></form>',
                            'https://other.tld/', 'https://other.tld/')
        self.assertEqual(data['status'], 'out_of_scope')

    def test_external_destination_from_id_is_in_scope(self):
        data = inspect_html('<form action="https://another.tld/login">'
                            '<input type=password></form>',
                            'https://landing.tld/login', initial_url='https://seed.id/')
        self.assertEqual(data['status'], 'review_recommended')

    def test_oversized_html_is_bounded(self):
        data = inspect_html('z'*(MAX_HTML_CHARS+10), 'https://example.id/')
        self.assertTrue(data['truncated'])
        self.assertEqual(len(data['html_prefix_sha256']), 64)

    def test_disabled_does_not_call_html_callable(self):
        def bad():
            raise AssertionError('should not be called')
        base = {'status':'low_risk'}
        output = enrich_classification(base, initial_url='https://a.id/',
            current_url='https://a.id/', page_html=bad,
            environ={'BIMA_PHISHING_SHADOW':'0'})
        self.assertIs(output, base)

    def test_shadow_preserves_legacy_verdict(self):
        base = {'status':'low_risk', 'reason':'context', 'ml_conf':0.1}
        output = enrich_classification(base, initial_url='https://a.id/',
            current_url='https://a.id/', page_html='<form action="https://x.tld/">'
            '<input type=password></form>',
            environ={'BIMA_PHISHING_SHADOW':'1', 'BIMA_PHISHING_SHADOW_RATE':'1'})
        self.assertEqual(base, {'status':'low_risk','reason':'context','ml_conf':0.1})
        self.assertEqual(output['status'], 'low_risk')
        self.assertEqual(output['phishing_triage']['status'], 'review_recommended')

    def test_rate_zero_keeps_record_unchanged(self):
        d = {'status':'low_risk'}
        o = enrich_classification(d, initial_url='https://a.id/',
            current_url='https://a.id/', page_html='<form></form>',
            environ={'BIMA_PHISHING_SHADOW':'1','BIMA_PHISHING_SHADOW_RATE':'0'})
        self.assertIs(o,d)

    def test_bad_rate_not_crash(self):
        d = {'status':'low_risk'}
        o = enrich_classification(d, initial_url='https://a.id/',
            current_url='https://a.id/', page_html='',
            environ={'BIMA_PHISHING_SHADOW':'1','BIMA_PHISHING_SHADOW_RATE':'nan'})
        self.assertIs(o,d)

    def test_failure_from_browser_callable_does_not_break_classifier(self):
        def bad(): raise RuntimeError('browser offline')
        d = {'status':'low_risk'}
        o = enrich_classification(d, initial_url='https://a.id/',
            current_url='https://a.id/', page_html=bad,
            environ={'BIMA_PHISHING_SHADOW':'1','BIMA_PHISHING_SHADOW_RATE':'1'})
        self.assertEqual(o['phishing_triage']['status'],'scanner_error')


if __name__ == '__main__':
    unittest.main()
