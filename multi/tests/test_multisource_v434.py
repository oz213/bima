"""BIMA v4.3.4 multi-source discovery: synthetic API fixtures, no network."""
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import multi_source_discovery as ms


class FakeResponse:
    def __init__(self, payload=None, status=200, raw=None, chunks=None):
        self.status_code = status
        self.chunks = chunks if chunks is not None else [
            (json.dumps(payload if payload is not None else {}).encode('utf-8')
             if raw is None else raw)]
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield from self.chunks

    def close(self):
        self.closed = True


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.domains = root / 'domains.json'
        self.config = root / 'config.json'
        self.state = root / 'state.json'
        self.domains.write_text(json.dumps({'domains': ['.co.id', '.web.id', '.id']}))
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot', 'gacor'],
            'GOOGLE_API_KEY': 'SECRET_GOOGLE', 'GOOGLE_CX': 'CX_SECRET',
            'BRAVE_SEARCH_API_KEY': 'SECRET_BRAVE', 'URLSCAN_API_KEY': 'SECRET_URLSCAN',
        }))
        self.args = argparse.Namespace(
            domains=str(self.domains), config=str(self.config),
            providers='google,brave,urlscan', scope_limit=14, max_keywords=6,
            max_google_requests=3, max_brave_requests=3,
            max_urlscan_requests=3, results_per_request=5,
            preview_limit=500, timeout=8.0, request_interval=0.0,
            state_file='', execute=False, acknowledge_quota=False,
            brave_monthly_ledger=str(root / 'brave-quota.json'),
            brave_monthly_request_cap=900)
        self.now = datetime(2026, 11, 21, 6, tzinfo=timezone.utc)

    def discover(self, providers=None):
        return ms.discover(self.args, providers=providers, now=self.now)


class PlanTests(Fixture):
    def test_all_providers_plan_no_external_calls(self):
        providers = {p: mock.Mock() for p in ms.PROVIDERS}
        data = self.discover(providers)
        self.assertEqual(data['mode'], 'plan')
        self.assertEqual(data['api_requests'], 0)
        self.assertEqual(data['db_writes'], 0)
        self.assertFalse(data['browser_launched'])
        self.assertEqual(len(data['planned_queries']), 9)
        self.assertTrue(all(x['configured'] for x in data['providers'].values()))
        for prov in providers.values():
            prov.search.assert_not_called()
        self.assertNotIn('SECRET_', json.dumps(data))

    def test_keyword_query_remains_slot_or_gacor(self):
        d = self.discover()
        self.assertIn('site:co.id ("slot" OR "gacor")',
                      [x['query'] for x in d['planned_queries']])

    def test_urlscan_query_uses_fielded_search_not_google_syntax(self):
        q = ms.query_for('urlscan', 'co.id', ['slot', 'gacor'])
        self.assertIn('page.domain:co.id', q)
        self.assertIn('page.title:slot', q)
        self.assertIn('task.url:gacor', q)
        self.assertIn('date:>now-7d', q)
        self.assertNotIn('site:', q)

    def test_no_state_write_from_plan_mode(self):
        self.args.state_file = str(self.state)
        self.discover()
        self.assertFalse(self.state.exists())

    def test_provider_subset(self):
        self.args.providers = 'urlscan'
        d = self.discover()
        self.assertEqual(list(d['providers']), ['urlscan'])
        self.assertEqual({x['provider'] for x in d['planned_queries']}, {'urlscan'})

    def test_parse_providers(self):
        self.assertEqual(ms.parse_providers('BRAVE,google'), ['brave', 'google'])
        for val in ('', 'google,google', 'spam', 'google,,brave,brave'):
            with self.subTest(val=val), self.assertRaises(ms.DiscoveryError):
                ms.parse_providers(val)

    def test_rotate_over_full_scope_set(self):
        scopes = ms.load_scopes(self.domains)
        spans = [ms.choose_scopes(scopes, 1, self.now.date() + timedelta(days=i), 'brave')
                 for i in range(3)]
        self.assertEqual(set(x[0] for x in spans), set(scopes))

    def test_bounded_two_requests_rotate_daily(self):
        scopes = ms.load_scopes(self.domains)
        spans = [ms.choose_scopes(scopes, 2, self.now.date() + timedelta(days=i), 'urlscan')
                 for i in range(3)]
        self.assertEqual(set().union(*map(set, spans)), set(scopes))

    def test_plan_does_not_require_brave_or_urlscan_credentials(self):
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot', 'gacor'], 'GOOGLE_API_KEY': 'abc', 'GOOGLE_CX': 'cid'}))
        d = self.discover()
        self.assertFalse(d['providers']['brave']['configured'])
        self.assertTrue(d['providers']['google']['configured'])

    def test_empty_keywords_rejected(self):
        self.config.write_text(json.dumps({'QUERY_KEYWORDS': [], 'GOOGLE_API_KEY': 'abc'}))
        with self.assertRaisesRegex(ms.DiscoveryError, 'QUERY_KEYWORDS'):
            self.discover()

    def test_get_config_credentials_from_environment(self):
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot'],
            'GOOGLE_API_KEY': '${GOOGLE_DISCOVERY_KEY}',
            'GOOGLE_CX': 'cx', 'BRAVE_SEARCH_API_KEY': '${BRAVE_SECRET}',
        }))
        with mock.patch.dict('os.environ', {'GOOGLE_DISCOVERY_KEY': 'SECRET',
                                           'BRAVE_SECRET': 'TOKEN'}):
            d = self.discover()
            self.assertTrue(d['providers']['brave']['configured'])
            self.assertNotIn('TOKEN', json.dumps(d))


