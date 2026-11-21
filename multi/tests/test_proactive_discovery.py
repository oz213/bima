"""Offline-only tests; no network requests, BIMA alerts, or IndoBERT needed."""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proactive_discovery as discovery


class URLValidationTests(unittest.TestCase):
    def test_id_and_subdomain(self):
        self.assertEqual(discovery.normalize_url('EXAMPLE.ID'), 'https://example.id/')
        self.assertEqual(discovery.normalize_url('HTTPS://WWW.Example.Id/slot#abc'),
                         'https://www.example.id/slot')

    def test_idn_and_punycode(self):
        result = discovery.normalize_url('https://café.id/promo')
        self.assertEqual(result, 'https://xn--caf-dma.id/promo')
        self.assertEqual(discovery.normalize_url(result), result)

    def test_strip_query_and_fragment(self):
        self.assertEqual(discovery.normalize_url('https://contoh.id/slot?token=secret#foo'),
                         'https://contoh.id/slot')

    def test_block_off_scope_and_credentials(self):
        for value in ('https://a.id.evil.com/', 'https://example.com/',
                      'http://localhost/', 'https://a.id@evil.com/',
                      'https://user:password@contoh.id/', 'file:///tmp/a.id'):
            with self.subTest(value=value):
                self.assertIsNone(discovery.normalize_url(value))

    def test_block_bad_ports_actions_and_controls(self):
        for value in ('https://contoh.id:8080/', 'https://contoh.id:99999/',
                      'https://contoh.id/logout', 'https://contoh.id/assets/a.js',
                      'https://contoh.id/x\ny', 'https://contoh.id/a/../admin',
                      'https://contoh.id\\@other.id/'):
            with self.subTest(value=value):
                self.assertIsNone(discovery.normalize_url(value))

    def test_seed_scope_boundary(self):
        self.assertTrue(discovery.within_seed('https://a.example.co.id/', 'example.co.id'))
        self.assertFalse(discovery.within_seed('https://fakeexample.co.id/', 'example.co.id'))
        self.assertFalse(discovery.within_seed('https://example.co.id.attacker.id/', 'example.co.id'))

    def test_no_seed_homepage_duplicate(self):
        self.assertIsNone(discovery.make_candidate('https://contoh.id/', source='ct', seed='contoh.id'))
        self.assertIsNotNone(discovery.make_candidate('https://www.contoh.id/', source='ct', seed='contoh.id'))

    def test_score_is_priority_not_abuse_verdict(self):
        high = discovery.make_candidate('https://contoh.id/slot/', source='homepage', seed='contoh.id')
        low = discovery.make_candidate('https://contoh.id/about/', source='homepage', seed='contoh.id')
        self.assertGreater(high.priority_score, low.priority_score)


class InputTests(unittest.TestCase):
    def test_load_same_formats_as_crawler(self):
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / 'urls.json'
            for data in (['contoh.id', 'https://contoh.id/a', 'bad.com'],
                         {'urls': ['contoh.id', 'https://contoh.id/a', 'bad.com']}):
                filename.write_text(json.dumps(data), encoding='utf-8')
                hosts, digest, rejected = discovery.load_seeds(filename)
                self.assertEqual(hosts, ['contoh.id'])
                self.assertEqual(rejected, 1)
                self.assertEqual(len(digest), 64)

    def test_reject_unknown_structure(self):
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / 'urls.json'
            filename.write_text('{"hosts":["example.id"]}', encoding='utf-8')
            with self.assertRaises(ValueError):
                discovery.load_seeds(filename)


