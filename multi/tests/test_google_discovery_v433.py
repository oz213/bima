"""Safe v4.3.3 Google .id Discovery tests: no network / DB / browser."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import google_discovery as gd


class GoogleScopeTests(unittest.TestCase):
    def test_leading_dot_converts_to_search_suffix(self):
        self.assertEqual(gd.normalize_scope('.co.id'), 'co.id')
        self.assertEqual(gd.normalize_scope('.id'), 'id')

    def test_reject_bare_scope(self):
        for s in ('co.id', 'id', 'http://co.id', 'a.id/path', ''):
            with self.subTest(s=s), self.assertRaises(gd.GoogleDiscoveryError):
                gd.normalize_scope(s)

    def test_reject_malformed_scopes(self):
        for s in ('.co.id.', '.co..id', '.-bad.id', '.x_id', '.evil.com',
                  '.co.id\n', '.co.\tid', '.no@@.id'):
            with self.subTest(s=s), self.assertRaises(gd.GoogleDiscoveryError):
                gd.normalize_scope(s)

    def test_scopes_are_unique_and_narrow_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'domains.json'
            path.write_text(json.dumps({'domains': ['.id', '.co.id', '.id', '.web.id']}))
            self.assertEqual(gd.load_scopes(path), ['co.id', 'web.id', 'id'])

    def test_bad_domains_format_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'domains.json'
            for payload in ({'urls': ['.id']}, {'domains': 'abc'}, {'domains': []}):
                path.write_text(json.dumps(payload))
                with self.assertRaises(gd.GoogleDiscoveryError):
                    gd.load_scopes(path)

    def test_host_match_uses_dot_boundary(self):
        self.assertTrue(gd.hostname_matches_scope('x.co.id', 'co.id'))
        self.assertTrue(gd.hostname_matches_scope('x.sub.co.id', 'co.id'))
        self.assertFalse(gd.hostname_matches_scope('co.id', 'co.id'))
        self.assertFalse(gd.hostname_matches_scope('xco.id', 'co.id'))
        self.assertFalse(gd.hostname_matches_scope('x.co.id.evil.com', 'co.id'))

    def test_best_scope_prioritizes_specific_suffix(self):
        scopes = ['co.id', 'web.id', 'id']
        self.assertEqual(gd.best_scope('a.co.id', scopes), '.co.id')
        self.assertEqual(gd.best_scope('a.id', scopes), '.id')
        self.assertIsNone(gd.best_scope('evil.com', scopes))

    def test_candidate_normalizes_and_redacts_query(self):
        got = gd.normalize_candidate('https://x.co.id/path?token=SECRET#frag',
                                     'co.id', ['co.id', 'id'])
        self.assertEqual(got, ('https://x.co.id/path', '.co.id'))

    def test_candidate_cross_scope_rejected(self):
        for value in ('https://x.web.id/a', 'https://x.co.id.evil.com/a',
                      'http://127.0.0.1/a', 'file:///etc/passwd',
                      'https://x.co.id/logout'):
            with self.subTest(value=value):
                self.assertIsNone(gd.normalize_candidate(value, 'co.id', ['co.id', 'id']))

    def test_invalid_credentials_in_candidate_rejected(self):
        self.assertIsNone(gd.normalize_candidate(
            'https://user:password@x.co.id/path', 'co.id', ['co.id', 'id']))

    def test_general_id_scope_can_match_narrow_suffix(self):
        self.assertEqual(gd.normalize_candidate(
            'https://a.co.id/path', 'id', ['co.id', 'id']),
            ('https://a.co.id/path', '.co.id'))

    def test_query_quotes_terms(self):
        self.assertEqual(gd.google_query('co.id', ['judi online', 'slot']),
                         'site:co.id ("judi online" OR "slot")')
        self.assertEqual(gd.google_query('id', []), 'site:.id')

    def test_clean_keywords_prevents_operator_injection(self):
        self.assertEqual(gd.clean_keywords(['slot', 'site:evil.com', 'slot'], 3),
                         ['slot', 'site evil com'])


class ProviderTests(unittest.TestCase):
    def test_http_call_is_bounded_and_does_not_follow_redirects(self):
        session = mock.Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {
            'items': [{'link': 'https://a.co.id/a'}, {'link': 'https://b.co.id/b'}]}
        provider = gd.GoogleCSEProvider('SUPER_SECRET', 'CX_PRIVATE', 7.0, session=session)
        self.assertEqual(provider.search('site:co.id slot', 1), ['https://a.co.id/a'])
        opts = session.get.call_args
        self.assertEqual(opts.args[0], gd.API_ENDPOINT)
        self.assertFalse(opts.kwargs['allow_redirects'])
        self.assertEqual(opts.kwargs['params']['num'], 1)
        self.assertEqual(opts.kwargs['params']['start'], 1)

    def test_error_does_not_leak_api_key(self):
        session = mock.Mock()
        session.get.side_effect = RuntimeError('https://google.test/?key=SUPER_SECRET')
        provider = gd.GoogleCSEProvider('SUPER_SECRET', 'CX_PRIVATE', 7, session=session)
        with self.assertRaises(gd.GoogleDiscoveryError) as ctx:
            provider.search('site:.id', 10)
        self.assertNotIn('SUPER_SECRET', str(ctx.exception))

    def test_http_403_is_sanitized(self):
        session = mock.Mock()
        session.get.return_value.status_code = 403
        provider = gd.GoogleCSEProvider('SUPER_SECRET', 'CX_PRIVATE', 7, session=session)
        with self.assertRaisesRegex(gd.GoogleDiscoveryError, 'HTTP status 403'):
            provider.search('site:.id', 10)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.domains = root / 'domains.json'
        self.config = root / 'config.json'
        self.domains.write_text(json.dumps({'domains': ['.id', '.co.id', '.web.id']}))
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot gacor', 'judi online'],
            'GOOGLE_API_KEY': 'SECRET', 'GOOGLE_CX': 'PRIVATE_CX'}))
        self.args = argparse.Namespace(domains=str(self.domains), config=str(self.config),
            scope_limit=14, max_keywords=6, preview_limit=20,
            execute_google_api=False, acknowledge_quota=False,
            max_api_requests=2, results_per_request=10,
            request_interval=0.0, timeout=8.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_plan_makes_no_search_calls(self):
        provider = mock.Mock()
        result = gd.preview(self.args, provider=provider)
        provider.search.assert_not_called()
        self.assertEqual(result['mode'], 'plan')
        self.assertEqual(result['new_jobs'], 0)
        self.assertEqual(result['db_writes'], 0)
        self.assertFalse(result['browser_launched'])
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(result['scopes'], ['.co.id', '.web.id', '.id'])
        self.assertEqual(result['queries'][0]['query'],
                         'site:co.id ("slot gacor" OR "judi online")')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_execute_requires_quota_acknowledgement(self):
        self.args.execute_google_api = True
        with self.assertRaisesRegex(gd.GoogleDiscoveryError, 'acknowledge-quota'):
            gd.preview(self.args, provider=mock.Mock())

    def test_mock_search_scoped_dedup_and_redaction(self):
        self.args.execute_google_api = True
        self.args.acknowledge_quota = True
        self.args.max_api_requests = 2
        provider = mock.Mock()
        provider.search.side_effect = [
            ['https://a.co.id/path?secret=1', 'https://a.co.id/path?secret=2',
             'https://x.web.id/path', 'https://a.co.id.evil.com/path',
             'https://a.co.id/login'],
            ['https://a.web.id/slot', 'https://a.co.id/slot'],
        ]
        result = gd.preview(self.args, provider=provider)
        self.assertTrue(result['ok'])
        self.assertEqual(result['api_requests'], 2)
        self.assertEqual(result['raw_results'], 7)
        self.assertEqual(result['duplicate_results'], 1)
        self.assertEqual(result['outside_scope_or_invalid'], 3)
        self.assertEqual(result['valid_results'], 3)
        self.assertEqual(result['db_writes'], 0)
        urls = [x['url'] for x in result['preview_candidates']]
        self.assertIn('https://a.co.id/path', urls)
        self.assertIn('https://a.co.id/login', urls)
        self.assertIn('https://a.web.id/slot', urls)
        self.assertNotIn('secret=1', json.dumps(result))
        self.assertEqual(result['preview_candidates'][0]['evidence_level'],
                         'search_index_signal')

    def test_http_error_stops_remaining_requests_without_secret_leak(self):
        self.args.execute_google_api = True
        self.args.acknowledge_quota = True
        provider = mock.Mock()
        provider.search.side_effect = [gd.GoogleDiscoveryError('Google CSE HTTP status 429')]
        result = gd.preview(self.args, provider=provider)
        self.assertFalse(result['ok'])
        self.assertIn('429', result['provider_error'])
        self.assertEqual(provider.search.call_count, 1)
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(result['successful_requests'], 0)

    def test_expiration_disables_google_api(self):
        self.args.execute_google_api = True
        self.args.acknowledge_quota = True
        future = datetime_stub(2027, 1, 1)
        with mock.patch.object(gd, 'datetime', future):
            with self.assertRaisesRegex(gd.GoogleDiscoveryError, 'end date'):
                gd.preview(self.args, provider=mock.Mock())

    def test_google_api_execution_requires_keywords(self):
        self.args.execute_google_api = True
        self.args.acknowledge_quota = True
        self.config.write_text(json.dumps({
            'GOOGLE_API_KEY': 'SECRET', 'GOOGLE_CX': 'PRIVATE_CX',
            'QUERY_KEYWORDS': []}))
        with self.assertRaisesRegex(gd.GoogleDiscoveryError, 'QUERY_KEYWORDS'):
            gd.preview(self.args, provider=mock.Mock())

    def test_execute_honors_one_api_request(self):
        self.args.execute_google_api = True
        self.args.acknowledge_quota = True
        self.args.max_api_requests = 1
        provider = mock.Mock()
        provider.search.return_value = []
        result = gd.preview(self.args, provider=provider)
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(provider.search.call_count, 1)


class datetime_stub:
    def __init__(self, year, month, day):
        self.year, self.month, self.day = year, month, day

    @classmethod
    def now(cls, tz=None):
        class T:
            @staticmethod
            def date():
                return gd.GOOGLE_CSE_END
        return T()


if __name__ == '__main__':
    unittest.main()
