"""v4.3.5: Brave quota/residency protections; all providers are synthetic."""
import argparse
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import brave_quota as bq
import multi_source_discovery as ms


NOV = datetime(2026, 11, 21, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 1, tzinfo=timezone.utc)


def reserve_for_process(args):
    path, limit = args
    return bq.reserve_request(path, limit, NOV)[0]


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / 'brave-quota.json'

    def test_initial_reservation_private_and_counter_only(self):
        self.assertEqual(bq.reserve_request(self.ledger, 900, NOV), (True, 1))
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.with_name(self.ledger.name + '.lock').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.ledger.read_text()),
                         {'version': 1, 'month_utc': '2026-11', 'attempts': 1})

    def test_monthly_cap_is_conservative(self):
        self.assertEqual(bq.reserve_request(self.ledger, 2, NOV), (True, 1))
        self.assertEqual(bq.reserve_request(self.ledger, 2, NOV), (True, 2))
        self.assertEqual(bq.reserve_request(self.ledger, 2, NOV), (False, 2))
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'], 2)

    def test_utc_month_rollover(self):
        self.assertEqual(bq.reserve_request(self.ledger, 1, NOV), (True, 1))
        self.assertEqual(bq.reserve_request(self.ledger, 1, DEC), (True, 1))
        self.assertEqual(json.loads(self.ledger.read_text())['month_utc'], '2026-12')

    def test_refuses_symlinked_ledger(self):
        victim = Path(self.temp.name) / 'victim'
        victim.write_text('unchanged')
        self.ledger.symlink_to(victim)
        with self.assertRaisesRegex(bq.BraveQuotaError, 'symlink'):
            bq.reserve_request(self.ledger, 900, NOV)
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_refuses_symlinked_lock(self):
        victim = Path(self.temp.name) / 'victim'
        victim.write_text('unchanged')
        self.ledger.with_name(self.ledger.name + '.lock').symlink_to(victim)
        with self.assertRaisesRegex(bq.BraveQuotaError, 'symlink'):
            bq.reserve_request(self.ledger, 900, NOV)
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_rejects_corruption_instead_of_resetting_usage(self):
        self.ledger.write_text('{bad')
        self.ledger.chmod(0o600)
        with self.assertRaisesRegex(bq.BraveQuotaError, 'malformed'):
            bq.reserve_request(self.ledger, 900, NOV)

    def test_rejects_public_ledger_permissions(self):
        self.ledger.write_text('{"version":1,"month_utc":"2026-11","attempts":1}')
        self.ledger.chmod(0o644)
        with self.assertRaisesRegex(bq.BraveQuotaError, 'private regular file'):
            bq.reserve_request(self.ledger, 900, NOV)

    def test_rejects_bad_limit(self):
        for limit in (0, -1, 1001):
            with self.subTest(limit=limit), self.assertRaises(bq.BraveQuotaError):
                bq.reserve_request(self.ledger, limit, NOV)

    def test_no_unlocked_overbooking_across_processes(self):
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as executor:
            results = list(executor.map(reserve_for_process,
                                        [(str(self.ledger), 5)] * 16))
        self.assertEqual(sum(results), 5)
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'], 5)


class BraveTransientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.domains = root / 'domains.json'
        self.domains.write_text(json.dumps({'domains': ['.co.id']}))
        self.config = root / 'config.json'
        self.config.write_text(json.dumps({
            'QUERY_KEYWORDS': ['slot', 'gacor'],
            'GOOGLE_API_KEY': 'GOOG', 'GOOGLE_CX': 'CX',
            'BRAVE_SEARCH_API_KEY': 'BRAVE_KEY',
        }))
        self.ledger = root / 'brave-quota.json'
        self.state = root / 'seen.json'
        self.args = argparse.Namespace(
            domains=str(self.domains), config=str(self.config),
            providers='brave', scope_limit=14, max_keywords=6,
            max_google_requests=1, max_brave_requests=3,
            max_urlscan_requests=1, results_per_request=5,
            preview_limit=5, timeout=8.0, request_interval=0.0,
            state_file=str(self.state), execute=True, acknowledge_quota=True,
            brave_monthly_ledger=str(self.ledger), brave_monthly_request_cap=900)

    def discover(self, mocks=None, now=NOV):
        return ms.discover(self.args, providers=mocks, now=now)

    def test_no_brave_url_in_output_or_seen_state(self):
        brave = mock.Mock()
        brave.search.return_value = [
            'https://private-unique.co.id/slot?token=SENSITIVE',
            'https://private-unique.co.id/slot?token=DIFFERENT',
        ]
        result = self.discover({'brave': brave})
        self.assertEqual(result['api_requests'], 1)
        self.assertTrue(result['ok'])
        self.assertEqual(result['brave_transient_valid_results'], 2)
        self.assertEqual(result['brave_transient_unique_results'], 1)
        self.assertEqual(result['preview_candidates'], [])
        self.assertEqual(result['new_candidates'], 0)
        self.assertFalse(self.state.exists())
        data = json.dumps(result)
        self.assertNotIn('private-unique', data)
        self.assertNotIn('SENSITIVE', data)
        self.assertNotIn('BRAVE_KEY', data)
        self.assertNotIn('brave', (self.ledger.read_text()))

    def test_brave_does_not_annotate_independent_google_result(self):
        self.args.providers = 'google,brave'
        google = mock.Mock()
        google.search.return_value = ['https://same.co.id/some-path']
        brave = mock.Mock()
        brave.search.return_value = ['https://same.co.id/some-path', 'https://secret.co.id/x']
        result = self.discover({'brave': brave, 'google': google})
        self.assertEqual(len(result['preview_candidates']), 1)
        self.assertEqual(result['preview_candidates'][0]['sources'], ['google'])
        self.assertNotIn('secret.co.id', json.dumps(result))
        self.assertNotIn(ms.hash_url('https://secret.co.id/x'), self.state.read_text())
        self.assertIn(ms.hash_url('https://same.co.id/some-path'), self.state.read_text())

    def test_explicit_ledger_required_for_execution(self):
        self.args.brave_monthly_ledger = ''
        brave = mock.Mock()
        result = self.discover({'brave': brave})
        self.assertEqual(result['providers']['brave']['status'], 'skipped_missing_quota_ledger')
        self.assertEqual(result['api_requests'], 0)
        brave.search.assert_not_called()

    def test_cap_enforced_before_request_and_provider_stops(self):
        self.args.brave_monthly_request_cap = 1
        self.domains.write_text(json.dumps({'domains': ['.co.id', '.web.id']}))
        brave = mock.Mock()
        brave.search.return_value = ['https://test.co.id/x']
        result = self.discover({'brave': brave})
        self.assertEqual(result['providers']['brave']['status'], 'skipped_monthly_cap')
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(brave.search.call_count, 1)
        other = self.discover({'brave': brave})
        self.assertEqual(other['api_requests'], 0)
        self.assertEqual(brave.search.call_count, 1)

    def test_transport_failure_keeps_conservative_reservation(self):
        brave = mock.Mock()
        brave.search.side_effect = RuntimeError('secret response BRAVE_KEY')
        result = self.discover({'brave': brave})
        self.assertFalse(result['ok'])
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'], 1)
        self.assertNotIn('BRAVE_KEY', json.dumps(result))

    def test_env_only_brave_key_without_modifying_config(self):
        self.config.write_text(json.dumps({'QUERY_KEYWORDS': ['slot', 'gacor']}))
        with mock.patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': 'SENSITIVE_BRAVE'}):
            plan_args = argparse.Namespace(**vars(self.args))
            plan_args.execute = False
            data = ms.discover(plan_args, now=NOV)
        self.assertTrue(data['providers']['brave']['configured'])
        self.assertNotIn('SENSITIVE_BRAVE', json.dumps(data))

    def test_no_ledger_write_in_plan(self):
        self.args.execute = False
        result = self.discover()
        self.assertEqual(result['mode'], 'plan')
        self.assertFalse(self.ledger.exists())


if __name__ == '__main__':
    unittest.main()