class WebDiscoveryTests(unittest.TestCase):
    def test_homepage_sitemap_ct_and_robots(self):
        seed = 'contoh.id'
        paths = []
        fixtures = {
            '/robots.txt': discovery.FetchResult(200, 'https://contoh.id/robots.txt',
                'User-agent: *\nDisallow: /private\nSitemap: https://contoh.id/site.xml\n'),
            '/': discovery.FetchResult(200, 'https://contoh.id/',
                '<a href="/slot/">slot</a>'
                '<a href="/private/bonus">private</a>'
                '<a href="https://other.id/promo">external</a>'
                '<a href="https://foo.contoh.id/bonus">sub</a>'
                '<a href="/logout">skip</a>'),
            '/site.xml': discovery.FetchResult(200, 'https://contoh.id/site.xml',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                '<url><loc>https://contoh.id/bonus</loc></url>'
                '<url><loc>https://other.id/slot</loc></url>'
                '<url><loc>https://contoh.id/private/a</loc></url>'
                '</urlset>'),
        }

        def fake_fetch(url):
            path = discovery.urlsplit(url).path
            paths.append(path)
            return fixtures.get(path, discovery.FetchResult(404, url, ''))

        results, stats = discovery.discover_seed(
            seed, fetch=fake_fetch, include_ct=True,
            ct_lookup=lambda _: ['https://new.contoh.id/', 'https://nonid.com/'],
            max_candidates=30)
        urls = {r.url for r in results}
        self.assertIn('https://contoh.id/slot/', urls)
        self.assertIn('https://foo.contoh.id/bonus', urls)
        self.assertIn('https://contoh.id/bonus', urls)
        self.assertIn('https://new.contoh.id/', urls)
        self.assertNotIn('https://contoh.id/private/bonus', urls)
        self.assertNotIn('https://other.id/slot', urls)
        self.assertEqual(stats['web_requests'], 3)
        self.assertEqual(paths, ['/robots.txt', '/', '/site.xml'])

    def test_robots_deny_skips_web_fetch(self):
        paths = []
        def fake_fetch(url):
            paths.append(discovery.urlsplit(url).path)
            return discovery.FetchResult(200, url, 'User-agent: *\nDisallow: /\n')
        items, _ = discovery.discover_seed('contoh.id', fetch=fake_fetch,
                                            include_ct=False)
        self.assertEqual(items, [])
        self.assertEqual(paths, ['/robots.txt'])

    def test_robots_403_fail_closed(self):
        count = []
        def fake_fetch(url):
            count.append(url)
            return discovery.FetchResult(403, url, '')
        items, stats = discovery.discover_seed('contoh.id', fetch=fake_fetch)
        self.assertEqual(items, [])
        self.assertEqual(len(count), 1)
        self.assertEqual(stats['web_errors'], 1)

    def test_sitemap_index_nested(self):
        xml = '<sitemapindex><sitemap><loc>https://example.id/sub.xml</loc></sitemap></sitemapindex>'
        pages, child = discovery.parse_sitemap(xml)
        self.assertEqual(pages, [])
        self.assertEqual(child, ['https://example.id/sub.xml'])

    def test_ct_failure_does_not_abort_web_scan(self):
        def failing(_):
            raise TimeoutError('timed out')
        items, stats = discovery.discover_seed('example.id', fetch=None,
                                              include_ct=True, ct_lookup=failing)
        self.assertEqual(items, [])
        self.assertEqual(stats['ct_errors'], 1)


