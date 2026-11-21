"""BIMA v4.3.1: bounded DNS evidence and reproducible index sampling (offline)."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery_dns as dnscheck
import proactive_discovery as d


HOST = 'asazuzuqidisa.web.id'
ZONE = HOST + '.'


def audit_result(*, diagnosis='authoritative_refused', zone=ZONE,
                 coverage=True, rcodes=('REFUSED', 'REFUSED'), **kwargs):
    result = {
        'diagnosis': diagnosis, 'delegated_zone': zone,
        'coverage_complete': coverage,
        'authority_checks': [{'server': f'203.0.113.{i+1}', 'rcode': r}
                             for i, r in enumerate(rcodes)],
        'warnings': ['never expose private-token-sample'],
        'error': 'hidden-auth-token-sample',
    }
    result.update(kwargs)
    return result


class EvidenceSafetyTests(unittest.TestCase):
    def test_confirmed_lame_only_with_full_coverage(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result())
        self.assertEqual(result['dns_status'], 'dns_lame_delegation')
        self.assertEqual(result['authority_rcodes'], ['REFUSED'])
        self.assertEqual(result['authoritative_refused_observed'], 2)
        self.assertTrue(result['coverage_complete'])
        self.assertNotIn('token', str(result))
        self.assertNotIn('203.0.113.', str(result))

    def test_incomplete_refused_is_suspected_never_confirmed(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result(coverage=False))
        self.assertEqual(result['dns_status'], 'dns_lame_delegation_suspected')

    def test_all_refused_incomplete_is_suspected_not_confirmed(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result(coverage=False))
        self.assertEqual(result['dns_status'], 'dns_lame_delegation_suspected')

    def test_only_one_authoritative_refused_not_enough(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(rcodes=('REFUSED', 'NO_RESPONSE')))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_parent_zone_refused_not_lame(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result(zone='id.'))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_wrong_zone_refused_not_lame(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result(zone='evil.example.'))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_no_checks_refused_not_lame(self):
        result = dnscheck.summarize_dns_audit(HOST, audit_result(rcodes=()))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_servfail_does_not_prove_lame(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='authority_inconsistent_or_unavailable',
                               rcodes=('SERVFAIL', 'REFUSED')))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_dns_parent_nxdomain_is_separate_status(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='nxdomain_authoritative', zone=None,
                               coverage=False, rcodes=('NXDOMAIN',)))
        self.assertEqual(result['dns_status'], 'dns_nxdomain_authoritative')

    def test_unresolvable_delegated_nameservers(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='delegated_ns_unresolvable',
                               coverage=False, rcodes=()))
        self.assertEqual(result['dns_status'], 'dns_delegated_ns_unresolvable')

    def test_unreachable_authority(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='authority_unreachable',
                               coverage=False, rcodes=('NO_RESPONSE',)))
        self.assertEqual(result['dns_status'], 'dns_authority_unreachable')

    def test_missing_direct_address_is_not_lame(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='authoritative_responding',
                               rcodes=('NOERROR',),
                               address_observation='no_direct_address_observed'))
        self.assertEqual(result['dns_status'], 'dns_no_direct_address_observed')

    def test_address_observed_is_not_lame(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='authoritative_responding',
                               rcodes=('NOERROR',),
                               address_observation='address_record_present'))
        self.assertEqual(result['dns_status'], 'dns_authoritative_address_observed')

    def test_host_nxdomain_under_authoritative_zone(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='authoritative_responding',
                               rcodes=('NOERROR',), address_observation='host_nxdomain'))
        self.assertEqual(result['dns_status'], 'dns_host_nxdomain_observed')

    def test_delegation_loop_separate(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(diagnosis='delegation_loop', rcodes=()))
        self.assertEqual(result['dns_status'], 'dns_delegation_loop')

    def test_unknown_rcodes_are_not_exported(self):
        result = dnscheck.summarize_dns_audit(
            HOST, audit_result(rcodes=('REFUSED', 'token=XYZ')))
        self.assertNotIn('XYZ', str(result))
        self.assertEqual(result['dns_status'], 'dns_audit_inconclusive')

    def test_reuse_existing_dns_audit_without_network(self):
        calls = []
        fake = types.SimpleNamespace(audit_domain=lambda domain, **kwargs: (
            calls.append((domain, kwargs)) or audit_result()))
        with patch.dict(sys.modules, {'dns_audit': fake}):
            result = dnscheck.audit_dns_failure(HOST, timeout=1.5)
        self.assertEqual(result['dns_status'], 'dns_lame_delegation')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], HOST)
        self.assertEqual(calls[0][1]['timeout'], 1.5)
        self.assertEqual(calls[0][1]['max_hops'], 6)

    def test_dns_auditor_failure_leaks_no_exception(self):
        def broken(*args, **kwargs):
            raise OSError('API_KEY_NOT_PUBLIC')
        with patch.dict(sys.modules, {'dns_audit': types.SimpleNamespace(audit_domain=broken)}):
            result = dnscheck.audit_dns_failure(HOST, timeout=1.5)
        self.assertEqual(result['dns_status'], 'dns_audit_unavailable_or_error')
        self.assertNotIn('API_KEY_NOT_PUBLIC', str(result))


class SamplingTests(unittest.TestCase):
    def test_strata_nonoverlap_and_spread(self):
        picks = d.stratified_seed_indices(1000, 10, 'abc', 17)
        self.assertEqual(len(set(picks)), 10)
        self.assertEqual(len(picks), 10)
        for i, index in enumerate(picks):
            self.assertTrue(100 * i <= index < 100 * (i+1))

    def test_repeatable_across_runs(self):
        p1 = d.stratified_seed_indices(1547435, 30, 'registry-hash', 7)
        p2 = d.stratified_seed_indices(1547435, 30, 'registry-hash', 7)
        self.assertEqual(p1, p2)

    def test_different_seed_changes_positions(self):
        p1 = d.stratified_seed_indices(10000, 50, 'registry-hash', 7)
        p2 = d.stratified_seed_indices(10000, 50, 'registry-hash', 8)
        self.assertNotEqual(p1, p2)

    def test_digest_change_changes_positions(self):
        p1 = d.stratified_seed_indices(10000, 50, 'hash-one', 7)
        p2 = d.stratified_seed_indices(10000, 50, 'hash-two', 7)
        self.assertNotEqual(p1, p2)

    def test_more_requested_than_available_no_duplicates(self):
        self.assertEqual(d.stratified_seed_indices(3, 30, 'sha', 0), [0, 1, 2])

    def test_bounds(self):
        for total, n, seed in ((0, 1, 0), (5, 0, 0), (5, 3, -1)):
            with self.subTest(total=total, n=n, seed=seed), self.assertRaises(ValueError):
                d.stratified_seed_indices(total, n, 'sha', seed)


class RunIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'urls.json'
        self.db = self.root / 'nonexistent.sqlite3'
        self.hosts = ['good1.id', 'broken1.id', 'good2.id', 'broken2.id', 'broken3.id']
        self.input.write_text(json.dumps(self.hosts), encoding='utf-8')

    def args(self, *extra):
        return d.arg_parser().parse_args([
            '--input', str(self.input), '--db', str(self.db),
            '--seed-limit', '5', '--request-interval', '0',
            '--max-sitemaps', '0', *extra])

    def test_sequential_legacy_cursor_unchanged(self):
        result = d.run(self.args())
        self.assertEqual(result['seed_selection'], 'sequential')
        self.assertEqual(result['start_index'], 0)
        self.assertEqual(result['next_index'], 0)
        self.assertEqual(result['dns_audits_performed'], 0)
        self.assertEqual(result['sampled_indices'], [])
        self.assertFalse(self.db.exists())

    def test_stratified_preview_no_sqlite_write(self):
        args = self.args('--seed-selection', 'stratified', '--sampling-seed', '21')
        first = d.run(args)
        second = d.run(args)
        self.assertEqual(first['sampled_indices'], second['sampled_indices'])
        self.assertEqual(first['start_index'], None)
        self.assertEqual(first['next_index'], None)
        self.assertEqual(first['sampled_indices'], list(range(5)))
        self.assertFalse(self.db.exists())
        self.assertEqual(first['new_jobs'], 0)

    def test_programmatic_apply_bypass_is_rejected(self):
        for extra in [('--seed-selection', 'stratified'),
                      ('--dns-audit-limit', '1', '--scan-web')]:
            args = self.args(*extra)
            args.apply = True
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                d.run(args)
        self.assertFalse(self.db.exists())

    def test_dns_audit_only_on_web_dns_failures_and_bounded(self):
        class Fetcher:
            def __init__(self, **kwargs):
                pass
            def get(self, url, *, seed):
                if seed.startswith('broken'):
                    return d.FetchResult(0, url, '', 'gaierror')
                if url.endswith('robots.txt'):
                    return d.FetchResult(404, url, '', 'http_status')
                return d.FetchResult(200, url, '<body>OK</body>')
        calls = []
        def fake_audit(seed, *, timeout):
            calls.append((seed, timeout))
            return {'seed': seed, 'dns_status': 'dns_lame_delegation',
                    'delegated_zone': seed+'.', 'authority_rcodes': ['REFUSED'],
                    'authority_checks': 2, 'coverage_complete': True,
                    'authoritative_refused_observed': 2}
        args = self.args('--scan-web', '--dns-audit-limit', '2',
                         '--diagnostic-seeds', '5')
        with patch.object(d, 'SafeFetcher', Fetcher), patch.object(d, 'audit_dns_failure', fake_audit):
            result = d.run(args)
        self.assertEqual(result['dns_failed_seeds'], 3)
        self.assertEqual(result['dns_audits_performed'], 2)
        self.assertEqual(result['dns_status_counts'],
                         {'dns_lame_delegation': 2, 'dns_resolution_failed_unverified': 1})
        self.assertEqual(len(calls), 2)
        self.assertEqual([c[0] for c in calls], ['broken1.id', 'broken2.id'])
        self.assertEqual(result['preview_diagnostics'][1]['dns_status'], 'dns_lame_delegation')
        self.assertEqual(result['preview_diagnostics'][4]['dns_status'],
                         'dns_resolution_failed_unverified')
        self.assertFalse(self.db.exists())

    def test_no_dns_audit_by_default_even_on_failures(self):
        class AlwaysFail:
            def __init__(self, **kwargs): pass
            def get(self, url, *, seed):
                return d.FetchResult(0, url, '', 'gaierror')
        with patch.object(d, 'SafeFetcher', AlwaysFail), \
             patch.object(d, 'audit_dns_failure', side_effect=AssertionError('must not audit')):
            result = d.run(self.args('--scan-web'))
        self.assertEqual(result['dns_failed_seeds'], 5)
        self.assertEqual(result['dns_audits_performed'], 0)
        self.assertEqual(result['dns_status_counts']['dns_resolution_failed_unverified'], 5)
        self.assertFalse(self.db.exists())

    def test_validation_rejects_apply_stratified_and_dns_audit(self):
        bad = [
            ['--apply', '--seed-selection', 'stratified'],
            ['--apply', '--scan-web', '--dns-audit-limit', '1'],
            ['--seed-selection', 'stratified', '--start-index', '10'],
            ['--sampling-seed', '2'],
            ['--dns-audit-limit', '1'],
            ['--dns-audit-limit', '4', '--scan-web'],
            ['--dns-audit-timeout', '9'],
            ['--seed-selection', 'stratified', '--seed-limit', '101'],
        ]
        for flags in bad:
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                d.main(['--input', str(self.input), '--db', str(self.db), *flags])
        self.assertFalse(self.db.exists())


if __name__ == '__main__':
    unittest.main()
