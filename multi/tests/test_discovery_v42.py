"""BIMA v4.2 reliable discovery tests; local fixtures, no network or DB writes."""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proactive_discovery as d

SEED = 'contoh.id'
HTTPS = 'https://contoh.id/'
HTTP = 'http://contoh.id/'


def ok(url, body='', status=200):
    return d.FetchResult(status, url, body)


def fail(url, error='TimeoutError'):
    return d.FetchResult(0, url, '', error)


class FakeSite:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.pages.get(url, d.FetchResult(404, url, '', 'http_status'))


class FallbackPolicyTests(unittest.TestCase):
    def test_default_off_keeps_original_behavior(self):
        site = FakeSite({HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt')})
        candidates, stats = d.discover_seed(SEED, fetch=site, max_sitemaps=0)
        self.assertEqual(candidates, [])
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['http_fallback_attempts'], 0)
        self.assertEqual(stats['web_errors'], 1)
        self.assertEqual(stats['fetch_failure_types'], {'timeout': 1})

    def test_https_timeout_falls_back_after_separate_http_robots(self):
        site = FakeSite({
            HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=404),
            HTTP: ok(HTTP, '<a href="/promo/">Promo</a>'),
            HTTP + 'sitemap.xml': ok(HTTP + 'sitemap.xml', '<urlset/>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                      max_sitemaps=1)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTP + 'robots.txt',
                                      HTTP, HTTP + 'sitemap.xml'])
        self.assertEqual([i.url for i in urls], [HTTP + 'promo/'])
        self.assertEqual(stats['http_fallback_attempts'], 1)
        self.assertEqual(stats['http_fallback_success'], 1)
        self.assertEqual(stats['web_errors'], 0)
        self.assertEqual(stats['fetch_failure_types'], {'timeout': 1})

    def test_http_robots_403_prevents_http_homepage(self):
        site = FakeSite({
            HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt', 'ConnectionRefusedError'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=403),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTP + 'robots.txt'])
        self.assertEqual(stats['http_fallback_blocked'], 1)
        self.assertEqual(stats['http_fallback_success'], 0)
        self.assertEqual(stats['fetch_failure_types'],
                         {'connection': 1, 'access_denied': 1})

    def test_http_robots_disallow_is_respected(self):
        site = FakeSite({
            HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', 'User-agent: *\nDisallow: /\n'),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTP + 'robots.txt'])
        self.assertEqual(stats['http_fallback_blocked'], 1)

    def test_https_robots_disallow_cannot_be_bypassed(self):
        site = FakeSite({HTTPS + 'robots.txt': ok(
            HTTPS + 'robots.txt', 'User-agent: *\nDisallow: /\n')})
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['http_fallback_attempts'], 0)

    def test_https_robots_403_does_not_fallback(self):
        site = FakeSite({HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=403)})
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['fetch_failure_types'], {'access_denied': 1})

    def test_https_dns_failure_does_not_trigger_http_probe(self):
        site = FakeSite({HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt', 'gaierror')})
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['fetch_failure_types'], {'dns': 1})
        self.assertEqual(stats['http_fallback_attempts'], 0)

    def test_private_dns_failure_does_not_trigger_http_probe(self):
        site = FakeSite({HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt', 'ValueError')})
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['fetch_failure_types'], {'unsafe_or_invalid': 1})
        self.assertEqual(stats['http_fallback_attempts'], 0)

    def test_external_redirect_does_not_trigger_http_fallback(self):
        obs = d.make_redirect(HTTPS + 'robots.txt', 'https://other.example/landing?token=SECRET',
                              seed=SEED, method='http', status=302)
        site = FakeSite({HTTPS + 'robots.txt': d.FetchResult(
            302, HTTPS + 'robots.txt', '', 'redirect_out_of_scope', (obs,))})
        seen = []
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                    redirect_observer=seen.append)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['http_fallback_attempts'], 0)
        self.assertEqual(stats['external_redirects'], 1)
        self.assertEqual(seen[0].destination_url, 'https://other.example/landing')
        self.assertNotIn('SECRET', str(seen))

    def test_https_homepage_timeout_checks_http_robots_before_fallback(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=404),
            HTTPS: fail(HTTPS, 'TimeoutError'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=404),
            HTTP: ok(HTTP, '<a href="/about/">About</a>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                      max_sitemaps=0)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS,
                                      HTTP + 'robots.txt', HTTP])
        self.assertEqual([i.url for i in urls], [HTTP + 'about/'])
        self.assertEqual(stats['http_fallback_attempts'], 1)
        self.assertEqual(stats['http_fallback_success'], 1)

    def test_https_homepage_timeout_http_robots_denied(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=404),
            HTTPS: fail(HTTPS, 'TimeoutError'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=403),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS, HTTP + 'robots.txt'])
        self.assertEqual(stats['http_fallback_success'], 0)
        self.assertEqual(stats['http_fallback_blocked'], 1)

    def test_https_homepage_http_403_is_not_transport_failure(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=404),
            HTTPS: ok(HTTPS, status=403),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                    max_sitemaps=0)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS])
        self.assertEqual(stats['http_fallback_attempts'], 0)

    def test_http_robots_redirect_to_other_seed_subdomain_denied(self):
        site = FakeSite({
            HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt'),
            HTTP + 'robots.txt': ok('http://ads.contoh.id/robots.txt', status=404),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTP + 'robots.txt'])
        self.assertEqual(stats['http_fallback_blocked'], 1)
        self.assertEqual(stats['fetch_failure_types'],
                         {'timeout': 1, 'unsafe_or_invalid': 1})

    def test_https_sitemap_is_not_used_for_http_origin(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', 'User-agent: *\n'
                                    'Sitemap: https://contoh.id/from-https.xml\n'),
            HTTPS: fail(HTTPS),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=404),
            HTTP: ok(HTTP, '<html/>'),
            HTTP + 'sitemap.xml': ok(HTTP + 'sitemap.xml', '<urlset/>'),
        })
        d.discover_seed(SEED, fetch=site, http_fallback=True, max_sitemaps=1)
        self.assertIn(HTTP + 'sitemap.xml', site.calls)
        self.assertNotIn(HTTPS + 'from-https.xml', site.calls)

    def test_http_fallback_disabled_by_default_in_parser(self):
        args = d.arg_parser().parse_args([])
        self.assertFalse(args.http_fallback)
        self.assertEqual(args.scan_web, False)
        with self.assertRaises(SystemExit):
            d.main(['--http-fallback'])


