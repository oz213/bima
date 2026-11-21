"""BIMA v4.3.7 /cek > discovery > Registry, offline SQLite tests only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from discovery_priority import (
    PRIORITY_SCHEMA, DiscoveryPacer, cleanup_finished_priorities,
    find_next_discovery_job, has_priority_table, pending_priority_count,
)
from network_retry_queue import RetryPacer, choose_next_job
from test_discovery_queue_v436 import QueueFixture, candidate, record

NOW = '2026-11-21T05:00:00Z'


def db():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          error_category TEXT, next_retry_at TEXT, updated_at TEXT
        );
        CREATE INDEX idx_jobs_status ON jobs(status,id);
    ''')
    return conn


def add_job(conn, number, status='pending', source=None, when=NOW):
    conn.execute('INSERT INTO jobs(id,url,status) VALUES(?,?,?)',
                 (number, f'https://example{number}.co.id/', status))
    if source:
        conn.execute(
            'INSERT INTO discovery_job_priority(job_id,provider,discovered_at) VALUES (?,?,?)',
            (number, source, when))


class PriorityHelpers(unittest.TestCase):
    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)

    def test_no_priority_table_no_side_effects(self):
        add_job(self.conn, 1)
        self.assertFalse(has_priority_table(self.conn))
        self.assertIsNone(find_next_discovery_job(self.conn))
        self.assertEqual(pending_priority_count(self.conn), 0)
        self.assertFalse(has_priority_table(self.conn))

    def test_pending_only_and_preserve_failed_inflight(self):
        self.conn.execute(PRIORITY_SCHEMA)
        add_job(self.conn, 1, 'complete', 'google')
        add_job(self.conn, 2, 'http_inflight', 'urlscan')
        add_job(self.conn, 3, 'pending', 'google')
        add_job(self.conn, 4, 'pending', 'brave')
        self.assertEqual(find_next_discovery_job(self.conn)[0], 3)
        self.assertEqual(pending_priority_count(self.conn), 2)
        self.conn.execute("UPDATE jobs SET status='complete' WHERE id=3")
        self.assertEqual(find_next_discovery_job(self.conn)[0], 4)
        self.assertEqual(cleanup_finished_priorities(self.conn), 2)
        self.assertEqual(pending_priority_count(self.conn), 1)

    def test_inflight_requeued_retains_priority(self):
        self.conn.execute(PRIORITY_SCHEMA)
        add_job(self.conn, 1, 'pending', 'google')
        self.conn.execute("UPDATE jobs SET status='http_inflight' WHERE id=1")
        self.assertIsNone(find_next_discovery_job(self.conn))
        self.conn.execute("UPDATE jobs SET status='pending' WHERE id=1")
        self.assertEqual(find_next_discovery_job(self.conn)[0], 1)


class PriorityDispatch(unittest.TestCase):
    def setUp(self):
        self.conn = db()
        self.conn.execute(PRIORITY_SCHEMA)
        self.addCleanup(self.conn.close)

    def call(self, last_id=0, pacer=None, priority=None, dpace=None, clock=0):
        return choose_next_job(
            self.conn, last_id, interactive=True, priority=priority, pacer=pacer,
            now_utc=NOW, now_monotonic=clock, max_attempts=3,
            discovery_pacer=dpace,
        )

    def test_google_before_older_registry_pending(self):
        add_job(self.conn, 1)
        add_job(self.conn, 2)
        add_job(self.conn, 99, source='google')
        row, kind = self.call(dpace=DiscoveryPacer())
        self.assertEqual((row[0], kind), (99, 'discovery'))

    def test_telegram_check_first(self):
        add_job(self.conn, 1)
        add_job(self.conn, 20, source='google')
        row, kind = self.call(priority=(1, 'https://example1.co.id/'),
                              dpace=DiscoveryPacer())
        self.assertEqual((row[0], kind), (1, 'telegram'))

    def test_four_discovery_one_registry_fairness(self):
        for number in (1, 2, 3, 4):
            add_job(self.conn, number)
        for number in range(20, 34):
            add_job(self.conn, number, source='google')
        disc = DiscoveryPacer(max_burst=4)
        last_id = 0
        kinds = []
        ids = []
        for clock in range(12):
            row, kind = self.call(last_id, dpace=disc, clock=clock)
            kinds.append(kind)
            ids.append(row[0])
            disc.record(kind)
            if kind == 'routine':
                last_id = row[0]
            self.conn.execute("UPDATE jobs SET status='http_inflight' WHERE id=?", (row[0],))
        self.assertEqual(kinds[:10], ['discovery'] * 4 + ['routine'] + ['discovery'] * 4 + ['routine'])
        self.assertEqual(ids[4], 1)
        self.assertEqual(ids[9], 2)
        self.assertEqual(len(set(ids)), 12)

    def test_telegram_does_not_reset_discovery_fairness(self):
        add_job(self.conn, 1)
        add_job(self.conn, 5, source='urlscan')
        disc = DiscoveryPacer(max_burst=4)
        for _ in range(4):
            disc.record('discovery')
        disc.record('telegram')
        row, kind = self.call(dpace=disc)
        self.assertEqual((row[0], kind), (1, 'routine'))

    def test_due_network_retry_preserved_during_discovery_burst(self):
        add_job(self.conn, 1)
        add_job(self.conn, 20, source='google')
        self.conn.execute("INSERT INTO jobs(id,url,status,attempts,error_category,next_retry_at) "
                          "VALUES (31,'https://retry.co.id','retry_http',1,'timeout',?)", (NOW,))
        retry = RetryPacer(every=5)
        disc = DiscoveryPacer()
        for i in range(4):
            retry.record('discovery', float(i))
            disc.record('discovery')
        row, kind = self.call(pacer=retry, dpace=disc, clock=5)
        self.assertEqual((row[0], kind), (31, 'retry'))
        row, kind = self.call(pacer=retry, dpace=disc, priority=(1,'https://example1.co.id/'), clock=6)
        self.assertEqual((row[0], kind), (1, 'telegram'))

    def test_discovery_only_no_registry_runs_unbounded_until_empty(self):
        add_job(self.conn, 10, source='google')
        add_job(self.conn, 11, source='urlscan')
        disc = DiscoveryPacer(max_burst=1)
        disc.record('discovery')
        row, kind = self.call(dpace=disc)
        self.assertEqual((row[0], kind), (10, 'discovery'))

    def test_retry_pacer_accepts_discovery_kind(self):
        pacer = RetryPacer(every=5)
        pacer.record('discovery', 0.0)
        self.assertEqual(pacer.routines_since_retry, 1)
        self.assertFalse(pacer.can_retry(1.0, routine_available=True))


class ImportPriority(QueueFixture):
    def priorities(self):
        with sqlite3.connect(self.db) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_job_priority'").fetchone()
            if not exists:
                return []
            return conn.execute('SELECT job_id,provider FROM discovery_job_priority ORDER BY job_id').fetchall()

    def test_read_only_preview_never_creates_priority_table(self):
        result = self.preview()
        self.assertEqual(result['would_insert'], 3)
        self.assertEqual(result['pending_discovery_priority'], 0)
        self.assertEqual(self.priorities(), [])

    def test_apply_tags_google_jobs(self):
        result = self.apply()
        self.assertEqual(result['new_jobs'], 3)
        self.assertEqual(result['priority_added'], 3)
        self.assertEqual([source for _, source in self.priorities()], ['google'] * 3)

    def test_existing_pending_registry_promoted_without_requeue(self):
        self.queue(self.urls[0], 'pending', 2, 'existing')
        self.args.max_pending = 1  # backlog already full
        result = self.apply()
        self.assertEqual(result['new_jobs'], 0)
        self.assertEqual(result['promoted_existing_pending'], 1)
        self.assertEqual(result['pending_after'], 1)
        self.assertEqual(self.rows()[0][-2:], (2, 'existing'))
        self.assertEqual(len(self.priorities()), 1)

    def test_existing_alerted_never_promoted_or_reset(self):
        self.queue(self.urls[0], 'alerted', 4, 'already_alerted')
        self.assertEqual(self.apply()['new_jobs'], 2)
        self.assertNotIn((1, 'google'), self.priorities())
        self.assertEqual(self.rows()[0][-2:], (4, 'already_alerted'))

    def test_urlscan_provider_preserved(self):
        self.write_records(record([candidate('https://z.web.id/path', 'urlscan',
                                             matched='.web.id', scopes=['.web.id'])]))
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(self.priorities()[0][1], 'urlscan')

    def test_brave_not_persisted_even_with_mixed_sources(self):
        self.write_records(record([
            candidate('https://a.co.id/a', ['google', 'brave']),
            candidate('https://a.co.id/b', 'brave'),
            candidate('https://a.co.id/c', 'google'),
        ], version='4.3.4'))
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual([provider for _,provider in self.priorities()], ['google'])

    def test_active_lane_capacity_prevents_starvation_of_registry(self):
        self.args.max_priority = 1
        result = self.apply()
        self.assertEqual(result['new_jobs'], 1)
        self.assertEqual(result['pending_discovery_priority_after'], 1)
        self.assertEqual(self.apply()['new_jobs'], 0)

    def test_opt_in_backlog_override_still_bounded(self):
        self.queue('https://registry-only.co.id/')
        self.args.max_pending = 1
        self.args.max_new = 2
        self.args.max_priority = 2
        self.assertEqual(self.preview()['would_insert'], 0)
        self.args.allow_priority_over_pending_ceiling = True
        self.assertEqual(self.preview()['would_insert'], 2)
        result = self.apply()
        self.assertEqual(result['new_jobs'], 2)
        self.assertEqual(result['pending_after'], 3)
        self.assertEqual(result['pending_discovery_priority_after'], 2)
        self.assertEqual(self.apply()['new_jobs'], 0)

    def test_cleanup_finished_priority_on_followup_apply(self):
        self.args.max_priority = 1
        self.assertEqual(self.apply()['new_jobs'], 1)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE jobs SET status='complete' WHERE id=1")
            conn.commit()
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(len(self.priorities()), 1)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools' / 'upgrade_discovery_priority_crawler.py'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('upgrade_discovery_priority_crawler', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.mod = module
        self.valid_source = '''from network_retry_queue import RetryPacer, choose_next_job

def worker(args):
    def log(x): pass
    retry_pacer = None
    if retry_pacer is not None:
        log(f'Auto network retry enabled: every={retry_pacer.every}, '
            f'max_per_minute={retry_pacer.max_per_minute}')
    limit = args.limit or float('inf')
    def submit_one(pool):
        conn, last_id, interactive, priority, now_mono = None, 0, True, None, 0
        row, kind = choose_next_job(
            conn, last_id, interactive=interactive, priority=priority,
            pacer=retry_pacer, now_utc=utcnow(), now_monotonic=now_mono,
            max_attempts=args.max_network_attempts)
        id_, url = row
        if retry_pacer is not None:
            retry_pacer.record(kind, now_mono)
        futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)
'''

    def test_guarded_upgrade_repeat_safe(self):
        # Actual anchors are indented more than this synthetic function.
        m = self.mod
        source = self.valid_source.replace('    if retry_pacer is not None:\n',
                                           '        if retry_pacer is not None:\n', 1)
        # Better test the source behavior using an exact template rather than
        # modifying the live crawler or weakening guard checks.
        source = (m.IMPORT_OLD + '\n' +
                  'def log(x): pass\n'
                  'def worker(args):\n'
                  '    retry_pacer = None\n'
                  '    if False:\n'
                  '        if retry_pacer is not None:\n'
                  "            log(f'Auto network retry enabled: every={retry_pacer.every}, '\n"
                  "                f'max_per_minute={retry_pacer.max_per_minute}')\n"
                  "        limit = args.limit or float('inf')\n"
                  '    def go():\n'
                  '        if False:\n'
                  '            if True:\n'
                  '                row,kind = choose_next_job(\n'
                  '                    None,0,interactive=True,priority=None,\n'
                  '                    pacer=retry_pacer, now_utc=utcnow(), now_monotonic=now_mono,\n'
                  '                    max_attempts=args.max_network_attempts)\n'
                  '                if retry_pacer is not None:\n'
                  '                    retry_pacer.record(kind, now_mono)\n'
                  '            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)\n')
        upgraded, status = m.plan_change(source)
        self.assertEqual(status, 'compatible version')
        self.assertIn('discovery_pacer=discovery_pacer', upgraded)
        self.assertIn('discovery_pacer.record(kind)', upgraded)
        again, status = m.plan_change(upgraded)
        self.assertEqual(status, 'already upgraded')
        self.assertEqual(again, upgraded)

    def test_partial_source_fails_closed(self):
        with self.assertRaises(ValueError):
            self.mod.plan_change(self.mod.IMPORT_NEW + '\n')


if __name__ == '__main__':
    unittest.main()
