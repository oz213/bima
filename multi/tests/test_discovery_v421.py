"""BIMA v4.2.1 canonical-www and robots diagnostics; all offline, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proactive_discovery as d

SEED = 'contoh.id'
APEX = 'https://contoh.id/'
WWW = 'https://www.contoh.id/'


def ok(url: str, body: str = '', status: int = 200):
    return d.FetchResult(status, url, body, '' if status == 200 else 'http_status')


def hop(source: str, target: str, status: int = 301):
    item = d.make_redirect(source, target, seed=SEED, method='http', status=status)
    assert item is not None
    return d.FetchResult(status, source, '', 'redirect_origin_requires_robots', (item,))


class FakeSite:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.pages.get(url, ok(url, status=404))


class CrossOriginSafety(unittest.TestCase):
    def test_fetcher_stops_before_www_connection(self):
        class RedirectResponse:
            status = 301

            def getheader(self, name, default=''):
                return 'https://www.contoh.id/robots.txt' if name == 'Location' else default

        class FakeConn:
            calls = []

            def __init__(self, host, ip, port, timeout):
                self.host = host
                self.calls.append(host)

            def request(self, *a, **kw):
                pass

            def getresponse(self):
                return RedirectResponse()

            def close(self):
                pass

        with patch.object(d, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(d, 'PinnedHTTPSConnection', FakeConn):
            response = d.SafeFetcher(timeout=1, max_bytes=1024,
                                    min_host_interval=0).get(APEX + 'robots.txt', seed=SEED)
        self.assertEqual(FakeConn.calls, ['contoh.id'])
        self.assertEqual(response.error, 'redirect_origin_requires_robots')
        self.assertEqual(response.status, 301)
        self.assertEqual(response.redirects[0].destination_url, WWW + 'robots.txt')

    def test_fetcher_stops_https_to_http_origin_change(self):
        class RedirectResponse:
            status = 302

            def getheader(self, name, default=''):
                return 'http://contoh.id/' if name == 'Location' else default

        class FakeConn:
            calls = 0

            def __init__(self, *args):
                type(self).calls += 1

            def request(self, *a, **kw):
                pass

            def getresponse(self):
                return RedirectResponse()

            def close(self):
                pass

        with patch.object(d, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(d, 'PinnedHTTPSConnection', FakeConn), \
             patch.object(d, 'PinnedHTTPConnection', FakeConn):
            response = d.SafeFetcher(timeout=1, max_bytes=1024,
                                    min_host_interval=0).get(APEX, seed=SEED)
        self.assertEqual(FakeConn.calls, 1)
        self.assertEqual(response.error, 'redirect_origin_requires_robots')
        self.assertEqual(response.redirects[0].destination_url, 'http://contoh.id/')

    def test_external_redirect_remains_passive(self):
        obs = d.make_redirect(APEX, 'https://other.example/landing?token=hidden',
                              seed=SEED, method='http', status=302)
        self.assertTrue(obs.external)
        self.assertNotIn('hidden', str(obs))

    def test_canonical_target_limited_to_www_same_scheme_and_path(self):
        valid = hop(APEX + 'robots.txt', WWW + 'robots.txt')
        self.assertEqual(d.canonical_www_origin(valid, origin=APEX,
                                                source_path='/robots.txt', seed=SEED), WWW)
        for target in ('https://ads.contoh.id/robots.txt',
                       'http://www.contoh.id/robots.txt',
                       'https://www.contoh.id/auth/signin',
                       'https://www.contoh.id/private'):
            with self.subTest(target=target):
                other = hop(APEX + 'robots.txt', target)
                self.assertIsNone(d.canonical_www_origin(other, origin=APEX,
                                  source_path='/robots.txt', seed=SEED))
        self.assertIsNone(d.canonical_www_origin(valid, origin=APEX,
                                                source_path='/', seed=SEED))


class CanonicalRobotsTests(unittest.TestCase):
    def robots_redirect(self):
        return hop(APEX + 'robots.txt', WWW + 'robots.txt')

    def test_www_robots_checked_independently_before_homepage(self):
        site = FakeSite({
            APEX + 'robots.txt': self.robots_redirect(),
            WWW + 'robots.txt': ok(WWW + 'robots.txt', status=404),
            WWW: ok(WWW, '<a href="/berita/">Berita</a>'),
        })
        urls, counters = d.discover_seed(SEED, fetch=site, canonical_www=True,
                                         max_sitemaps=0)
        self.assertEqual(site.calls, [APEX + 'robots.txt', WWW + 'robots.txt', WWW])
        self.assertEqual([x.url for x in urls], [WWW + 'berita/'])
        self.assertEqual(counters['canonical_www_attempts'], 1)
        self.assertEqual(counters['canonical_www_verified'], 1)
        self.assertEqual(counters['canonical_www_homepage_success'], 1)
        self.assertEqual(counters['web_errors'], 0)
        self.assertEqual(counters['diagnostic']['selected_origin'], WWW)
        self.assertEqual(counters['robots_reasons'],
                         {'robots_origin_redirect': 1, 'robots_not_found': 1})

    def test_canonical_disabled_default_does_not_visit_www(self):
        site = FakeSite({APEX + 'robots.txt': self.robots_redirect()})
        urls, counters = d.discover_seed(SEED, fetch=site, max_sitemaps=0)
        self.assertEqual(urls, [])
        self.assertEqual(site.calls, [APEX + 'robots.txt'])
        self.assertEqual(counters['canonical_www_attempts'], 0)

    def test_www_denied_blocks_homepage(self):
        for www_robots in (
            ok(WWW + 'robots.txt', status=403),
            ok(WWW + 'robots.txt', 'User-agent: *\nDisallow: /\n'),
            d.FetchResult(0, WWW + 'robots.txt', '', 'TimeoutError'),
            d.FetchResult(200, WWW + 'auth/signin', '<html>login</html>'),
        ):
            with self.subTest(robots=www_robots):
                site = FakeSite({APEX + 'robots.txt': self.robots_redirect(),
                                 WWW + 'robots.txt': www_robots})
                urls, counters = d.discover_seed(SEED, fetch=site,
                                                 canonical_www=True, max_sitemaps=0)
                self.assertEqual(urls, [])
                self.assertEqual(site.calls, [APEX + 'robots.txt', WWW + 'robots.txt'])
                self.assertEqual(counters['canonical_www_verified'], 0)
                self.assertEqual(counters['canonical_www_blocked'], 1)

    def test_www_robots_login_response_is_not_permission(self):
        site = FakeSite({APEX + 'robots.txt': self.robots_redirect(),
                         WWW + 'robots.txt': d.FetchResult(200, WWW + 'auth/signin', '<html></html>')})
        _, stats = d.discover_seed(SEED, fetch=site, canonical_www=True)
        self.assertEqual(stats['robots_reasons']['robots_origin_or_path_mismatch'], 1)
        self.assertEqual(stats['canonical_www_blocked'], 1)

    def test_apex_robots_404_then_homepage_redirect_www(self):
        site = FakeSite({
            APEX + 'robots.txt': ok(APEX + 'robots.txt', status=404),
            APEX: hop(APEX, WWW),
            WWW + 'robots.txt': ok(WWW + 'robots.txt', status=404),
            WWW: ok(WWW, '<a href="/about/">About</a>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, canonical_www=True,
                                      max_sitemaps=0)
        self.assertEqual(site.calls, [APEX + 'robots.txt', APEX,
                                      WWW + 'robots.txt', WWW])
        self.assertEqual([x.url for x in urls], [WWW + 'about/'])
        self.assertEqual(stats['canonical_www_verified'], 1)

    def test_apex_to_www_homepage_robots_disallow(self):
        site = FakeSite({
            APEX + 'robots.txt': ok(APEX + 'robots.txt', status=404),
            APEX: hop(APEX, WWW),
            WWW + 'robots.txt': ok(WWW + 'robots.txt', 'User-agent: *\nDisallow: /\n'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, canonical_www=True,
                                      max_sitemaps=0)
        self.assertEqual(urls, [])
        self.assertNotIn(WWW, site.calls)
        self.assertEqual(stats['canonical_www_blocked'], 1)

    def test_www_robots_filters_links_and_probe(self):
        site = FakeSite({
            APEX + 'robots.txt': self.robots_redirect(),
            WWW + 'robots.txt': ok(WWW + 'robots.txt',
                                   'User-agent: *\nDisallow: /private\nAllow: /\n'),
            WWW: ok(WWW, '<a href="/private/secret">blocked</a>'
                            '<a href="/public">ok</a>'),
            WWW + 'public': ok(WWW + 'public', '<html>safe</html>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, canonical_www=True,
                                      max_sitemaps=0, probe_candidates=2)
        self.assertEqual([x.url for x in urls], [WWW + 'public'])
        self.assertNotIn(WWW + 'private/secret', site.calls)
        self.assertEqual(stats['probed_pages'], 1)

    def test_non_www_subdomain_never_selected(self):
        other = hop(APEX + 'robots.txt', 'https://ads.contoh.id/robots.txt')
        site = FakeSite({APEX + 'robots.txt': other})
        _, stats = d.discover_seed(SEED, fetch=site, canonical_www=True)
        self.assertEqual(site.calls, [APEX + 'robots.txt'])
        self.assertEqual(stats['canonical_www_attempts'], 0)

    def test_https_fallback_then_http_www_robots(self):
        http = 'http://contoh.id/'
        www_http = 'http://www.contoh.id/'
        site = FakeSite({
            APEX + 'robots.txt': d.FetchResult(0, APEX + 'robots.txt', '', 'SSLError'),
            http + 'robots.txt': hop(http + 'robots.txt', www_http + 'robots.txt'),
            www_http + 'robots.txt': ok(www_http + 'robots.txt', status=404),
            www_http: ok(www_http, '<a href="/ok/">ok</a>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, canonical_www=True,
                                      http_fallback=True, max_sitemaps=0)
        self.assertEqual(site.calls, [APEX + 'robots.txt', http + 'robots.txt',
                                      www_http + 'robots.txt', www_http])
        self.assertEqual([x.url for x in urls], [www_http + 'ok/'])
        self.assertEqual(stats['http_fallback_success'], 1)
        self.assertEqual(stats['canonical_www_verified'], 1)

    def test_login_redirected_same_origin_robots_not_grant(self):
        site = FakeSite({
            APEX + 'robots.txt': d.FetchResult(200, APEX + 'auth/signin', '<html>login</html>')
        })
        urls, stats = d.discover_seed(SEED, fetch=site, canonical_www=True)
        self.assertEqual(urls, [])
        self.assertEqual(site.calls, [APEX + 'robots.txt'])
        self.assertEqual(stats['robots_reasons'], {'robots_origin_or_path_mismatch': 1})

    def test_robots_http_status_categories(self):
        for result, expected in (
            (d.FetchResult(200, APEX+'robots.txt', '', 'http_status'), 'robots_http_error'),
            (d.FetchResult(0, APEX+'robots.txt', '', 'gaierror'), 'robots_dns'),
            (d.FetchResult(0, APEX+'robots.txt', '', 'TimeoutError'), 'robots_timeout'),
            (d.FetchResult(0, APEX+'robots.txt', '', 'SSLError'), 'robots_tls'),
            (ok(APEX+'robots.txt', status=403), 'robots_access_denied'),
            (ok(APEX+'robots.txt', status=429), 'robots_rate_limited'),
            (d.FetchResult(200, APEX+'robots.txt', '', 'non_text'), 'robots_invalid_content'),
        ):
            with self.subTest(outcome=expected):
                site = FakeSite({APEX+'robots.txt': result})
                _, stats = d.discover_seed(SEED, fetch=site, max_sitemaps=0)
                self.assertEqual(stats['robots_reasons'], {expected: 1})


class DiagnosticsTests(unittest.TestCase):
    def test_cli_optin_flags(self):
        parsed = d.arg_parser().parse_args([])
        self.assertFalse(parsed.canonical_www)
        self.assertEqual(parsed.diagnostic_seeds, 0)
        for argv in (['--canonical-www'], ['--diagnostic-seeds', '10'],
                     ['--scan-web', '--diagnostic-seeds', '101'],
                     ['--scan-web', '--diagnostic-seeds', '-1'],
                     ['--scan-web', '--diagnostic-seeds', '2', '--apply']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                d.main(argv)

    def test_preview_aggregates_safe_per_seed_diagnostics(self):
        class FakeFetcher:
            def __init__(self, *args, **kwargs):
                self.calls = []

            def get(self, url, *, seed):
                self.calls.append(url)
                if seed == 'one.id':
                    return d.FetchResult(0, url, '', 'TimeoutError')
                return ok(url, status=404) if url.endswith('robots.txt') else ok(url, '<html/>')

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'seeds.json'
            f.write_text(json.dumps(['one.id', 'two.id']))
            args = d.arg_parser().parse_args(['--input', str(f), '--db', str(Path(td)/'absent.db'),
                                             '--scan-web', '--seed-limit', '2',
                                             '--max-sitemaps', '0', '--diagnostic-seeds', '2',
                                             '--request-interval', '0'])
            with patch.object(d, 'SafeFetcher', FakeFetcher):
                result = d.run(args)
        self.assertEqual(result['mode'], 'preview')
        self.assertEqual(len(result['preview_diagnostics']), 2)
        self.assertEqual(result['robots_reasons'],
                         {'robots_timeout': 1, 'robots_not_found': 1})
        self.assertEqual(result['fetch_failure_types'], {'timeout': 1})
        self.assertEqual(result['http_response_codes'].get('404'), 1)
        self.assertEqual([x['seed'] for x in result['preview_diagnostics']],
                         ['one.id', 'two.id'])
        self.assertNotIn('error_message', str(result))
        self.assertEqual(result['new_jobs'], 0)

    def test_no_unbounded_sensitive_diagnostics(self):
        secret = 'MY_SECRET_SESSION_TOKEN'
        item = d.make_redirect(APEX, 'https://outside.example/path?token='+secret,
                               seed=SEED, method='http', status=302)
        site = FakeSite({APEX+'robots.txt': ok(APEX+'robots.txt', status=404),
                         APEX: d.FetchResult(302, APEX, '', 'redirect_out_of_scope', (item,))})
        _, stats = d.discover_seed(SEED, fetch=site, max_sitemaps=0)
        self.assertNotIn(secret, json.dumps(stats))
        self.assertEqual(stats['diagnostic']['redirect_stop_reasons'],
                         {'redirect_out_of_scope': 1})


if __name__ == '__main__':
    unittest.main()
