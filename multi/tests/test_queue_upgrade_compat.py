"""Offline tests for the Mac-side adaptive queue compatibility upgrade."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools' / 'upgrade_bima_queue.py'
module_spec = importlib.util.spec_from_file_location('bima_queue_upgrader', SCRIPT)
upgrader = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(upgrader)

OLD_SOURCE = '''from __future__ import annotations
import requests
from telegram_checks import CHECK_SCHEMA, normalize_input_url, save_results

def run(args):
    last_id = 0
    def submit_one(pool):
        nonlocal last_id
        row = priority or conn.execute(
            "SELECT id,url FROM jobs WHERE status='pending' AND id>? ORDER BY id LIMIT 1",
            (last_id,)).fetchone()
        return row
'''


class PlanTests(unittest.TestCase):
    def test_upgrade_v2_query_and_import(self):
        output, state = upgrader.plan_change(OLD_SOURCE)
        self.assertEqual(state, 'old queue query found')
        self.assertIn('find_next_pending_job(conn, last_id, wrap=interactive)', output)
        self.assertEqual(output.count('from routine_queue import find_next_pending_job'), 1)

    def test_idempotent(self):
        upgraded, _ = upgrader.plan_change(OLD_SOURCE)
        again, state = upgrader.plan_change(upgraded)
        self.assertEqual(state, 'already upgraded')
        self.assertEqual(upgraded, again)

    def test_one_line_variant(self):
        text = OLD_SOURCE.replace('''row = priority or conn.execute(
            "SELECT id,url FROM jobs WHERE status='pending' AND id>? ORDER BY id LIMIT 1",
            (last_id,)).fetchone()''', '''row = priority or conn.execute("SELECT id,url FROM jobs WHERE status='pending' AND id>? ORDER BY id LIMIT 1", (last_id,)).fetchone()''')
        result, _ = upgrader.plan_change(text)
        self.assertIn('find_next_pending_job(conn, last_id, wrap=interactive)', result)

    def test_unknown_query_refused(self):
        text = OLD_SOURCE.replace('id>?', 'id>=?')
        with self.assertRaises(ValueError):
            upgrader.plan_change(text)

    def test_cli_check_apply_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            multi = Path(tmp) / 'multi'
            multi.mkdir()
            (multi / 'routine_queue.py').write_text('def find_next_pending_job(*args, **kw): pass\n')
            path = multi / 'crawler_multi.py'
            path.write_bytes(OLD_SOURCE.replace('\n', '\r\n').encode())
            check = subprocess.run([sys.executable, str(SCRIPT), '--path', str(path), '--check'],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(path.read_bytes(), OLD_SOURCE.replace('\n', '\r\n').encode())
            apply = subprocess.run([sys.executable, str(SCRIPT), '--path', str(path), '--apply'],
                                   capture_output=True, text=True)
            self.assertEqual(apply.returncode, 0, apply.stderr)
            self.assertIn(b'find_next_pending_job', path.read_bytes())
            self.assertNotIn(b'\r\r\n', path.read_bytes())
            self.assertIn(b'\r\n', path.read_bytes())


if __name__ == '__main__':
    unittest.main()