class NetworkSafetyTests(unittest.TestCase):
    def test_mixed_private_and_public_dns_rejected(self):
        answers = [(None, None, None, '', ('8.8.8.8', 443)),
                   (None, None, None, '', ('192.168.1.1', 443))]
        with patch.object(discovery.socket, 'getaddrinfo', return_value=answers):
            with self.assertRaisesRegex(ValueError, 'dns_non_public_address'):
                discovery.public_ip_for('example.id', 443)

    def test_private_only_dns_is_rejected(self):
        answers = [(None, None, None, '', ('127.0.0.1', 443))]
        with patch.object(discovery.socket, 'getaddrinfo', return_value=answers):
            with self.assertRaisesRegex(ValueError, 'dns_non_public_address'):
                discovery.public_ip_for('example.id', 443)

    def test_private_dns_skips_connection(self):
        with patch.object(discovery, 'public_ip_for', side_effect=ValueError('dns_non_public_address')), \
             patch.object(discovery, 'PinnedHTTPSConnection') as connection:
            result = discovery.SafeFetcher(timeout=1, max_bytes=1000,
                                           min_host_interval=0).get(
                'https://example.id/', seed='example.id')
        self.assertEqual(result.status, 0)
        connection.assert_not_called()

    def test_discovery_fetch_rejects_non_seed(self):
        with patch.object(discovery, 'public_ip_for') as resolver:
            result = discovery.SafeFetcher(timeout=1, max_bytes=1000).get(
                'https://attacker.id/', seed='example.id')
        self.assertEqual(result.error, 'out_of_scope')
        resolver.assert_not_called()

    def test_external_redirect_never_fetched(self):
        class RedirectResponse:
            status = 302
            def getheader(self, name, default=''):
                return 'https://evil.com/exfiltrate' if name == 'Location' else default
        class FakeConn:
            calls = 0
            def __init__(self, *args):
                FakeConn.calls += 1
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return RedirectResponse()
            def close(self):
                pass
        FakeConn.calls = 0
        with patch.object(discovery, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(discovery, 'PinnedHTTPSConnection', FakeConn):
            result = discovery.SafeFetcher(timeout=1, max_bytes=1024, min_host_interval=0).get(
                'https://example.id/', seed='example.id')
        self.assertEqual(result.error, 'redirect_out_of_scope')
        self.assertEqual(FakeConn.calls, 1)


class RedirectDetectionTests(unittest.TestCase):
    def test_redirect_target_is_redacted_and_not_enqueued_as_global_domain(self):
        item = discovery.make_redirect(
            'https://contoh.id/', 'https://judol.example/landing?token=topsecret#x',
            seed='contoh.id', method='http', status=302)
        self.assertIsNotNone(item)
        self.assertEqual(item.source_url, 'https://contoh.id/')
        self.assertEqual(item.destination_url, 'https://judol.example/landing')
        self.assertTrue(item.external)
        self.assertEqual(item.http_status, 302)
        self.assertIsNone(discovery.make_candidate(item.destination_url,
                seed='contoh.id', source='redirect'))

    def test_invalid_redirect_targets_are_rejected(self):
        for destination in ('javascript:alert(1)', 'file:///etc/passwd',
                            'http://127.0.0.1/admin', 'http://localhost/admin',
                            'http://user:password@example.com/',
                            'https://example.com:8080/private'):
            with self.subTest(destination=destination):
                self.assertIsNone(discovery.make_redirect(
                    'https://contoh.id/', destination, seed='contoh.id',
                    method='http', status=302))

    def test_meta_refresh_and_static_javascript_patterns(self):
        html = '''<html><head><meta http-equiv="refresh" content="0;url=https://outside.example/slot?k=secret"/></head>
        <body><script>window.location.href="https://other.example/path";
        location.replace('/internal'); location.assign('https://another.example/path?code=ABC');</script></body></html>'''
        observations = discovery.inspect_html_redirects(
            html, page_url='https://contoh.id/', seed='contoh.id')
        self.assertEqual(len(observations), 4)
        self.assertEqual({o.method for o in observations}, {'meta_refresh', 'js_literal'})
        self.assertTrue(any(o.destination_url == 'https://outside.example/slot'
                            for o in observations))
        self.assertTrue(any(not o.external for o in observations))
        self.assertNotIn('secret', str(observations))
        self.assertNotIn('ABC', str(observations))

    def test_static_patterns_not_a_runtime_confirmation(self):
        html = '<script src="https://external.example/a.js"></script><a href="https://external.example">go</a>'
        self.assertEqual(discovery.inspect_html_redirects(html,
            page_url='https://contoh.id/', seed='contoh.id'), ())

    def test_external_http_redirect_records_without_following(self):
        class RedirectResponse:
            status = 302
            def getheader(self, name, default=''):
                return 'https://external.example/page?session=secret' if name == 'Location' else default
        class FakeConn:
            calls = 0
            def __init__(self, *args):
                FakeConn.calls += 1
            def request(self, *args, **kwargs): pass
            def getresponse(self): return RedirectResponse()
            def close(self): pass
        with patch.object(discovery, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(discovery, 'PinnedHTTPSConnection', FakeConn):
            result = discovery.SafeFetcher(timeout=1, max_bytes=1024,
                        min_host_interval=0).get('https://contoh.id/', seed='contoh.id')
        self.assertEqual(result.error, 'redirect_out_of_scope')
        self.assertEqual(len(result.redirects), 1)
        self.assertEqual(result.redirects[0].destination_url, 'https://external.example/page')
        self.assertEqual(FakeConn.calls, 1)

    def test_multihop_internal_then_external_within_budget(self):
        class RedirectResponse:
            status = 302
            def __init__(self, target): self.target = target
            def getheader(self, name, default=''):
                return self.target if name == 'Location' else default
        class FakeConn:
            calls = []
            def __init__(self, *args): pass
            def request(self, method, path, **kwargs):
                self.path = path
                FakeConn.calls.append(path)
            def getresponse(self):
                return RedirectResponse('/step') if self.path == '/' else RedirectResponse('https://outside.example/')
            def close(self): pass
        with patch.object(discovery, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(discovery, 'PinnedHTTPSConnection', FakeConn):
            result = discovery.SafeFetcher(timeout=1, max_bytes=1024,
                min_host_interval=0).get('https://contoh.id/', seed='contoh.id')
        self.assertEqual(FakeConn.calls, ['/', '/step'])
        self.assertEqual(result.error, 'redirect_out_of_scope')
        self.assertEqual(len(result.redirects), 2)
        self.assertFalse(result.redirects[0].external)
        self.assertTrue(result.redirects[1].external)

    def test_internal_redirect_query_is_sent_but_never_stored(self):
        class Response:
            def __init__(self, status, dest=''):
                self.status, self.dest = status, dest
            def getheader(self, name, default=''):
                if name == 'Location': return self.dest
                if name == 'Content-Type': return 'text/html'
                return default
            def read(self, count): return b'<html>safe</html>'
        class FakeConn:
            calls = []
            def __init__(self, *args): pass
            def request(self, method, path, **kwargs):
                self.path = path
                FakeConn.calls.append(path)
            def getresponse(self):
                if self.path == '/':
                    return Response(302, '/next?ticket=keepsecret')
                return Response(200)
            def close(self): pass
        with patch.object(discovery, 'public_ip_for', return_value='8.8.8.8'), \
             patch.object(discovery, 'PinnedHTTPSConnection', FakeConn):
            result = discovery.SafeFetcher(timeout=1, max_bytes=1024,
                 min_host_interval=0).get('https://contoh.id/', seed='contoh.id')
        self.assertEqual(FakeConn.calls, ['/', '/next?ticket=keepsecret'])
        self.assertEqual(result.final_url, 'https://contoh.id/next')
        self.assertEqual(result.redirects[0].destination_url, 'https://contoh.id/next')
        self.assertNotIn('keepsecret', str(result))

    def test_discovery_callback_reports_redirect_event(self):
        target = discovery.make_redirect('https://contoh.id/',
                    'https://outside.example/', seed='contoh.id',
                    method='http', status=302)
        seen = []
        def fetch(url):
            path = discovery.urlsplit(url).path
            if path == '/robots.txt':
                return discovery.FetchResult(404, url, '')
            if path == '/':
                return discovery.FetchResult(302, url, '', 'redirect_out_of_scope', (target,))
            return discovery.FetchResult(404, url, '')
        _, counters = discovery.discover_seed('contoh.id', fetch=fetch,
                                             redirect_observer=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(counters['external_redirects'], 1)
        self.assertEqual(counters['redirect_observations'], 1)

    def test_persist_redirect_observation_without_auto_alert(self):
        with tempfile.TemporaryDirectory() as dirname:
            db = Path(dirname) / 'jobs.sqlite3'
            conn = sqlite3.connect(db)
            conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, '
                         "status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT)")
            conn.commit()
            item = discovery.make_redirect('https://contoh.id/',
                 'https://outside.example/landing?token=hidden',
                 seed='contoh.id', method='http', status=302)
            discovery.apply_candidates(conn, candidates=[], input_digest='digest',
                next_cursor=1, max_new=10, max_pending=100, redirects=[item])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            rows = conn.execute('SELECT destination_url,review_status FROM '
                                'proactive_redirect_observations').fetchall()
            self.assertEqual(rows, [('https://outside.example/landing', 'new')])
            conn.execute("UPDATE proactive_redirect_observations SET review_status='reviewed'")
            conn.commit()
            discovery.apply_candidates(conn, candidates=[], input_digest='digest',
                next_cursor=2, max_new=10, max_pending=100, redirects=[item])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM proactive_redirect_observations').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT review_status FROM proactive_redirect_observations').fetchone()[0], 'reviewed')
            conn.close()


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'crawler.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('CREATE TABLE jobs ('
                          'id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, '
                          "status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT)")
        self.conn.execute('INSERT INTO jobs(url,updated_at) VALUES (?,?)',
                          ('https://example.id/', discovery.utcnow()))
        self.conn.commit()
        self.conn.close()

    def new_conn(self, apply):
        return discovery.open_jobs_db(self.path, apply=apply)

    def test_preview_read_only_and_no_migration(self):
        conn = self.new_conn(False)
        try:
            self.assertEqual(discovery.pending_count(conn), 1)
            self.assertEqual(discovery.state_of(conn), (None, 0))
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('INSERT INTO jobs(url) VALUES ("https://evil.id/")')
            table_count = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                                       "AND name='proactive_discovery_candidates'").fetchone()[0]
            self.assertEqual(table_count, 0)
        finally:
            conn.close()

    def test_apply_inserts_job_preserves_existing_and_cursor(self):
        candidates = [discovery.make_candidate('https://example.id/slot', source='sitemap', seed='example.id'),
                      discovery.make_candidate('https://example.id/slot', source='homepage', seed='example.id')]
        conn = self.new_conn(True)
        try:
            num, pending, held = discovery.apply_candidates(conn, candidates=candidates,
                input_digest='sha-one', next_cursor=2, max_new=10, max_pending=100)
            self.assertEqual(num, 1)
            self.assertEqual(pending, 2)
            self.assertEqual(held, 0)
            num, pending, held = discovery.apply_candidates(conn, candidates=candidates,
                input_digest='sha-one', next_cursor=3, max_new=10, max_pending=100)
            self.assertEqual(num, 0)
            self.assertEqual(pending, 2)
            self.assertEqual(held, 0)
            self.assertEqual(discovery.state_of(conn), ('sha-one', 3))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
        finally:
            conn.close()

    def test_backpressure(self):
        candidate = discovery.make_candidate('https://example.id/slot', source='ct', seed='example.id')
        conn = self.new_conn(True)
        try:
            num, pending, held = discovery.apply_candidates(conn, candidates=[candidate],
                input_digest='sha-two', next_cursor=1, max_new=10, max_pending=1)
            self.assertEqual((num, pending, held), (0, 1, 1))
        finally:
            conn.close()

    def test_backlog_eventually_drains_without_refetch(self):
        items = [discovery.make_candidate('https://example.id/slot', source='sitemap', seed='example.id'),
                 discovery.make_candidate('https://example.id/bonus', source='sitemap', seed='example.id')]
        conn = self.new_conn(True)
        try:
            created, backlog, held = discovery.apply_candidates(conn, candidates=items,
                input_digest='sha', next_cursor=1, max_new=1, max_pending=100)
            self.assertEqual((created, held), (1, 1))
            created, backlog, held = discovery.apply_candidates(conn, candidates=[],
                input_digest='sha', next_cursor=2, max_new=1, max_pending=100)
            self.assertEqual((created, held), (1, 0))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 3)
        finally:
            conn.close()

    def test_apply_does_not_create_missing_db(self):
        with self.assertRaises(FileNotFoundError):
            discovery.open_jobs_db(self.path.parent / 'missing.db', apply=True)

    def test_manual_file_update_resets_cursor_to_zero(self):
        seed_file = Path(self.temp.name) / 'urls.json'
        seed_file.write_text(json.dumps(['example.id', 'other.id']), encoding='utf-8')
        args = discovery.arg_parser().parse_args([
            '--input', str(seed_file), '--db', str(self.path), '--seed-limit', '1',
            '--apply', '--request-interval', '0'])
        one = discovery.run(args)
        self.assertEqual(one['start_index'], 0)
        seed_file.write_text(json.dumps(['example.id', 'other.id', 'new.id']), encoding='utf-8')
        two = discovery.run(args)
        self.assertEqual(two['start_index'], 0)

    def test_cli_defaults_are_read_only(self):
        args = discovery.arg_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.scan_web)
        self.assertFalse(args.ct)

    def test_run_offline_preview_and_apply(self):
        seed_file = Path(self.temp.name) / 'urls.json'
        seed_file.write_text(json.dumps({'urls': ['example.id', 'other.id']}), encoding='utf-8')
        p = discovery.arg_parser()
        args = p.parse_args(['--input', str(seed_file), '--db', str(self.path),
                             '--seed-limit', '1', '--request-interval', '0'])
        preview = discovery.run(args)
        self.assertEqual(preview['mode'], 'preview')
        self.assertEqual(preview['pending_after'], 1)
        self.assertEqual(preview['next_index'], 1)
        args.apply = True
        result = discovery.run(args)
        self.assertEqual(result['mode'], 'apply')
        self.assertEqual(result['next_index'], 1)
        conn = self.new_conn(False)
        try:
            self.assertEqual(discovery.state_of(conn)[1], 1)
        finally:
            conn.close()