class ClassificationTests(unittest.TestCase):
    def test_classify_error_by_type_no_sensitive_message(self):
        self.assertEqual(d.fetch_failure_kind(fail(HTTPS, 'SSLCertVerificationError')), 'tls')
        self.assertEqual(d.fetch_failure_kind(fail(HTTPS, 'gaierror')), 'dns')
        self.assertEqual(d.fetch_failure_kind(fail(HTTPS, 'TimeoutError')), 'timeout')
        self.assertEqual(d.fetch_failure_kind(ok(HTTPS, status=429)), 'rate_limited')
        self.assertEqual(d.fetch_failure_kind(ok(HTTPS, status=403)), 'access_denied')
        self.assertEqual(d.fetch_failure_kind(fail(HTTPS, 'ValueError')), 'unsafe_or_invalid')

    def test_priority_evidence_only_for_http_meta_redirect(self):
        http_obs = d.make_redirect(HTTP + 'promo/', 'https://outside.example/landing?token=BAD',
                                   seed=SEED, method='http', status=302)
        js_obs = d.make_redirect(HTTP + 'about/', 'https://outside.example/landing',
                                 seed=SEED, method='js_literal')
        site = FakeSite({
            HTTPS + 'robots.txt': fail(HTTPS + 'robots.txt'),
            HTTP + 'robots.txt': ok(HTTP + 'robots.txt', status=404),
            HTTP: ok(HTTP, '<a href="/promo/">Promo</a><a href="/about/">About</a>'),
            HTTP + 'promo/': d.FetchResult(302, HTTP + 'promo/', '', 'redirect_out_of_scope', (http_obs,)),
            HTTP + 'about/': d.FetchResult(200, HTTP + 'about/', '<html/>', '', (js_obs,)),
        })
        urls, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                      max_sitemaps=0, probe_candidates=2)
        by_url = {x.url: x.priority_score for x in urls}
        self.assertGreater(by_url[HTTP + 'promo/'], by_url[HTTP + 'about/'])
        self.assertEqual(stats['external_redirects'], 2)
        self.assertEqual(stats['probed_pages'], 2)
        self.assertNotIn('BAD', str(urls))
        self.assertNotIn('https://outside.example/', str(urls))

    def test_robots_policy_blocks_disallowed_probe(self):
        robots = 'User-agent: *\nDisallow: /private\n'
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', robots),
            HTTPS: ok(HTTPS, '<a href="/private/bonus/">one</a>'
                              '<a href="/public/">two</a>'),
            HTTPS + 'public/': ok(HTTPS + 'public/', '<html/>'),
        })
        urls, stats = d.discover_seed(SEED, fetch=site,
                                      max_sitemaps=0, probe_candidates=2)
        self.assertEqual(stats['probed_pages'], 1)
        self.assertIn(HTTPS + 'public/', site.calls)
        self.assertNotIn(HTTPS + 'private/bonus/', site.calls)
        self.assertEqual([item.url for item in urls], [HTTPS + 'public/'])


