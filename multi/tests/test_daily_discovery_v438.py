"""BIMA v4.3.8: no-network FIFO spool and opt-in daily scheduler tests."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import daily_discovery_import as daily
from test_discovery_queue_v436 import SCHEMA, candidate, record


class DailyImportFixture(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.spool = self.root / 'private-spool'
        self.spool.mkdir(mode=0o700)
        self.db = self.root / 'prod.db'
        self.domains = self.root / 'domains.json'
        self.domains.write_text(json.dumps({'domains': ['.co.id', '.id']}))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        self.args = argparse.Namespace(
            spool_dir=str(self.spool), db=str(self.db), domains=str(self.domains),
            max_new=2, max_priority=5, max_pending=10000,
            allow_priority_over_pending_ceiling=False,
            apply=False, acknowledge_queue_write=False,
        )

    def write_run(self, code, urls, source='google'):
        path = self.spool / ('pending-20261121T062800Z-' + code + '.jsonl')
        path.write_text(json.dumps(record([candidate(url, source) for url in urls])) + '\n')
        path.chmod(0o600)
        return path

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT url,status,attempts FROM jobs ORDER BY id').fetchall()

    def apply(self):
        self.args.apply = True
        self.args.acknowledge_queue_write = True
        return daily.drain(self.args)


class DailyImportTests(DailyImportFixture):
    def test_preview_is_read_only_and_keeps_spool(self):
        path = self.write_run('ABCDEFGH', ['https://a.co.id/1'])
        r = daily.drain(self.args)
        self.assertTrue(r['db_read_only'])
        self.assertEqual(r['new_jobs'], 1)  # preview estimate only
        self.assertEqual(self.rows(), [])
        self.assertTrue(path.exists())

    def test_apply_has_explicit_double_gate(self):
        path = self.write_run('ABCDEFGH', ['https://a.co.id/1'])
        self.args.apply = True
        with self.assertRaisesRegex(daily.QueueImportError, 'acknowledge'):
            daily.drain(self.args)
        self.assertEqual(self.rows(), [])
        self.assertTrue(path.exists())

    def test_drains_complete_file_after_import(self):
        path = self.write_run('ABCDEFGH', ['https://a.co.id/1'])
        result = self.apply()
        self.assertEqual(result['new_jobs'], 1)
        self.assertEqual(result['priority_added'], 1)
        self.assertEqual(result['spool_files_drained'], 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.rows(), [('https://a.co.id/1', 'pending', 0)])

    def test_partial_file_kept_and_replayed_without_duplicates(self):
        path = self.write_run('ABCDEFGH', [f'https://a.co.id/{x}' for x in range(4)])
        first = self.apply()
        self.assertEqual(first['new_jobs'], 2)
        self.assertTrue(path.exists())
        second = self.apply()
        self.assertEqual(second['new_jobs'], 2)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.rows()), 4)
        third = self.apply()
        self.assertEqual(third['new_jobs'], 0)

    def test_fifo_before_later_runs(self):
        older = self.write_run('ZZZZZZZ1', ['https://a.co.id/old'])
        later = self.write_run('AAAAAAA2', ['https://a.co.id/new'])
        older.rename(self.spool / 'pending-20261120T062800Z-ZZZZZZZ1.jsonl')
        self.args.max_new = 1
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertTrue(later.exists())
        self.assertEqual(self.rows()[0][0], 'https://a.co.id/old')

    def test_cap_five_new_jobs_per_invocation(self):
        self.write_run('AAAAAAA1', [f'https://a.co.id/{x}' for x in range(20)])
        self.args.max_new = 5
        self.args.max_priority = 20
        self.assertEqual(self.apply()['new_jobs'], 5)
        self.assertEqual(len(self.rows()), 5)

    def test_existing_complete_never_resubmitted(self):
        path = self.write_run('AAAAAAA1', ['https://a.co.id/old'])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs(url,status,attempts) VALUES (?,'complete',3)",
                         ('https://a.co.id/old',))
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.rows(), [('https://a.co.id/old', 'complete', 3)])

    def test_existing_pending_registry_job_is_promoted_not_duplicated(self):
        path = self.write_run('AAAAAAA1', ['https://a.co.id/existing'])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs(url,status,attempts) VALUES (?,'pending',0)",
                         ('https://a.co.id/existing',))
        result = self.apply()
        self.assertEqual(result['new_jobs'], 0)
        self.assertEqual(result['promoted_existing_pending'], 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(path.exists())

    def test_full_priority_lane_defers_until_job_finishes(self):
        self.args.max_priority = 1
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs(url,status,attempts) VALUES (?,'pending',0)",
                         ('https://a.co.id/inflight',))
            conn.executescript('''CREATE TABLE discovery_job_priority (
                job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
                provider TEXT NOT NULL, discovered_at TEXT NOT NULL
            );''')
            conn.execute("INSERT INTO discovery_job_priority(job_id,provider,discovered_at) "
                         "VALUES (1,'google','2026-11-21T01:00:00Z')")
        path = self.write_run('AAAAAAA1', ['https://a.co.id/next'])
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertTrue(path.exists())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE jobs SET status='complete' WHERE url=?",
                         ('https://a.co.id/inflight',))
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertFalse(path.exists())

    def test_brave_url_never_imported(self):
        path = self.write_run('AAAAAAA1', ['https://a.co.id/secret'], 'brave')
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(self.rows(), [])
        self.assertFalse(path.exists())

    def test_blocks_over_pending_without_explicit_override(self):
        self.write_run('AAAAAAA1', ['https://a.co.id/new'])
        self.args.max_pending = 1
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO jobs(url) VALUES (?)', ('https://a.co.id/existing',))
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.args.allow_priority_over_pending_ceiling = True
        self.assertEqual(self.apply()['new_jobs'], 1)

    def test_rejects_insecure_spool_file(self):
        path = self.write_run('AAAAAAA1', ['https://a.co.id/1'])
        path.chmod(0o644)
        with self.assertRaisesRegex(daily.QueueImportError, 'unsafe'):
            daily.drain(self.args)
        self.assertEqual(self.rows(), [])

    def test_rejects_spool_symlink(self):
        path = self.spool / 'pending-20261121T062800Z-AAAAAAA1.jsonl'
        path.symlink_to(self.domains)
        with self.assertRaisesRegex(daily.QueueImportError, 'unsafe'):
            daily.drain(self.args)
        self.assertEqual(self.rows(), [])

    def test_rejects_world_readable_directory(self):
        self.spool.chmod(0o755)
        with self.assertRaisesRegex(daily.QueueImportError, 'private'):
            daily.drain(self.args)

    def test_malformed_input_fail_closed_no_job(self):
        path = self.spool / 'pending-20261121T062800Z-AAAAAAA1.jsonl'
        path.write_text('NOT_JSON\n')
        path.chmod(0o600)
        with self.assertRaises(daily.QueueImportError):
            self.apply()
        self.assertEqual(self.rows(), [])
        self.assertTrue(path.exists())

    def test_max_new_above_five_supported(self):
        self.write_run('AAAAAAA1', [f'https://a.co.id/{x}' for x in range(8)])
        self.args.max_new = 8
        self.args.max_priority = 8
        self.assertEqual(self.apply()['new_jobs'], 8)
        self.assertEqual(len(self.rows()), 8)

    def test_skips_nonmatching_files(self):
        (self.spool / 'other.jsonl').write_text('BAD\n')
        self.assertEqual(self.apply()['spool_files_pending_before'], 0)

    def test_missing_db_not_created(self):
        self.write_run('AAAAAAA1', ['https://a.co.id/1'])
        self.args.db = str(self.root / 'missing.db')
        with self.assertRaises(daily.QueueImportError):
            self.apply()
        self.assertFalse(Path(self.args.db).exists())


@unittest.skipUnless(sys.platform.startswith("linux") and Path("/usr/bin/flock").is_file(),
                     "Linux production cron smoke requires /usr/bin/flock")
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.app = self.root / 'repo'
        (self.app / 'tools').mkdir(parents=True)
        (self.app / 'venv' / 'bin').mkdir(parents=True)
        (self.app / 'multi').mkdir()
        source = Path(__file__).resolve().parents[2] / 'tools' / 'run_multisource_daily.sh'
        self.runner = self.app / 'tools' / 'run_multisource_daily.sh'
        shutil.copy2(source, self.runner)
        self.fake_py = self.app / 'venv' / 'bin' / 'python3'
        self.fake_py.write_text('''#!/bin/sh
case "$2" in
  */multi_source_discovery.py)
    printf '%s\\n' "$*" >> "$HOME/provider-args"
    if [ "${FAKE_PROVIDER_FAIL:-0}" = 1 ]; then
      printf '{"ok":false,"mode":"error"}\\n'
      exit 1
    fi
    printf '{"ok":true,"mode":"api_preview","version":"4.3.5","preview_candidates":[]}\\n'
    ;;
  */daily_discovery_import.py)
    printf '%s\\n' "$*" >> "$HOME/import-args"
    printf '{"ok":true,"new_jobs":0}\\n'
    ;;
  *) exit 99 ;;
esac
''')
        self.fake_py.chmod(0o755)
        self.log = self.root / 'logs'
        self.env = dict(os.environ, HOME=str(self.home), BIMA_DISCOVERY_LOG_DIR=str(self.log))
        self.env.pop('BIMA_ENABLE_BRAVE_DAILY', None)
        self.env.pop('BIMA_DISCOVERY_AUTO_IMPORT', None)

    def run_daily(self, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(['/bin/sh', str(self.runner)], cwd=str(self.root), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_default_unchanged_and_no_import(self):
        p = self.run_daily()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((self.home / 'import-args').exists())
        self.assertFalse((self.log / 'multi-source-pending').exists())
        self.assertIn('--providers google,urlscan', (self.home / 'provider-args').read_text())
        self.assertEqual(len((self.log / 'multi-source.jsonl').read_text().splitlines()), 1)

    def test_opt_in_one_import_per_utc_day(self):
        p = self.run_daily(BIMA_DISCOVERY_AUTO_IMPORT='1')
        self.assertEqual(p.returncode, 0, p.stderr)
        args_line = (self.home / 'import-args').read_text()
        self.assertIn('--max-new 0', args_line)
        self.assertIn('--max-priority 0', args_line)
        self.assertIn('--max-pending 0', args_line)
        self.assertIn('--apply --acknowledge-queue-write', (self.home / 'import-args').read_text())
        spool = self.log / 'multi-source-pending'
        self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(list(spool.glob('pending-*.jsonl'))), 1)
        p2 = self.run_daily(BIMA_DISCOVERY_AUTO_IMPORT='1')
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(len((self.home / 'import-args').read_text().splitlines()), 1)
        self.assertEqual(len(list(spool.glob('pending-*.jsonl'))), 2)

    def test_failed_api_leaves_failed_spool_without_import(self):
        p = self.run_daily(BIMA_DISCOVERY_AUTO_IMPORT='1', FAKE_PROVIDER_FAIL='1')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.home / 'import-args').exists())
        self.assertEqual(len(list((self.log / 'multi-source-pending').glob('*.failed'))), 1)

    def test_invalid_auto_flag_fails_before_api(self):
        p = self.run_daily(BIMA_DISCOVERY_AUTO_IMPORT='yes')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.home / 'provider-args').exists())


if __name__ == '__main__':
    unittest.main()