class CandidateProbeTests(unittest.TestCase):
    def test_probe_first_scored_page_and_save_redirect_observation(self):
        contacted = []
        observed = []
        def fetch(url):
            contacted.append(url)
            path = discovery.urlsplit(url).path
            if path == '/robots.txt':
                return discovery.FetchResult(404, url, '')
            if path == '/':
                return discovery.FetchResult(200, url,
                    '<a href="/slot/">target</a><a href="/about/">about</a>'
                    '<a href="https://another.contoh.id/promo">other host</a>')
            if path == '/slot/':
                item = discovery.make_redirect(url, 'https://elsewhere.example/landing?key=secret',
                                               seed='contoh.id', method='http', status=302)
                return discovery.FetchResult(302, url, '', 'redirect_out_of_scope', (item,))
            return discovery.FetchResult(200, url, 'normal')
        candidates, counts = discovery.discover_seed(
            'contoh.id', fetch=fetch, max_sitemaps=0, probe_candidates=1,
            redirect_observer=observed.append)
        self.assertEqual(counts['probed_pages'], 1)
        self.assertEqual(counts['external_redirects'], 1)
        self.assertEqual(counts['probe_errors'], 0)
        self.assertIn('https://contoh.id/slot/', contacted)
        self.assertNotIn('https://contoh.id/about/', contacted)
        self.assertNotIn('https://another.contoh.id/promo', contacted)
        self.assertEqual(observed[0].destination_url, 'https://elsewhere.example/landing')

    def test_robots_excluded_page_never_probed(self):
        contacted = []
        def fetch(url):
            contacted.append(url)
            path = discovery.urlsplit(url).path
            if path == '/robots.txt':
                return discovery.FetchResult(200, url, 'User-agent: *\nDisallow: /private\n')
            if path == '/':
                return discovery.FetchResult(200, url,
                    '<a href="/private/slot">bad</a><a href="/public/promo">good</a>')
            return discovery.FetchResult(200, url, '')
        _, counts = discovery.discover_seed(
            'contoh.id', fetch=fetch, max_sitemaps=0, probe_candidates=2)
        self.assertEqual(counts['probed_pages'], 1)
        self.assertNotIn('https://contoh.id/private/slot', contacted)
        self.assertIn('https://contoh.id/public/promo', contacted)

    def test_probe_default_off(self):
        self.assertEqual(discovery.arg_parser().parse_args([]).probe_candidates, 0)


if __name__ == '__main__':
    unittest.main()
