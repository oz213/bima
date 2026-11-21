"""BIMA v4.3 offline tests: no network, no JS execution, no browser or alert."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proactive_discovery as d
from redirect_intelligence import JsSignal, analyze_html, build_redirect_chains, resolve_static, scan_js

SEED = 'contoh.id'
ROOT = 'https://contoh.id/'


def robots_404(url=ROOT+'robots.txt'):
    return d.FetchResult(404, url, '', 'http_status')


class PassiveJSRecognition(unittest.TestCase):
    def test_concatenated_location(self):
        js = "window.location.href = 'https://' + 'offsite.example' + '/slot?key=secret';"
        self.assertEqual(scan_js(js)[0].kind, 'js_static_concat')
        self.assertEqual(scan_js(js)[0].target, 'https://offsite.example/slot?key=secret')

    def test_base64_decode_literal(self):
        encoded = base64.b64encode(b'https://foreign.example/landing?k=PRIVATE').decode()
        signals = analyze_html(f'<script>window.location.replace(atob("{encoded}"));</script>')
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].kind, 'js_static_decoded')
        self.assertIn('PRIVATE', signals[0].target)

    def test_from_charcode(self):
        numbers = ','.join(str(ord(c)) for c in 'https://outside.example/slot')
        result, kind = resolve_static('String.fromCharCode(' + numbers + ')')
        self.assertEqual(result, 'https://outside.example/slot')
        self.assertEqual(kind, 'decoded')

    def test_escaped_forward_slashes_and_unpadded_base64(self):
        value, kind = resolve_static(r'"https:\/\/outside.example\/slot"')
        self.assertEqual(value, 'https://outside.example/slot')
        unpadded = base64.b64encode(b'https://outside.example/x').decode().rstrip('=')
        value, kind = resolve_static("atob('" + unpadded + "')")
        self.assertEqual(value, 'https://outside.example/x')

    def test_unicode_percent_decode(self):
        signal = scan_js('location.assign(decodeURIComponent("https%3A%2F%2Fsite.example%2Fx"));')
        self.assertEqual(signal[0].target, 'https://site.example/x')

    def test_const_binding_only(self):
        js = 'const REDIRECT = "https://elsewhere.example/p"; window.location.replace(REDIRECT);'
        signal = scan_js(js)
        self.assertEqual(signal[0].kind, 'js_static_constant')
        self.assertEqual(signal[0].target, 'https://elsewhere.example/p')

    def test_document_and_computed_href(self):
        js = '''document.location['href'] = "https://elsewhere.example/a";'''
        self.assertEqual(scan_js(js)[0].kind, 'js_static_computed_literal')
        self.assertEqual(scan_js('document.location.assign("https://outside.example/")')[0].target,
                         'https://outside.example/')

    def test_window_open_self_is_signal_blank_is_not(self):
        self.assertEqual(scan_js('window.open("https://other.example/", "_self")')[0].kind,
                         'js_static_open_self')
        self.assertEqual(scan_js('window.open("https://other.example/", "_blank")'), [])

    def test_dynamic_is_not_a_confirmed_redirect(self):
        self.assertEqual(scan_js('window.location.href = getDestination();'),
                         [JsSignal('js_dynamic')])

    def test_comments_quoted_documentation_are_ignored(self):
        js = '''// location.assign("https://fake.example/");
        /* window.location.href = "https://fake2.example/"; */
        const docs = 'location.replace("https://fake3.example")';
        const value = "https://good.example/";
        '''
        self.assertEqual(scan_js(js), [])

    def test_other_object_property_not_navigation(self):
        self.assertEqual(scan_js('myConfig.location = "https://fake.example/";'), [])

    def test_equals_comparison_and_regex_not_navigation(self):
        self.assertEqual(scan_js('if (window.location.href == "https://not-a-redirect.example/") {}'), [])
        self.assertEqual(scan_js(r'const pattern = /location.href = "https:\/\/example.com"/;'), [])

    def test_no_external_script_fetched_or_executed(self):
        html = '<script src="https://elsewhere.example/app.js"></script>'
        self.assertEqual(analyze_html(html), [])

    def test_unknown_js_escape_not_logged_or_interpreted(self):
        value, kind = resolve_static(r"'https:\qoutside.example'" )
        self.assertIsNone(value)
        self.assertEqual(kind, 'dynamic')

    def test_other_unknown_or_malformed_js_escapes_are_dynamic(self):
        for literal in (r"'https:\zoutside.example'",
                        r"'https:\N{NAME}outside.example'",
                        r"'https:\xG0outside.example'",
                        r"'https:\8outside.example'"):
            with self.subTest(literal=literal):
                self.assertEqual(resolve_static(literal), (None, 'dynamic'))

    def test_escaped_backslash_is_not_mistaken_for_escaped_slash(self):
        # \\/ means a literal backslash followed by a slash, not //.
        value, kind = resolve_static(r"'https:\\/outside.example/'")
        self.assertEqual(value, 'https:\\/outside.example/')
        self.assertEqual(kind, 'literal')
        self.assertEqual(resolve_static(r"'https:\/\/outside.example/'"),
                         ('https://outside.example/', 'literal'))

    def test_reject_invalid_base64_and_large_values(self):
        self.assertIsNone(resolve_static("atob('not@base64')")[0])
        huge = 'a' * 4000
        self.assertIsNone(resolve_static(repr(huge))[0])

    def test_bounds_on_large_html(self):
        html = '<script>' + ('location.assign(getTarget());' * 50000) + '</script>'
        signals = analyze_html(html)
        self.assertLessEqual(len(signals), 16)

    def test_invalid_scheme_not_evidence(self):
        self.assertIsNone(d.make_redirect(ROOT, 'javascript:alert(1)',
                                          seed=SEED, method='js_static_decoded'))
        self.assertIsNone(d.make_redirect(ROOT, 'http://127.0.0.1/',
                                          seed=SEED, method='js_static_decoded'))


class HTTPAndReviewSafety(unittest.TestCase):
    class FakeResponse:
        status = 200
        def __init__(self, status=200, location='', body=b'<html></html>'):
            self.status = status
            self.location = location
            self.body = body
        def getheader(self, key, default=''):
            if key == 'Location':
                return self.location
            if key == 'Content-Type':
                return 'text/html'
            return default
        def read(self, amount):
            return self.body[:amount]

    def test_safe_fetcher_static_js_external_never_fetched(self):
        body = b'''<script>window.location.href = atob('aHR0cHM6Ly9vdXRzaWRlLmV4YW1wbGUvYg==');</script>'''
        response = self.FakeResponse(200, body=body)
        class Conn:
            calls = 0
            def __init__(self, *args):
                type(self).calls += 1
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return response
            def close(self):
                pass
        with patch.object(d, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(d, 'PinnedHTTPSConnection', Conn):
            result = d.SafeFetcher(timeout=1, max_bytes=10000, advanced_js=True,
                                   min_host_interval=0).get(ROOT, seed=SEED)
        self.assertEqual(Conn.calls, 1)
        self.assertEqual(result.redirects[0].destination_url, 'https://outside.example/b')
        self.assertTrue(result.redirects[0].external)
        self.assertEqual(result.redirects[0].method, 'js_static_decoded')

    def test_safe_fetcher_default_off_preserves_literal(self):
        body = b'<script>window.location.href = "https://outside.example/?token=secret";</script>'
        response = self.FakeResponse(body=body)
        class Conn:
            def __init__(self, *args): pass
            def request(self, *args, **kw): pass
            def getresponse(self): return response
            def close(self): pass
        with patch.object(d, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(d, 'PinnedHTTPSConnection', Conn):
            result = d.SafeFetcher(timeout=1, max_bytes=10000,
                                   min_host_interval=0).get(ROOT, seed=SEED)
        self.assertEqual(result.redirects[0].method, 'js_literal')
        self.assertEqual(result.redirects[0].destination_url, 'https://outside.example/')
        self.assertEqual(result.js_review_hints, ())

    def test_dynamic_hints_only_without_external_destination(self):
        pages = {
            ROOT+'robots.txt': robots_404(),
            ROOT: d.FetchResult(200, ROOT, '<html/>', '', (), ('js_dynamic_location',)),
        }
        def fetch(url):
            return pages.get(url, robots_404(url))
        items, stats = d.discover_seed(SEED, fetch=fetch, max_sitemaps=0,
                                        browser_review_limit=5)
        self.assertEqual(items, [])
        self.assertEqual(stats['js_dynamic_pages'], 1)
        self.assertEqual(stats['external_redirects'], 0)
        self.assertEqual(stats['browser_review_candidates'][0]['reason'], 'js_dynamic_location')
        self.assertIsNone(stats['browser_review_candidates'][0]['destination'])

    def test_browser_review_priority_and_query_redaction(self):
        js = d.make_redirect(ROOT, 'https://outside.example/slot?apiKey=SECRET',
                              seed=SEED, method='js_static_decoded')
        meta = d.make_redirect(ROOT, 'https://foo.example/something?token=SECRET',
                                seed=SEED, method='meta_refresh')
        pages = {
            ROOT+'robots.txt': robots_404(),
            ROOT: d.FetchResult(200, ROOT, '<html/>', '', (meta, js), ('js_dynamic_location',)),
        }
        _, stats = d.discover_seed(SEED, fetch=lambda u: pages.get(u, robots_404(u)),
                                   max_sitemaps=0, browser_review_limit=10)
        review = stats['browser_review_candidates']
        self.assertEqual([row['priority'] for row in review], [85, 75, 30])
        self.assertNotIn('SECRET', str(review))
        self.assertEqual(stats['js_static_redirects'], 1)

    def test_robots_redirect_not_browser_review(self):
        item = d.make_redirect(ROOT+'robots.txt', 'https://other.example/',
                               seed=SEED, method='http', status=302)
        _, stats = d.discover_seed(SEED, fetch=lambda u: d.FetchResult(
            302, ROOT+'robots.txt', '', 'redirect_out_of_scope', (item,)),
            max_sitemaps=0, browser_review_limit=3)
        self.assertEqual(stats['browser_review_candidates'], [])

    def test_robots_redirect_disallow_prevents_second_http_request(self):
        class Conn:
            calls = []
            def __init__(self, host, ip, port, timeout):
                self.calls.append(host)
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return HTTPAndReviewSafety.FakeResponse(302, location='/private/')
            def close(self):
                pass
        with patch.object(d, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(d, 'PinnedHTTPSConnection', Conn):
            result = d.SafeFetcher(timeout=1, max_bytes=1024,
                                   min_host_interval=0).get(ROOT, seed=SEED,
                 redirect_allowed=lambda target: not target.endswith('/private/'))
        self.assertEqual(Conn.calls, ['contoh.id'])
        self.assertEqual(result.error, 'robots_redirect_disallow')
        self.assertEqual(d.fetch_failure_kind(result), 'robots_denied_redirect')

    def test_robots_checked_policy_passed_to_page_fetch(self):
        pages = {
            ROOT+'robots.txt': d.FetchResult(200, ROOT+'robots.txt',
                         'User-agent: *\nDisallow: /private\nAllow: /\n'),
            ROOT: d.FetchResult(200, ROOT, '<a href="/public">a</a>'),
        }
        seen = []
        def checked(url, policy):
            seen.append(policy.can_fetch(d.USER_AGENT, ROOT+'private/'))
            return pages.get(url, robots_404(url))
        _, stats = d.discover_seed(SEED,
                        fetch=lambda u: pages.get(u, robots_404(u)),
                        fetch_with_robots=checked, max_sitemaps=0)
        self.assertEqual(seen, [False])
        self.assertEqual(stats['web_errors'], 0)

    def test_deduplicated_static_js_vs_existing_literal(self):
        html = '''<script>window.location.href="https://outside.example/";</script>'''
        js = analyze_html(html)
        self.assertEqual([x.kind for x in js], ['js_literal'])
        existing = d.inspect_html_redirects(html, page_url=ROOT, seed=SEED)
        self.assertEqual(len(existing), 1)


class PreviewAndChainTests(unittest.TestCase):
    def test_http_chain_and_static_signal_not_conflated(self):
        a = d.make_redirect(ROOT, ROOT+'landing', seed=SEED, method='http', status=301)
        b = d.make_redirect(ROOT+'landing', 'https://away.example/?secret=redacted',
                            seed=SEED, method='http', status=302)
        c = d.make_redirect(ROOT, 'https://other.example/?secret=hide',
                            seed=SEED, method='js_static_decoded')
        chains = build_redirect_chains([a, b, c], limit=5)
        self.assertEqual(len(chains), 2)
        self.assertEqual(chains[0]['confidence'], 'http_observed')
        self.assertEqual(len(chains[0]['hops']), 2)
        self.assertEqual(chains[1]['confidence'], 'static_signal')
        self.assertNotIn('secret', str(chains))

    def test_cycles_and_branches_remain_bounded(self):
        a = d.make_redirect(ROOT+'a', ROOT+'b', seed=SEED, method='http', status=302)
        b = d.make_redirect(ROOT+'b', ROOT+'a', seed=SEED, method='http', status=302)
        chains = build_redirect_chains([a, b], limit=5, max_hops=4)
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0]['hops']), 2)

    def test_chain_respects_limit(self):
        a = d.make_redirect(ROOT, ROOT+'a', seed=SEED, method='http', status=301)
        self.assertEqual(build_redirect_chains([a], limit=0), [])
        self.assertEqual(len(build_redirect_chains([a], limit=1)), 1)

    def test_cli_default_off_and_flags(self):
        parser = d.arg_parser()
        args = parser.parse_args([])
        self.assertFalse(args.advanced_js)
        self.assertEqual(args.browser_review_limit, 0)
        self.assertEqual(args.redirect_chain_preview, 0)
        self.assertTrue(parser.parse_args(['--advanced-js']).advanced_js)
        with self.assertRaises(SystemExit):
            d.main(['--browser-review-limit', '3', '--apply'])
        with self.assertRaises(SystemExit):
            d.main(['--advanced-js'])
        with self.assertRaises(SystemExit):
            d.main(['--scan-web', '--advanced-js', '--apply'])

    def test_preview_has_browser_review_and_chains_no_db_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp)/'urls.json'
            db_file = Path(tmp)/'missing.sqlite3'
            input_file.write_text(json.dumps([SEED]), encoding='utf-8')
            obs = d.make_redirect(ROOT, 'https://external.example/?secret=CREDENTIAL',
                                  seed=SEED, method='js_static_decoded')
            def fake_get(_, url, *, seed, **kwargs):
                if url.endswith('/robots.txt'):
                    return robots_404(url)
                if url == ROOT:
                    return d.FetchResult(200, url, '<html/>', '', (obs,), ('js_dynamic_location',))
                return robots_404(url)
            args = d.arg_parser().parse_args(['--input', str(input_file), '--db', str(db_file),
                   '--scan-web', '--advanced-js', '--browser-review-limit', '3',
                   '--redirect-chain-preview', '3', '--max-sitemaps', '0',
                   '--request-interval', '0'])
            with patch.object(d.SafeFetcher, 'get', fake_get):
                result = d.run(args)
            self.assertEqual(result['mode'], 'preview')
            self.assertEqual(len(result['preview_browser_review']), 2)
            self.assertEqual(len(result['preview_redirect_chains']), 1)
            self.assertEqual(result['unique_redirects'], 1)
            self.assertNotIn('CREDENTIAL', json.dumps(result))
            self.assertFalse(db_file.exists())

    def test_preview_review_limit_default_no_browser_output(self):
        page = d.FetchResult(200, ROOT, '<html/>', '', (), ('js_dynamic_location',))
        _, stats = d.discover_seed(SEED, fetch=lambda u: robots_404(u)
                                if u.endswith('/robots.txt') else page, max_sitemaps=0)
        self.assertEqual(stats['browser_review_candidates'], [])
        self.assertEqual(stats['js_dynamic_pages'], 1)


if __name__ == '__main__':
    unittest.main()