class OriginPolicySafetyTests(unittest.TestCase):
    def test_robots_http_200_non_text_is_denied(self):
        robots = d.FetchResult(200, HTTPS + 'robots.txt', '', 'non_text')
        site = FakeSite({HTTPS + 'robots.txt': robots})
        urls, stats = d.discover_seed(SEED, fetch=site, http_fallback=True)
        self.assertEqual(urls, [])
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['fetch_failure_types'], {'non_text': 1})
        self.assertEqual(stats['http_fallback_attempts'], 0)
        self.assertEqual(stats['web_errors'], 1)

    def test_robots_http_200_oversize_is_denied(self):
        robots = d.FetchResult(200, HTTPS + 'robots.txt', '', 'oversize')
        site = FakeSite({HTTPS + 'robots.txt': robots})
        _, stats = d.discover_seed(SEED, fetch=site)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt'])
        self.assertEqual(stats['fetch_failure_types'], {'oversize': 1})

    def test_sitemap_disallowed_by_robots_never_fetched(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt',
                'User-agent: *\nDisallow: /secret-map.xml\n'
                'Sitemap: https://contoh.id/secret-map.xml\n'),
            HTTPS: ok(HTTPS, '<html/>'),
        })
        _, stats = d.discover_seed(SEED, fetch=site, max_sitemaps=2)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS])
        self.assertEqual(stats['web_requests'], 2)

    def test_sitemap_different_origin_is_not_fetched(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt',
                'User-agent: *\n'
                'Sitemap: https://www.contoh.id/sitemap.xml\n'),
            HTTPS: ok(HTTPS, '<html/>'),
        })
        d.discover_seed(SEED, fetch=site, max_sitemaps=2)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS])

    def test_200_non_text_homepage_is_recorded_as_error(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=404),
            HTTPS: d.FetchResult(200, HTTPS, '', 'non_text'),
        })
        _, stats = d.discover_seed(SEED, fetch=site, max_sitemaps=0)
        self.assertEqual(stats['web_errors'], 1)
        self.assertEqual(stats['fetch_failure_types'], {'non_text': 1})

    def test_http_fallback_does_not_run_on_http_200_non_text(self):
        site = FakeSite({
            HTTPS + 'robots.txt': ok(HTTPS + 'robots.txt', status=404),
            HTTPS: d.FetchResult(200, HTTPS, '', 'non_text'),
        })
        _, stats = d.discover_seed(SEED, fetch=site, http_fallback=True,
                                    max_sitemaps=0)
        self.assertEqual(site.calls, [HTTPS + 'robots.txt', HTTPS])
        self.assertEqual(stats['http_fallback_attempts'], 0)


class AggregationTests(unittest.TestCase):
    def test_cli_preview_aggregates_error_types_across_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / 'urls.json'
            input_file.write_text(json.dumps(['contoh.id', 'lain.id']), encoding='utf-8')
            args = d.arg_parser().parse_args([
                '--input', str(input_file), '--db', str(Path(tmp) / 'missing.sqlite3'),
                '--scan-web', '--http-fallback', '--seed-limit', '2',
                '--max-sitemaps', '0', '--request-interval', '0',
            ])
            def fake_get(_fetcher, url, *, seed):
                if seed == 'lain.id':
                    return fail(url, 'gaierror')
                if url.startswith('https://'):
                    return fail(url, 'TimeoutError')
                if url.endswith('/robots.txt'):
                    return ok(url, status=404)
                return ok(url, '<html/>')
            with patch.object(d.SafeFetcher, 'get', fake_get):
                result = d.run(args)
        self.assertEqual(result['mode'], 'preview')
        self.assertEqual(result['fetch_failure_types'], {'timeout': 1, 'dns': 1})
        self.assertEqual(result['http_fallback_attempts'], 1)
        self.assertEqual(result['http_fallback_success'], 1)
        self.assertEqual(result['new_jobs'], 0)


if __name__ == '__main__':
    unittest.main()
