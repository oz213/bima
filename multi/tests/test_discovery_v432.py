"""v4.3.2 offline coverage: DNS confidence and .id redirect evidence semantics.

No live network, browser, BIMA alerts, production SQLite, or DNS queries.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery_dns as dnscheck
import proactive_discovery as d
from redirect_intelligence import build_redirect_chains


SEED = 'fl3xu5.web.id'
ROOT = 'https://' + SEED + '/'


def audit_result(*, diagnosis='authoritative_refused', zone='me-portofolio.web.id.',
                 coverage=True, rcodes=('REFUSED', 'REFUSED'), **kw):
    result = {
        'diagnosis': diagnosis,
        'delegated_zone': zone,
        'coverage_complete': coverage,
        'authority_checks': [{'rcode': r, 'server': '192.0.2.15',
                              'error': 'APIKEY_PRIVATE'} for r in rcodes],
        'error': 'SUPER_SECRET',
    }
    result.update(kw)
    return result


class DNSConfidenceTests(unittest.TestCase):
    HOST = 'me-portofolio.web.id'

    def summarize(self, **kwargs):
        return dnscheck.summarize_dns_audit(self.HOST, audit_result(**kwargs))

    def test_confirmed_lame_full_coverage_only(self):
        result = self.summarize()
        self.assertEqual(result['dns_status'], 'dns_lame_delegation')
        self.assertEqual(result['dns_confidence'], 'confirmed')
        self.assertEqual(result['dns_uncertainty_reasons'], [])

    def test_lame_incomplete_is_suspected(self):
        result = self.summarize(coverage=False)
        self.assertEqual(result['dns_status'], 'dns_lame_delegation_suspected')
        self.assertEqual(result['dns_confidence'], 'suspected')
        self.assertIn('authority_coverage_incomplete', result['dns_uncertainty_reasons'])

    def test_mixed_refused_and_timeout_is_inconclusive(self):
        result = self.summarize(diagnosis='authority_inconsistent_or_unavailable',
                                rcodes=('REFUSED', 'NO_RESPONSE'), coverage=False)
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')
        self.assertEqual(result['dns_confidence'], 'inconclusive')

    def test_parent_nxdomain_is_partial_not_confirmed(self):
        result = self.summarize(diagnosis='nxdomain_authoritative', zone=None,
                                coverage=False, rcodes=('NO_RESPONSE', 'NXDOMAIN'))
        self.assertEqual(result['dns_status'], 'dns_nxdomain_authoritative')
        self.assertEqual(result['dns_confidence'], 'partial')
        self.assertIn('parent_nxdomain_observation', result['dns_uncertainty_reasons'])
        self.assertIn('authority_coverage_incomplete', result['dns_uncertainty_reasons'])

    def test_parent_nxdomain_with_no_response_not_lame(self):
        result = self.summarize(diagnosis='nxdomain_authoritative', zone=None,
                                coverage=False, rcodes=('NXDOMAIN', 'NO_RESPONSE'))
        self.assertNotEqual(result['dns_confidence'], 'confirmed')
        self.assertNotEqual(result['dns_status'], 'dns_lame_delegation')

    def test_no_direct_address_is_partial_even_with_coverage(self):
        result = self.summarize(diagnosis='authoritative_responding',
                                rcodes=('NOERROR',), coverage=True,
                                address_observation='no_direct_address_observed')
        self.assertEqual(result['dns_status'], 'dns_no_direct_address_observed')
        self.assertEqual(result['dns_confidence'], 'partial')
        self.assertIn('direct_a_aaaa_observation_only', result['dns_uncertainty_reasons'])

    def test_direct_address_present_still_partial_scope(self):
        result = self.summarize(diagnosis='authoritative_responding',
                                rcodes=('NOERROR',), address_observation='address_record_present')
        self.assertEqual(result['dns_confidence'], 'partial')

    def test_host_nxdomain_is_partial_observation(self):
        result = self.summarize(diagnosis='authoritative_responding',
                                rcodes=('NOERROR',), address_observation='host_nxdomain')
        self.assertEqual(result['dns_confidence'], 'partial')

    def test_unreachable_authority_not_confirmed(self):
        result = self.summarize(diagnosis='authority_unreachable',
                                rcodes=('NO_RESPONSE',), coverage=False)
        self.assertEqual(result['dns_confidence'], 'partial')

    def test_delegation_loop_is_partial_not_lame(self):
        result = self.summarize(diagnosis='delegation_loop', rcodes=(), coverage=False)
        self.assertEqual(result['dns_confidence'], 'partial')

    def test_invalid_delegation_zone_not_confirmed(self):
        result = self.summarize(zone='unrelated.example.', coverage=True)
        self.assertEqual(result['dns_confidence'], 'inconclusive')

    def test_sensitive_values_redacted(self):
        result = self.summarize()
        self.assertNotIn('SUPER_SECRET', str(result))
        self.assertNotIn('APIKEY_PRIVATE', str(result))
        self.assertNotIn('192.0.2.15', str(result))

    def test_unknown_rcodes_not_exported(self):
        result = self.summarize(rcodes=('REFUSED', 'header=SECRET'))
        self.assertEqual(result['authority_rcodes'], ['REFUSED'])
        self.assertNotIn('SECRET', str(result))

    def test_audit_error_is_inconclusive(self):
        # Missing auditor module or a resolver failure is never confirmation.
        with patch.dict(sys.modules, {'dns_audit': None}):
            result = dnscheck.audit_dns_failure(self.HOST, timeout=1.5)
        self.assertEqual(result['dns_confidence'], 'inconclusive')
        self.assertEqual(result['dns_uncertainty_reasons'], ['audit_unavailable'])


class RedirectScopeTests(unittest.TestCase):
    def make(self, target, method='http', source=ROOT):
        obs = d.make_redirect(source, target, seed=SEED, method=method, status=301)
        self.assertIsNotNone(obs)
        return obs

    def test_same_host(self):
        r = self.make('https://fl3xu5.web.id/landing')
        self.assertEqual(r.redirect_scope, 'same_host')
        self.assertFalse(r.external)
        self.assertFalse(r.outside_id)

    def test_within_seed_subdomain(self):
        r = self.make('https://www.fl3xu5.web.id/')
        self.assertEqual(r.redirect_scope, 'within_seed_subdomain')
        self.assertFalse(r.external)
        self.assertFalse(r.outside_id)

    def test_different_id_host(self):
        r = self.make('https://fl3xu5.id/?api_key=PRIVATE')
        self.assertEqual(r.redirect_scope, 'cross_id_host')
        self.assertTrue(r.external)  # legacy: outside seed, not outside .id
        self.assertFalse(r.outside_id)
        self.assertEqual(r.destination_url, 'https://fl3xu5.id/')

    def test_external_non_id_host(self):
        r = self.make('https://example.net/landing?token=hidden')
        self.assertEqual(r.redirect_scope, 'outside_id')
        self.assertTrue(r.external)
        self.assertTrue(r.outside_id)
        self.assertNotIn('hidden', str(r))

    def test_existing_id_subdomain_is_not_outside_id(self):
        r = self.make('https://other.web.id/a')
        self.assertEqual(r.redirect_scope, 'cross_id_host')
        self.assertFalse(r.outside_id)

    def test_evidence_levels(self):
        self.assertEqual(self.make('https://elsewhere.net/', 'http').evidence_level, 'http_observed')
        for method in ('meta_refresh', 'js_literal', 'js_static_decoded'):
            with self.subTest(method=method):
                self.assertEqual(self.make('https://elsewhere.net/', method).evidence_level,
                                 'static_signal')

    def test_invalid_destinations_are_rejected(self):
        for url in ('javascript:alert(1)', 'http://127.0.0.1/',
                    'https://user:secret@elsewhere.net/', 'https://elsewhere.net:8888/'):
            with self.subTest(url=url):
                self.assertIsNone(d.make_redirect(ROOT, url, seed=SEED,
                                                  method='http', status=302))

    def test_chain_metadata_for_http_and_js(self):
        http = self.make('https://fl3xu5.id/')
        js = self.make('https://example.net/?secret=x', 'js_static_decoded')
        chains = build_redirect_chains([http, js], limit=10)
        self.assertEqual(chains[0]['evidence_level'], 'http_observed')
        self.assertEqual(chains[0]['hops'][0]['redirect_scope'], 'cross_id_host')
        self.assertFalse(chains[0]['hops'][0]['outside_id'])
        self.assertEqual(chains[1]['evidence_level'], 'static_signal')
        self.assertTrue(chains[1]['outside_id'])
        self.assertNotIn('secret', str(chains))

    def test_review_cross_id_host_lower_priority_than_outside_id(self):
        id_redirect = self.make('https://fl3xu5.id/')
        other = self.make('https://example.net/')
        pages = {
            ROOT + 'robots.txt': d.FetchResult(404, ROOT + 'robots.txt', '', 'http_status'),
            ROOT: d.FetchResult(200, ROOT, '<body>text</body>', '', (id_redirect, other)),
        }
        _, stats = d.discover_seed(SEED, fetch=lambda u: pages.get(u, pages[ROOT]),
                                   max_sitemaps=0, browser_review_limit=5)
        entries = stats['browser_review_candidates']
        self.assertEqual([e['priority'] for e in entries], [95, 55])
        self.assertEqual([e['outside_id'] for e in entries], [True, False])
        self.assertEqual(stats['outside_id_redirects'], 1)
        self.assertEqual(stats['external_redirects'], 2)
        self.assertEqual(stats['redirect_scope_counts'],
                         {'cross_id_host': 1, 'outside_id': 1})
        self.assertEqual(stats['redirect_evidence_counts'], {'http_observed': 2})

    def test_review_static_cross_id_host(self):
        js = self.make('https://other.id/', 'js_static_decoded')
        pages = {
            ROOT + 'robots.txt': d.FetchResult(404, ROOT + 'robots.txt', '', 'http_status'),
            ROOT: d.FetchResult(200, ROOT, '<body>ok</body>', '', (js,)),
        }
        _, stats = d.discover_seed(SEED, fetch=lambda u: pages.get(u, pages[ROOT]),
                                   max_sitemaps=0, browser_review_limit=5)
        entry = stats['browser_review_candidates'][0]
        self.assertEqual(entry['priority'], 35)
        self.assertEqual(entry['evidence_level'], 'static_signal')
        self.assertEqual(entry['redirect_scope'], 'cross_id_host')

    def test_robots_redirect_is_not_review(self):
        hop = d.make_redirect(ROOT + 'robots.txt', 'https://elsewhere.net/',
                              seed=SEED, method='http', status=302)
        _, stats = d.discover_seed(SEED,
            fetch=lambda u: d.FetchResult(302, ROOT + 'robots.txt', '',
                                         'redirect_out_of_scope', (hop,)),
            max_sitemaps=0, browser_review_limit=5)
        self.assertEqual(stats['browser_review_candidates'], [])


class PreviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.inventory = root/'urls.json'
        self.database = root/'nonexistent.sqlite3'
        self.inventory.write_text(json.dumps(['me-portofolio.web.id',
                                              'madaniresidence.co.id',
                                              'solvivo.my.id']), encoding='utf-8')

    def args(self, *extra):
        return d.arg_parser().parse_args([
            '--input', str(self.inventory), '--db', str(self.database),
            '--seed-limit', '3', '--request-interval', '0', '--max-sitemaps', '0',
            *extra])

    def test_dns_confidence_aggregates_and_no_db_write(self):
        class DNSFail:
            def __init__(self, **kwargs): pass
            def get(self, url, *, seed):
                return d.FetchResult(0, url, '', 'gaierror')
        mapping = {
            'me-portofolio.web.id': ('dns_lame_delegation', 'confirmed'),
            'madaniresidence.co.id': ('dns_nxdomain_authoritative', 'partial'),
            'solvivo.my.id': ('dns_no_direct_address_observed', 'partial'),
        }
        def fake_audit(seed, *, timeout):
            return {'seed': seed, 'dns_status': mapping[seed][0],
                    'dns_confidence': mapping[seed][1],
                    'dns_uncertainty_reasons': []}
        with patch.object(d, 'SafeFetcher', DNSFail), \
             patch.object(d, 'audit_dns_failure', side_effect=fake_audit):
            out = d.run(self.args('--scan-web', '--dns-audit-limit', '3',
                                  '--diagnostic-seeds', '3'))
        self.assertEqual(out['dns_confidence_counts'], {'confirmed': 1, 'partial': 2})
        self.assertEqual(out['dns_audits_performed'], 3)
        self.assertEqual(out['dns_failed_seeds'], 3)
        self.assertEqual(out['pending_before'], out['pending_after'])
        self.assertFalse(self.database.exists())
        self.assertEqual(out['new_jobs'], 0)

    def test_unverified_dns_stays_inconclusive(self):
        class DNSFail:
            def __init__(self, **kwargs): pass
            def get(self, url, *, seed):
                return d.FetchResult(0, url, '', 'gaierror')
        with patch.object(d, 'SafeFetcher', DNSFail), \
             patch.object(d, 'audit_dns_failure', side_effect=AssertionError('unexpected')):
            out = d.run(self.args('--scan-web'))
        self.assertEqual(out['dns_confidence_counts'], {'inconclusive': 3})
        self.assertEqual(out['dns_audits_performed'], 0)
        self.assertFalse(self.database.exists())

    def test_output_redirect_counts_without_sqlite_write(self):
        self.inventory.write_text(json.dumps([SEED]), encoding='utf-8')
        cross = d.make_redirect(ROOT, 'https://fl3xu5.id/?ticket=TOPSECRET',
                                seed=SEED, method='http', status=301)
        class Fetcher:
            def __init__(self, **kwargs): pass
            def get(self, url, *, seed):
                if url.endswith('/robots.txt'):
                    return d.FetchResult(404, url, '', 'http_status')
                return d.FetchResult(301, url, '', 'redirect_out_of_scope', (cross,))
        with patch.object(d, 'SafeFetcher', Fetcher):
            out = d.run(self.args('--scan-web', '--browser-review-limit', '5',
                                  '--redirect-chain-preview', '5'))
        self.assertEqual(out['redirect_scope_counts'], {'cross_id_host': 1})
        self.assertEqual(out['outside_id_redirects'], 0)
        self.assertEqual(out['external_redirects'], 1)  # old field preserved
        self.assertEqual(out['preview_redirects'][0]['evidence_level'], 'http_observed')
        self.assertEqual(out['preview_redirects'][0]['redirect_scope'], 'cross_id_host')
        self.assertFalse(out['preview_browser_review'][0]['outside_id'])
        self.assertNotIn('TOPSECRET', json.dumps(out))
        self.assertFalse(self.database.exists())


if __name__ == '__main__':
    unittest.main()
