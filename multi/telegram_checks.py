"""Shared URL validation and durable Telegram check requests."""
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

CHECK_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_checks (
    id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id), chat_id TEXT NOT NULL,
    result TEXT, risk TEXT, reason TEXT, checked_at TEXT, delivered INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS telegram_waiting ON telegram_checks(job_id) WHERE result IS NULL;
CREATE INDEX IF NOT EXISTS telegram_outbox ON telegram_checks(id) WHERE delivered=0 AND result IS NOT NULL;
"""

def normalize_input_url(value):
    """Return a permitted .id URL, or None for ANY malformed input.

    urllib.parse.urlparse/urlsplit can raise ValueError for malformed IPv6
    brackets, invalid Unicode in netloc, or a non-numeric/out-of-range port.
    A single bad entry in urls.json must not kill a 100k-URL crawler run.
    """
    try:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if (not value or value.startswith('#') or len(value) > 8192 or
                any(ord(c) < 32 or ord(c) == 127 for c in value)):
            return None
        if value.startswith('//'):
            value = 'https:' + value
        if '://' not in value:
            value = 'https://' + value.lstrip('/')
        parsed = urlparse(value)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        hostname = parsed.hostname.lower().rstrip('.')
        # Crawling .id names only; redirects may go to non-.id public websites.
        if not hostname.endswith('.id'):
            return None
        if parsed.port not in (None, 80, 443):
            return None
        return value.split('#', 1)[0]
    except (ValueError, UnicodeError):
        # In particular: ValueError('Invalid IPv6 URL') from urlparse().
        return None



def save_results(conn, job_id, status, reason):
    if status in ('pending', 'http_inflight', 'browser_queued'):
        return
    if status == 'pending_alert' and not (reason or '').endswith('check_bima_before_retry'):
        return
    conn.execute("""
        UPDATE telegram_checks SET (result,risk,reason,checked_at) =
          (SELECT status,risk,reason,updated_at FROM jobs WHERE id=?)
        WHERE job_id=? AND result IS NULL
    """, (job_id, job_id))


def request_check(path, url, chat_id, update_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw', uri=True)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT id FROM telegram_checks WHERE update_id=?', (update_id,)).fetchone()
        if previous:
            return previous[0]
        conn.execute("INSERT OR IGNORE INTO jobs(url,status) VALUES (?,'pending')", (url,))
        job_id, status = conn.execute('SELECT id,status FROM jobs WHERE url=?', (url,)).fetchone()
        request_id = conn.execute(
            'INSERT INTO telegram_checks(update_id,job_id,chat_id) VALUES (?,?,?)',
            (update_id, job_id, chat_id)).lastrowid
        if status in ('alerted', 'pending_alert'):
            # Preserve duplicate-report protection, including ambiguous sends.
            conn.execute("""UPDATE telegram_checks SET (result,risk,reason,checked_at) =
                (SELECT status,risk,reason,updated_at FROM jobs WHERE id=?) WHERE id=?""",
                (job_id, request_id))
        elif status not in ('pending','http_inflight','browser_queued'):
            conn.execute("UPDATE jobs SET status='pending', attempts=0, next_retry_at=NULL WHERE id=?", (job_id,))
        return request_id
