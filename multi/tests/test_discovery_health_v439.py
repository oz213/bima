"""BIMA v4.3.9 read-only operational health checks (no API calls)."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import discovery_health as health


UTC = timezone.utc
SCHEDULE = '30 20 * * * BIMA_DISCOVERY_AUTO_IMPORT=1 /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh'
NOW = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)


class HealthTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir(mode=0o700)
        self.db = self.root / 'db.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, url TEXT UNIQUE, status TEXT NOT NULL
            );
            INSERT INTO jobs(url,status) VALUES ('https://registry.id','pending');
            INSERT INTO jobs(url,status) VALUES ('https://alert.id','pending_alert');
            CREATE TABLE discovery_job_priority (
              job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
              provider TEXT NOT NULL, discovered_at TEXT NOT NULL
            );
            INSERT INTO jobs(url,status) VALUES ('https://x.web.id','complete');
            INSERT INTO discovery_job_priority VALUES (3,'google','2026-11-21T06:28:00Z');
            ''')

    def call(self, **kw):
        return health.summarize(self.logs, self.db, now=NOW, crontab=SCHEDULE,
                                enabled_date=date(2026, 11, 21), **kw)

    def import_log(self, body=None, mtime=NOW):
        path = self.logs / 'multi-source-import.jsonl'
        path.write_text(json.dumps(body if body is not None else {
            'ok': True, 'new_jobs': 2, 'priority_added': 2,
            'spool_files_pending_after': 1, 'brave_ingestion_enabled': False
        }) + '\n')
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))

    def test_waiting_before_first_run(self):
        result = self.call()
        self.assertEqual(result['status'], 'awaiting_first_run')
        self.assertEqual(result['last_import']['record'], 'missing')
        self.assertFalse(result['last_import']['today_started_marker_exists'])

    def test_missed_first_run_detected_next_utc_day(self):
        result = health.summarize(self.logs, self.db,
                                  now=NOW + timedelta(days=1), crontab=SCHEDULE,
                                  enabled_date=date(2026, 11, 21))
        self.assertEqual(result['status'], 'attention')
        self.assertIn('scheduled_import_not_observed', result['problems'])

    def test_valid_import_output_is_ok(self):
        self.import_log()
        result = self.call()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['last_import']['new_jobs'], 2)
        self.assertEqual(result['database']['jobs']['pending_alert'], 1)
        self.assertEqual(result['database']['priority_counts']['google']['complete'], 1)

    def test_too_many_new_jobs_is_attention(self):
        self.import_log({'ok': True, 'new_jobs': 6, 'brave_ingestion_enabled': False})
        self.assertIn('new_job_limit', self.call()['problems'])

    def test_brave_ingestion_is_attention(self):
        self.import_log({'ok': True, 'new_jobs': 1, 'brave_ingestion_enabled': True})
        self.assertIn('brave_ingestion_unexpected', self.call()['problems'])

    def test_missing_brave_ingestion_field_is_attention(self):
        self.import_log({'ok': True, 'new_jobs': 1})
        self.assertIn('brave_ingestion_unexpected', self.call()['problems'])

    def test_bad_import_run_is_attention(self):
        self.import_log({'ok': False, 'new_jobs': 0, 'brave_ingestion_enabled': False})
        self.assertIn('import_run_error', self.call()['problems'])

    def test_stale_success_is_attention(self):
        self.import_log(mtime=NOW - timedelta(hours=40))
        self.assertIn('import_log_stale', self.call()['problems'])

    def test_older_success_does_not_mask_missed_cron(self):
        self.import_log(mtime=NOW)
        result = health.summarize(self.logs, self.db,
                                  now=datetime(2026, 11, 22, 23, 0, tzinfo=UTC),
                                  crontab=SCHEDULE, enabled_date=date(2026, 11, 21))
        self.assertIn('scheduled_import_not_observed', result['problems'])

    def test_last_jsonl_only_reports_counters_not_sensitive_content(self):
        content = {'ok': True, 'api_requests': 14, 'new_candidates': 3,
                   'preview_candidates': [{'url': 'https://private-example.id/secret?token=TOPSECRET'}],
                   'providers': {'google': {'status': 'ok', 'requests': 14, 'valid_results': 3},
                                 'brave': {'status': 'error', 'error': 'TOPSECRET'}}}
        path = self.logs / 'multi-source.jsonl'
        path.write_text(json.dumps(content) + '\n')
        result = self.call()
        payload = json.dumps(result)
        self.assertNotIn('TOPSECRET', payload)
        self.assertNotIn('private-example.id', payload)
        self.assertEqual(result['last_discovery']['providers']['google']['requests'], 14)

    def test_cron_exactly_one_expected(self):
        self.assertEqual(health._cron_status(SCHEDULE)['state'], 'ok')
        self.assertEqual(health._cron_status(SCHEDULE + '\n' + SCHEDULE)['state'], 'attention')
        self.assertEqual(health._cron_status('# ' + SCHEDULE + '\n')['state'], 'attention')

    def test_brave_cron_flag_is_attention(self):
        enabled = SCHEDULE.replace('BIMA_DISCOVERY_AUTO_IMPORT=1 ',
                                   'BIMA_DISCOVERY_AUTO_IMPORT=1 BIMA_ENABLE_BRAVE_DAILY=1 ')
        self.assertTrue(health._cron_status(enabled)['brave_daily_enabled'])
        self.assertEqual(health._cron_status(enabled)['state'], 'attention')

    def test_google_provider_skipped_is_attention(self):
        (self.logs / 'multi-source.jsonl').write_text(json.dumps({
            'ok': True, 'mode': 'api_preview', 'providers': {
                'google': {'status': 'skipped_missing_credentials', 'requests': 0}
            }
        }) + '\n')
        self.assertIn('google_provider_not_ok', self.call()['problems'])

    def test_global_brave_variable_is_attention(self):
        actual = 'BIMA_ENABLE_BRAVE_DAILY=1\n' + SCHEDULE
        self.assertEqual(health._cron_status(actual)['state'], 'attention')

    def test_missing_jobs_table_is_attention(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TABLE discovery_job_priority')
            conn.execute('DROP TABLE jobs')
        self.assertIn('jobs_database', self.call()['problems'])

    def test_spool_missing_allowed_before_first_run(self):
        self.assertEqual(self.call()['spool']['state'], 'not_created')

    def test_spool_private_and_pending_count(self):
        folder = self.logs / 'multi-source-pending'
        folder.mkdir(mode=0o700)
        p = folder / 'pending-20261121T203000Z-ABCDEFGH.jsonl'
        p.write_text('{}\n')
        p.chmod(0o600)
        self.assertEqual(self.call()['spool']['pending_files'], 1)

    def test_spool_bad_permissions_is_attention(self):
        folder = self.logs / 'multi-source-pending'
        folder.mkdir(mode=0o700)
        folder.chmod(0o755)
        self.assertIn('spool_security', self.call()['problems'])

    def test_spool_failed_file_is_attention(self):
        folder = self.logs / 'multi-source-pending'
        folder.mkdir(mode=0o700)
        (folder / 'pending-20261121T203000Z-ABCDEFGH.jsonl.failed').write_text('')
        self.assertIn('provider_failed_spool', self.call()['problems'])

    def test_priority_pending_limit_detected(self):
        with closing(sqlite3.connect(self.db)) as conn:
            for i in range(25):
                conn.execute('INSERT INTO jobs(url,status) VALUES (?,?)',
                             (f'https://extra{i}.id', 'pending'))
                id_ = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.execute('INSERT INTO discovery_job_priority VALUES (?,?,?)',
                             (id_, 'google', '2026-11-21T06:28:00Z'))
            conn.commit()
        self.assertIn('discovery_priority_limit', self.call()['problems'])

    def test_last_jsonl_handles_non_object(self):
        (self.logs / 'multi-source-import.jsonl').write_text('[1,2,3]\n')
        self.assertIn('import_log_format', self.call()['problems'])

    def test_does_not_create_files_or_change_database(self):
        before = self.db.stat().st_mtime_ns
        self.call()
        self.assertEqual(self.db.stat().st_mtime_ns, before)
        self.assertFalse((self.logs / 'multi-source-pending').exists())
        self.assertFalse((self.logs / 'multi-source-import.jsonl').exists())

    def test_untrusted_status_never_printed(self):
        result = health._provider_summary({'google': {'status': 'TOPSECRET', 'requests': 'oops'}})
        self.assertEqual(result['google']['status'], 'unknown')
        self.assertIsNone(result['google']['requests'])


if __name__ == '__main__':
    unittest.main()
