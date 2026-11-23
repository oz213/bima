"""BIMA Discovery v10: unlimited eligible volume, bounded source safety.

Offline regression tests; no provider API calls or BIMA alert delivery.
"""
from contextlib import closing
import sqlite3
import unittest

import daily_discovery_import as daily
from test_daily_discovery_v438 import DailyImportFixture


class UnlimitedDiscoveryTests(DailyImportFixture):
    def enable_unlimited(self):
        self.args.max_new = 0
        self.args.max_priority = 0
        self.args.max_pending = 0
        self.args.allow_priority_over_pending_ceiling = False

    def test_all_new_candidates_across_multiple_spools(self):
        self.enable_unlimited()
        first = self.write_run('AAAAAAA1', [f'https://a.co.id/{i}' for i in range(120)])
        second = self.write_run('AAAAAAA2', [f'https://a.co.id/{i}' for i in range(120, 200)])
        preview = daily.drain(self.args)
        self.assertEqual(preview['new_jobs'], 200)
        self.assertEqual(self.rows(), [])  # Preview must not mutate the DB.
        result = self.apply()
        self.assertEqual(result['new_jobs'], 200)
        self.assertEqual(result['priority_added'], 200)
        self.assertEqual(result['spool_files_drained'], 2)
        self.assertEqual(result['spool_files_pending_after'], 0)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(len(self.rows()), 200)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM discovery_job_priority').fetchone()[0], 200)
            names = [row[1] for row in conn.execute(
                'PRAGMA index_list(discovery_job_priority)').fetchall()]
            self.assertIn('idx_discovery_priority_fifo_v10', names)
        self.assertEqual(self.apply()['new_jobs'], 0)  # Repeated import is idempotent.

    def test_existing_registry_pending_is_promoted_despite_backlog(self):
        self.enable_unlimited()
        self.write_run('AAAAAAA3', [
            'https://a.co.id/existing',
            'https://a.co.id/new',
        ])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs(url,status,attempts) VALUES (?,'pending',0)",
                         ('https://a.co.id/existing',))
            for i in range(30):
                conn.execute('INSERT INTO jobs(url) VALUES (?)',
                             (f'https://other.co.id/registry-{i}',))
        result = self.apply()
        self.assertEqual(result['new_jobs'], 1)
        self.assertEqual(result['promoted_existing_pending'], 1)
        self.assertEqual(result['priority_added'], 2)
        with closing(sqlite3.connect(self.db)) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0]
            self.assertEqual(pending, 32)

    def test_brave_still_excluded_and_bad_limits_rejected(self):
        self.enable_unlimited()
        self.write_run('AAAAAAA4', ['https://a.co.id/brave-only'], source='brave')
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(self.rows(), [])
        self.args.max_new = -1
        with self.assertRaises(daily.QueueImportError):
            daily.drain(self.args)


if __name__ == '__main__':
    unittest.main()