class NetworkProviderTests(unittest.TestCase):
    def test_brave_get_request_has_header_and_no_api_token_in_url(self):
        resp = FakeResponse({'web': {'results': [{'url': 'https://a.co.id/a'}]}})
        session = mock.Mock()
        session.get.return_value = resp
        provider = ms.BraveSearchProvider('VERY_SECRET_BRAVE', session=session)
        self.assertEqual(provider.search('site:co.id ("slot" OR "gacor")', 5),
                         ['https://a.co.id/a'])
        call = session.get.call_args
        self.assertEqual(call.args[0], ms.BRAVE_ENDPOINT)
        self.assertEqual(call.kwargs['headers']['X-Subscription-Token'], 'VERY_SECRET_BRAVE')
        self.assertNotIn('VERY_SECRET_BRAVE', str(call.kwargs['params']))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertTrue(call.kwargs['stream'])
        self.assertTrue(resp.closed)

    def test_brave_does_not_trust_non_web_results(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse({'local': {'results': [{'url': 'https://evil.com/'}]}})
        self.assertEqual(ms.BraveSearchProvider('x', session=session).search('site:co.id', 3), [])

    def test_brave_invalid_results(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse({'web': {'results': 'oops'}})
        with self.assertRaisesRegex(ms.DiscoveryError, 'invalid results'):
            ms.BraveSearchProvider('x', session=session).search('q', 5)

    def test_urlscan_reads_original_task_url_not_destination(self):
        items = [{'task': {'url': 'https://real.co.id/slot?token=X'},
                  'page': {'url': 'https://evil.com/login'}},
                 {'task': {}, 'page': {'url': 'https://alt.co.id/a'}},
                 {'task': {'url': 123}}, None]
        session = mock.Mock()
        session.get.return_value = FakeResponse({'results': items})
        p = ms.UrlscanSearchProvider('SECRET_URLSCAN', session=session)
        self.assertEqual(p.search('page.domain:co.id', 5),
                         ['https://real.co.id/slot?token=X', 'https://alt.co.id/a'])
        call = session.get.call_args
        self.assertEqual(call.args[0], ms.URLSCAN_ENDPOINT)
        self.assertEqual(call.kwargs['headers']['API-Key'], 'SECRET_URLSCAN')
        self.assertEqual(call.kwargs['params']['size'], 5)
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_urlscan_invalid_results(self):
        sess = mock.Mock()
        sess.get.return_value = FakeResponse({'results': {}})
        with self.assertRaisesRegex(ms.DiscoveryError, 'invalid results'):
            ms.UrlscanSearchProvider('x', session=sess).search('q', 5)

    def test_provider_requires_key(self):
        with self.assertRaises(ms.DiscoveryError):
            ms.BraveSearchProvider('')
        with self.assertRaises(ms.DiscoveryError):
            ms.UrlscanSearchProvider('')

    def test_http_error_no_secrets_in_exception(self):
        sess = mock.Mock()
        sess.get.return_value = FakeResponse(status=403)
        with self.assertRaises(ms.DiscoveryError) as err:
            ms.BraveSearchProvider('SECRET', session=sess).search('q', 5)
        self.assertIn('403', str(err.exception))
        self.assertNotIn('SECRET', str(err.exception))
        self.assertTrue(sess.get.return_value.closed)

    def test_transport_error_does_not_leak_request_headers(self):
        sess = mock.Mock()
        sess.get.side_effect = RuntimeError('authorization: SECRET_TOKEN')
        with self.assertRaises(ms.DiscoveryError) as err:
            ms.UrlscanSearchProvider('SECRET_TOKEN', session=sess).search('q', 5)
        self.assertNotIn('SECRET_TOKEN', str(err.exception))

    def test_too_large_response_rejected_and_closed(self):
        sess = mock.Mock()
        response = FakeResponse(chunks=[b'X' * (ms.MAX_RESPONSE_BYTES + 1)])
        sess.get.return_value = response
        with self.assertRaisesRegex(ms.DiscoveryError, '1 MiB'):
            ms.BraveSearchProvider('KEY', session=sess).search('q', 5)
        self.assertTrue(response.closed)

    def test_invalid_json_rejected_and_closed(self):
        sess = mock.Mock()
        response = FakeResponse(raw=b'not json')
        sess.get.return_value = response
        with self.assertRaisesRegex(ms.DiscoveryError, 'invalid JSON'):
            ms.BraveSearchProvider('KEY', session=sess).search('q', 5)
        self.assertTrue(response.closed)

    def test_non_json_object_rejected(self):
        sess = mock.Mock()
        sess.get.return_value = FakeResponse(raw=b'[]')
        with self.assertRaisesRegex(ms.DiscoveryError, 'invalid JSON object'):
            ms.UrlscanSearchProvider('KEY', session=sess).search('q', 5)

    def test_unexpected_iter_content_exception_sanitized(self):
        sess = mock.Mock()
        resp = FakeResponse()
        def broken_iter(chunk_size):
            raise RuntimeError('SECRET_K')
            yield None
        resp.iter_content = broken_iter
        sess.get.return_value = resp
        with self.assertRaises(ms.DiscoveryError) as err:
            ms.BraveSearchProvider('SECRET_K', session=sess).search('q', 5)
        self.assertNotIn('SECRET_K', str(err.exception))
        self.assertTrue(resp.closed)


class ExecutionTests(Fixture):
    def test_execute_requires_acknowledge(self):
        self.args.execute = True
        with self.assertRaisesRegex(ms.DiscoveryError, '--acknowledge-quota'):
            self.discover()

    def test_multi_provider_deduplicates_and_preserves_provenance(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        providers = {x: mock.Mock() for x in ms.PROVIDERS}
        providers['google'].search.return_value = [
            'https://x.co.id/slot?private=123', 'https://evil.com/',
            'https://x.co.id/slot?private=456', 'https://x.co.id/logout']
        providers['brave'].search.return_value = [
            'https://x.co.id/slot', 'https://y.co.id/gacor']
        providers['urlscan'].search.return_value = [
            'https://y.co.id/gacor?token=SECRET_URL', 'https://z.co.id/a']
        output = self.discover(providers)
        self.assertTrue(output['ok'])
        self.assertEqual(output['api_requests'], 3)
        self.assertEqual(output['raw_results'], 8)
        self.assertEqual(output['valid_results'], 3)
        self.assertEqual(output['outside_scope_or_invalid'], 2)
        self.assertEqual(output['duplicate_results'], 1)
        self.assertEqual(output['brave_transient_valid_results'], 2)
        self.assertEqual(output['brave_transient_unique_results'], 2)
        self.assertEqual(output['new_jobs'], 0)
        self.assertEqual(output['alerts_sent'], 0)
        self.assertEqual(output['db_writes'], 0)
        self.assertFalse(output['browser_launched'])
        recs = {r['url']: r for r in output['preview_candidates']}
        self.assertEqual(recs['https://x.co.id/slot']['sources'], ['google'])
        self.assertEqual(recs['https://y.co.id/gacor']['sources'], ['urlscan'])
        self.assertEqual(recs['https://z.co.id/a']['evidence_level'], 'historical_scan_metadata')
        self.assertNotIn('SECRET_URL', json.dumps(output))
        self.assertNotIn('private=123', json.dumps(output))

    def test_missing_key_provider_skipped_without_error(self):
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot', 'gacor'],
            'GOOGLE_API_KEY': 'key', 'GOOGLE_CX': 'cx'}))
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        p = mock.Mock()
        p.search.return_value = []
        d = self.discover(providers={'google': p})
        self.assertTrue(d['ok'])
        self.assertEqual(d['api_requests'], 1)
        self.assertEqual(d['providers']['brave']['status'], 'skipped_missing_credentials')
        self.assertEqual(d['providers']['urlscan']['status'], 'skipped_missing_credentials')

    def test_api_429_stops_provider_but_next_one_runs(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        providers = {x: mock.Mock() for x in ms.PROVIDERS}
        providers['google'].search.side_effect = ms.GoogleDiscoveryError('Google CSE HTTP status 429')
        providers['brave'].search.return_value = ['https://a.co.id/slot']
        providers['urlscan'].search.return_value = []
        d = self.discover(providers)
        self.assertFalse(d['ok'])
        self.assertEqual(d['api_requests'], 3)
        self.assertIn('429', d['providers']['google']['error'])
        self.assertEqual(d['providers']['brave']['status'], 'ok')
        self.assertEqual(d['new_candidates'], 0)
        self.assertEqual(d['brave_transient_unique_results'], 1)

    def test_non_curated_exception_redacted(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'brave'
        p = mock.Mock()
        p.search.side_effect = RuntimeError('SECRET_BRAVE')
        d = self.discover(providers={'brave': p})
        self.assertFalse(d['ok'])
        self.assertNotIn('SECRET_BRAVE', json.dumps(d))

    def test_google_expiration_does_not_stop_other_providers(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google,brave'
        self.now = datetime(2027, 1, 2, tzinfo=timezone.utc)
        providers = {x: mock.Mock() for x in ms.PROVIDERS}
        providers['brave'].search.return_value = []
        d = self.discover(providers)
        self.assertTrue(d['ok'])
        self.assertEqual(d['providers']['google']['status'], 'skipped_expired')
        self.assertEqual(d['api_requests'], 1)
        providers['google'].search.assert_not_called()

    def test_no_state_file_when_omitted(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google'
        p = mock.Mock()
        p.search.return_value = ['https://a.co.id/a']
        d = self.discover({'google': p})
        self.assertFalse(d['state_file_updated'])
        self.assertFalse(self.state.exists())

    def test_dedup_state_survives_daily_runs(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google'
        self.args.state_file = str(self.state)
        p = mock.Mock()
        p.search.return_value = ['https://a.co.id/a?token=A']
        d = self.discover({'google': p})
        self.assertEqual(d['new_candidates'], 1)
        self.assertTrue(d['state_file_updated'])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('a.co.id', self.state.read_text())
        second = self.discover({'google': p})
        self.assertEqual(second['new_candidates'], 0)
        self.assertEqual(second['already_seen_candidates'], 1)
        self.assertEqual(second['preview_candidates'], [])

    def test_truncated_records_not_marked_seen(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google'
        self.args.preview_limit = 1
        self.args.state_file = str(self.state)
        p = mock.Mock()
        p.search.return_value = ['https://a.co.id/a', 'https://b.co.id/b']
        first = self.discover({'google': p})
        self.assertTrue(first['preview_truncated'])
        self.assertEqual(first['new_candidates'], 2)
        second = self.discover({'google': p})
        self.assertEqual(second['new_candidates'], 1)
        self.assertEqual(second['preview_candidates'][0]['url'], 'https://b.co.id/b')

    def test_bad_state_and_symlink_rejected(self):
        self.state.write_text('garbage')
        with self.assertRaises(ms.DiscoveryError):
            ms.read_state(self.state)
        self.state.unlink()
        self.state.symlink_to(self.config)
        with self.assertRaisesRegex(ms.DiscoveryError, 'symlink'):
            ms.read_state(self.state)

    def test_empty_valid_results_still_success(self):
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google'
        p = mock.Mock()
        p.search.return_value = []
        d = self.discover({'google': p})
        self.assertTrue(d['ok'])
        self.assertEqual(d['new_candidates'], 0)
        self.assertEqual(d['api_requests'], 1)

    def test_default_api_request_budget_bounded(self):
        self.args.execute = True
        self.args.acknowledge_quota = True
        self.args.providers = 'google,brave,urlscan'
        self.args.max_google_requests = 1
        self.args.max_brave_requests = 1
        self.args.max_urlscan_requests = 1
        providers = {x: mock.Mock() for x in ms.PROVIDERS}
        for p in providers.values():
            p.search.return_value = []
        d = self.discover(providers)
        self.assertEqual(d['api_requests'], 3)
        for p in providers.values():
            self.assertEqual(p.search.call_count, 1)

    def test_cli_default_is_plan_with_zero_network(self):
        with mock.patch.object(ms, 'discover', return_value={'ok': True, 'mode': 'plan'}) as fn:
            with mock.patch('builtins.print'):
                exit_code = ms.main(['--domains', str(self.domains), '--config', str(self.config)])
        self.assertEqual(exit_code, 0)
        self.assertFalse(fn.call_args.args[0].execute)

    def test_cli_rejects_invalid_bounds(self):
        with mock.patch('builtins.print') as log:
            exit_code = ms.main(['--scope-limit', '0'])
        self.assertEqual(exit_code, 1)
        self.assertTrue(log.called)


if __name__ == '__main__':
    unittest.main()
