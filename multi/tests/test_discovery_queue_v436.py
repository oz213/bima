"""BIMA v4.3.6 discovery-to-jobs integration: synthetic, offline, no services."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import discovery_queue as dq


SCHEMA = """
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 url TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 http_ms INTEGER,
 browser_ms INTEGER,
 http_status INTEGER,
 reason TEXT,
 risk TEXT,
 classifier_json TEXT,
 final_url TEXT,
 evidence_path TEXT,
 alert_confidence REAL,
 error TEXT,
 updated_at TEXT,
 route_reason TEXT,
 error_category TEXT,
 classification_ms INTEGER,
 next_retry_at TEXT,
 bert_ms INTEGER,
 dns_result TEXT,
 dns_errno INTEGER,
 dns_host TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status,id);
CREATE TABLE telegram_checks(id INTEGER PRIMARY KEY, job_id INTEGER);
"""


def candidate(url, source='google', matched='.co.id', scopes=None):
    return {
        'url': url,
        'sources': [source] if isinstance(source, str) else source,
        'matched_scope': matched,
        'found_via_scopes': scopes or ['.co.id'],
        'evidence_level': 'search_index_signal' if source != 'urlscan' else 'historical_scan_metadata',
    }


def record(items, **kw):
    return {
        'version': '4.3.5', 'mode': 'api_preview',
        'ok': True, 'preview_candidates': items, 'preview_truncated': False,
        **kw,
    }


class QueueFixture(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name)
        self.db = self.base / 'prod.db'
        self.input = self.base / 'multi-source.jsonl'
        self.domains = self.base / 'domains.json'
        self.domains.write_text(json.dumps({'domains': ['.co.id', '.web.id', '.id']}))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        self.args = argparse.Namespace(
            input=str(self.input), db=str(self.db), domains=str(self.domains),
            max_new=20, max_pending=10000, apply=False,
            acknowledge_queue_write=False,
        )
        self.urls = [f'https://a.co.id/post-{i}' for i in range(3)]
        self.write_records(record([candidate(url) for url in self.urls]))

    def write_records(self, *rows):
        self.input.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT url,status,attempts,reason FROM jobs ORDER BY id').fetchall()

    def queue(self, url, status='pending', attempts=0, reason=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO jobs(url,status,attempts,reason) VALUES (?,?,?,?)',
                         (url, status, attempts, reason))

    def preview(self):
        return dq.audit(self.args)

    def apply(self):
        self.args.apply = True
        self.args.acknowledge_queue_write = True
        return dq.audit(self.args)


class PreviewTests(QueueFixture):
    def test_preview_read_only_and_counts(self):
        with mock.patch.object(dq.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
            data = self.preview()
        self.assertEqual(data['would_insert'], 3)
        self.assertTrue(data['db_read_only'])
        self.assertEqual(data['db_writes'], 0)
        self.assertEqual(data['new_jobs'], 0)
        self.assertEqual(self.rows(), [])
        self.assertIn('mode=ro', connect.call_args.args[0])

    def test_preview_does_not_start_network_or_send_alert(self):
        self.assertNotIn('requests', dq.__dict__)
        self.assertNotIn('crawler_multi', dq.__dict__)
        self.assertEqual(self.preview()['alerts_sent'], 0)

    def test_existing_jobs_excluded_even_if_complete(self):
        self.queue(self.urls[0], 'complete', 2, 'already_completed')
        data = self.preview()
        self.assertEqual(data['already_in_jobs'], 1)
        self.assertEqual(data['would_insert'], 2)

    def test_root_slash_equivalence(self):
        self.queue('https://a.co.id', 'alerted', 3, 'keep')
        self.write_records(record([candidate('https://a.co.id/')]))
        self.assertEqual(self.preview()['would_insert'], 0)

    def test_backpressure_blocks_preview(self):
        self.queue('https://existing.co.id/')
        self.args.max_pending = 1
        data = self.preview()
        self.assertEqual(data['would_insert'], 0)
        self.assertTrue(data['blocked_by_pending_limit'])

    def test_max_new_bounded(self):
        self.args.max_new = 2
        self.assertEqual(self.preview()['would_insert'], 2)

    def test_does_not_create_missing_db(self):
        self.args.db = str(self.base / 'nonexistent.db')
        with self.assertRaises(dq.QueueImportError):
            self.preview()
        self.assertFalse(Path(self.args.db).exists())

    def test_read_log_skips_plan_records(self):
        self.write_records({'mode': 'plan', 'version': '4.3.5'}, record([candidate(self.urls[0])]))
        data = self.preview()
        self.assertEqual(data['skipped_non_preview_records'], 1)
        self.assertEqual(data['unique_valid_candidates'], 1)

    def test_duplicate_entries_across_days(self):
        self.write_records(record([candidate(self.urls[0])]), record([candidate(self.urls[0])]))
        data = self.preview()
        self.assertEqual(data['unique_valid_candidates'], 1)
        self.assertEqual(data['duplicates_in_log'], 1)

    def test_truncation_exposed(self):
        self.write_records(record([candidate(self.urls[0])], preview_truncated=True))
        data = self.preview()
        self.assertEqual(data['truncated_preview_runs'], 1)

    def test_absent_log_fails_closed(self):
        self.input.unlink()
        with self.assertRaises(dq.QueueImportError):
            self.preview()


class ApplyTests(QueueFixture):
    def test_apply_requires_explicit_acknowledgement(self):
        self.args.apply = True
        with self.assertRaisesRegex(dq.QueueImportError, 'acknowledge'):
            dq.audit(self.args)
        self.assertEqual(self.rows(), [])

    def test_apply_inserts_only_pending_defaults(self):
        data = self.apply()
        self.assertEqual(data['new_jobs'], 3)
        self.assertEqual(data['db_writes'], 3)
        self.assertEqual(data['pending_after'], 3)
        self.assertEqual([x[:3] for x in self.rows()],
                         [(url, 'pending', 0) for url in self.urls])

    def test_apply_idempotent_and_preserves_existing_verdict(self):
        self.queue(self.urls[0], 'alerted', 6, 'do_not_retry')
        self.assertEqual(self.apply()['new_jobs'], 2)
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(self.rows()[0], (self.urls[0], 'alerted', 6, 'do_not_retry'))
        self.assertEqual(len(self.rows()), 3)

    def test_apply_obeys_max_new_and_followup_backfill(self):
        self.args.max_new = 1
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(len(self.rows()), 3)

    def test_apply_obeys_pending_ceiling_with_existing_backlog(self):
        self.queue('https://existing.co.id/')
        self.args.max_pending = 2
        self.assertEqual(self.apply()['new_jobs'], 1)
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(len(self.rows()), 2)

    def test_existing_root_seed_not_duplicated(self):
        self.queue('https://a.co.id')
        self.write_records(record([candidate('https://a.co.id/')]))
        self.assertEqual(self.apply()['new_jobs'], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_apply_never_modifies_other_tables(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO telegram_checks(id,job_id) VALUES (1,42)')
        self.apply()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM telegram_checks').fetchall(), [(1,42)])
            self.assertEqual([r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"') if r[0].startswith('proactive_')], [])

    def test_invalid_jsonl_aborts_before_any_write(self):
        self.input.write_bytes(self.input.read_bytes() + b'{INVALID\n')
        with self.assertRaises(dq.QueueImportError):
            self.apply()
        self.assertEqual(self.rows(), [])


class ValidationTests(QueueFixture):
    def test_ignores_brave_from_v434_even_if_google_mixed(self):
        self.write_records(record([
            candidate('https://a.co.id/slot', ['google', 'brave']),
            candidate('https://a.co.id/a', 'brave'),
            candidate('https://a.co.id/b', 'google'),
        ], version='4.3.4'))
        data = self.apply()
        self.assertEqual(data['excluded_brave_entries'], 2)
        self.assertEqual(data['new_jobs'], 1)
        self.assertEqual(self.rows()[0][0], 'https://a.co.id/b')

    def test_rejects_invalid_and_outside_id_without_side_effects(self):
        bad = [
            candidate('https://evil.com/', 'google'),
            candidate('https://co.id.evil.com/', 'urlscan'),
            candidate('https://foo.web.id/', 'google'),
            candidate('file:///etc/passwd', 'google'),
            candidate('https://a.co.id/logout', 'google'),
            candidate('https://user:pass@a.co.id/', 'google'),
            candidate('https://a.co.id/?token=SECRET', 'google'),
            candidate('https://a.co.id/path', 'google', matched='.ac.id'),
        ]
        self.write_records(record(bad))
        data = self.apply()
        self.assertEqual(data['new_jobs'], 0)
        self.assertEqual(data['rejected_entries'], len(bad))
        self.assertEqual(self.rows(), [])

    def test_urlscan_historical_metadata_is_accepted(self):
        self.write_records(record([candidate('https://x.web.id/slot', 'urlscan',
                                             matched='.web.id', scopes=['.web.id'])]))
        self.assertEqual(self.apply()['new_jobs'], 1)

    def test_no_unique_url_index_fails_closed(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE jobs')
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, url TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, updated_at TEXT)")
        with self.assertRaisesRegex(dq.QueueImportError, 'UNIQUE'):
            self.apply()

    def test_incompatible_defaults_abort(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE jobs')
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, url TEXT UNIQUE, status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, updated_at TEXT)")
        with self.assertRaisesRegex(dq.QueueImportError, 'defaults'):
            self.apply()

    def test_limits_allow_unbounded_but_reject_negative_values(self):
        for value in (0, 101):
            self.args.max_new = value
            self.assertEqual(
                self.preview()['would_insert'], len(self.urls)
            )

        self.args.max_new = -1
        with self.assertRaises(dq.QueueImportError):
            self.preview()

    def test_rejects_symlinked_jsonl(self):
        alias = self.base / 'alias.jsonl'
        alias.symlink_to(self.input)
        self.args.input = str(alias)
        with self.assertRaisesRegex(dq.QueueImportError, 'regular file'):
            self.apply()

    def test_rejects_oversized_jsonl_without_db_write(self):
        self.input.write_bytes(b' ' * (dq.MAX_LINE_BYTES + 1) + b'\n')
        with self.assertRaises(dq.QueueImportError):
            self.apply()
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
