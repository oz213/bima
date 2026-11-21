"""Tests for AST-aware v4.1 upgrader. No live database or network operations."""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'tools' / 'upgrade_bima_retry_v41.py'
spec = importlib.util.spec_from_file_location('upgrade_bima_retry_v41', PATH)
upgrader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrader)


SOURCE = '''\
import threading
import time
import requests
from telegram_checks import CHECK_SCHEMA, normalize_input_url, save_results
from routine_queue import find_next_pending_job

_HTTP_LOCAL = threading.local()

def arg_parser():
    p = object()
    p.add_argument('--import-limit', type=int, default=0,
                   help='Import only first N unique valid URLs; 0 = entire file')
    p.add_argument('--retry-network', action='store_true',
                   help='Retry due DNS/timeout jobs from earlier runs')
    p.add_argument('--network-retry-minutes', type=int, default=30)
    return p

def validate_options(args):
    if getattr(args, 'dns_not_found_retry_minutes', 1440) < 0:
        raise ValueError('dns-not-found-retry-minutes must be >= 0')
    if args.max_network_attempts < 1:
        raise ValueError('max-network-attempts must be >= 1')
    if args.http_max_bytes < 1024:
        raise ValueError('http-max-bytes must be >= 1024')

def run(args):
    validate_options(args)
    interactive = getattr(args, 'telegram_checks', False)
    if interactive and (args.limit or args.http_only):
        raise ValueError('--telegram-checks requires --limit 0 and browser support')
    engine = load_engine(args.engine_file, args)
    try:
        processed, waiting_browser, last_id, logged = 0, 0, 0, 0
        futures = {}
        limit = args.limit or float('inf')
        def submit_one(pool):
            nonlocal last_id
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                priority = None
                if interactive:
                    priority = conn.execute(
                        'SELECT j.id,j.url FROM telegram_checks t CROSS JOIN jobs j ON j.id=t.job_id '
                        "WHERE t.result IS NULL AND j.status='pending' ORDER BY t.id LIMIT 1"
                    ).fetchone()
                # Leave capacity for interactive work when Selenium is backlogged.
                if interactive and priority is None and waiting_browser + len(futures) >= args.browser_queue_size:
                    return False
                row = priority or find_next_pending_job(conn, last_id, wrap=interactive)
                if row is None:
                    return False
                id_, url = row
                if priority is None:
                    last_id = id_
                conn.execute("UPDATE jobs SET status='http_inflight', attempts=attempts+1, "
                             "updated_at=? WHERE id=?", (utcnow(), id_))
            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)
            return True
        if processed - logged >= 25:
            deferred_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='retry_http'").fetchone()[0]
            browser_queued_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='browser_queued'").fetchone()[0]
            log(f'HTTP attempted={processed}, browser_queued={browser_queued_count}, '
                f'http_retry_pending={deferred_count}, browser_inflight={waiting_browser}, '
                f'elapsed={time.monotonic()-started:.0f}s')
            logged = processed
    finally:
        conn.close()
'''


class ASTUpgradeTests(unittest.TestCase):
    def test_upgrade_and_idempotence(self):
        status, upgraded = upgrader.upgrade_text(SOURCE)
        self.assertEqual(status, 'ready')
        self.assertNotEqual(SOURCE, upgraded)
        self.assertEqual(upgrader.upgrade_text(upgraded), ('already-upgraded', upgraded))
        ast.parse(upgraded)
        self.assertIn("from network_retry_queue import RetryPacer, choose_next_job", upgraded)
        self.assertIn('retry_pacer.record(kind, now_mono)', upgraded)
        self.assertIn("if kind == 'routine':", upgraded)
        self.assertIn('--auto-network-retry', upgraded)
        self.assertIn('auto_retry_dispatched=', upgraded)

    def test_extra_blank_lines_do_not_break_import(self):
        variant = SOURCE.replace(
            'from routine_queue import find_next_pending_job\n\n_HTTP_LOCAL',
            'from routine_queue import find_next_pending_job  \n\n\n_HTTP_LOCAL')
        status, changed = upgrader.upgrade_text(variant)
        self.assertEqual(status, 'ready')
        self.assertIn('from network_retry_queue import RetryPacer', changed)

    def test_whitespace_differences_do_not_break_row_selection(self):
        variant = SOURCE.replace(
            'row = priority or find_next_pending_job(conn, last_id, wrap=interactive)',
            'row = (priority or\n                       find_next_pending_job(conn, last_id, wrap=interactive))')
        status, changed = upgrader.upgrade_text(variant)
        self.assertEqual(status, 'ready')
        ast.parse(changed)

    def test_unknown_queue_pattern_refused(self):
        variant = SOURCE.replace('row = priority or find_next_pending_job',
                                 'row = priority or next_pending')
        with self.assertRaisesRegex(ValueError, 'queue row selection'):
            upgrader.upgrade_text(variant)

    def test_partial_install_refused(self):
        variant = SOURCE.replace('from routine_queue import find_next_pending_job\n',
                                 'from routine_queue import find_next_pending_job\n'
                                 'from network_retry_queue import RetryPacer, choose_next_job\n')
        with self.assertRaisesRegex(ValueError, 'partial'):
            upgrader.upgrade_text(variant)

    def test_duplicate_symbols_refused(self):
        variant = SOURCE + SOURCE[SOURCE.index('def validate_options'):SOURCE.index('def run')]
        with self.assertRaisesRegex(ValueError, 'validate_options'):
            upgrader.upgrade_text(variant)

    def test_unrelated_comment_preserved(self):
        variant = SOURCE.replace('def run(args):', '# USER INTERNAL COMMENT\ndef run(args):')
        status, changed = upgrader.upgrade_text(variant)
        self.assertEqual(status, 'ready')
        self.assertIn('# USER INTERNAL COMMENT\n', changed)


if __name__ == '__main__':
    unittest.main()
