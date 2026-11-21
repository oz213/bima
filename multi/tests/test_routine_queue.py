"""Pure sqlite tests of cursor wrap; no crawler/IndoBERT runtime import."""
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routine_queue import find_next_pending_job


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT, status TEXT)')
        self.conn.executemany('INSERT INTO jobs VALUES (?,?,?)', [
            (1, 'https://a.id/', 'complete'),
            (2, 'https://b.id/', 'pending'),
            (3, 'https://c.id/', 'complete'),
            (4, 'https://d.id/', 'pending'),
        ])

    def test_forward_pass(self):
        self.assertEqual(find_next_pending_job(self.conn, 1, wrap=True)[0], 2)
        self.assertEqual(find_next_pending_job(self.conn, 2, wrap=True)[0], 4)

    def test_interactive_cursor_wrap(self):
        self.assertEqual(find_next_pending_job(self.conn, 4, wrap=True)[0], 2)

    def test_noninteractive_unchanged(self):
        self.assertIsNone(find_next_pending_job(self.conn, 4, wrap=False))

    def test_after_requeue_old_id(self):
        self.conn.execute("UPDATE jobs SET status='complete' WHERE id IN (2,4)")
        self.assertIsNone(find_next_pending_job(self.conn, 4, wrap=True))
        self.conn.execute("UPDATE jobs SET status='pending' WHERE id=1")
        self.assertEqual(find_next_pending_job(self.conn, 4, wrap=True)[0], 1)
