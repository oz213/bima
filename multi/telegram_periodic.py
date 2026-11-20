#!/usr/bin/env python3
"""Send one read-only crawler summary to Telegram; designed for a systemd timer."""
import html
import json
import os
import re
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

DB = Path(os.environ.get('CRAWLER_DB_PATH', '/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3'))
STATE = DB.parent / '.telegram_periodic_state.json'


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def rows_for(conn, status, limit=2):
    cols = {str(row[1]) for row in conn.execute('PRAGMA table_info(jobs)')}
    ucol = next((x for x in ('url', 'domain_name', 'domain', 'hostname') if x in cols), None)
    if not ucol:
        return []
    order = next((x for x in ('updated_at', 'last_attempt_at', 'last_checked_at', 'last_crawled_at', 'completed_at', 'id') if x in cols), None)
    sql = f'SELECT {quote(ucol)} FROM jobs WHERE status = ?'
    if order:
        sql += f' ORDER BY {quote(order)} DESC'
    sql += ' LIMIT ?'
    return [str(r[0]) for r in conn.execute(sql, (status, limit)) if r[0]]


def display_url(raw):
    raw = str(raw).strip()
    parsed = urlparse(raw)
    if len(raw) <= 300 and parsed.scheme in ('http', 'https') and parsed.netloc:
        label = (parsed.netloc + parsed.path)[:65]
        return f'<a href="{html.escape(raw, quote=True)}">{html.escape(label)}</a>'
    return html.escape(raw[:65])


def render_report(conn, previous):
    counts = dict(conn.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status').fetchall())
    total = sum(counts.values())
    processed = total - counts.get('pending', 0)
    delta = max(0, processed - previous) if previous is not None else 0
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    host = html.escape(socket.getfqdn())
    lines = [
        '📊 <b>PANDI CRAWLER | MONITORING BERKALA</b>',
        f'Server: {host}',
        f'Waktu: {timestamp}',
        f'URL diproses/non-pending: {processed:,} / {total:,}',
        f'Perubahan sejak laporan sebelumnya: +{delta:,}',
        f'Selesai: {counts.get("complete", 0):,} | Alert BIMA: {counts.get("alerted", 0):,}',
        f'Retry: {counts.get("retry_http", 0):,} | Inconclusive: {counts.get("inconclusive", 0):,}',
        f'HTTP aktif: {counts.get("http_inflight", 0):,} | Browser queued: {counts.get("browser_queued", 0):,}',
    ]
    active_http = rows_for(conn, 'http_inflight')
    active_browser = rows_for(conn, 'browser_queued')
    if active_http:
        lines.append('Sedang diperiksa (HTTP): ' + ', '.join(map(display_url, active_http)))
    if active_browser:
        lines.append('Antrean/proses browser: ' + ', '.join(map(display_url, active_browser)))
    cols = {str(r[1]) for r in conn.execute('PRAGMA table_info(jobs)')}
    url_col = next((c for c in ('url', 'domain_name', 'domain', 'hostname') if c in cols), None)
    time_col = next((c for c in ('updated_at', 'last_attempt_at', 'last_checked_at', 'last_crawled_at', 'completed_at') if c in cols), None)
    if url_col:
        fields = [quote(url_col), 'status'] + (['reason'] if 'reason' in cols else [])
        query = 'SELECT ' + ', '.join(fields) + ' FROM jobs WHERE status NOT IN (\'pending\', \'http_inflight\', \'browser_queued\')'
        query += ' ORDER BY ' + (quote(time_col) + ' DESC' if time_col else 'rowid DESC') + ' LIMIT 5'
        recent = conn.execute(query).fetchall()
        if recent:
            lines.append('Hasil tersimpan (' + ('terbaru' if time_col else 'contoh') + '):')
            for row in recent:
                reason = f' ({html.escape(str(row[2])[:60])})' if len(row) > 2 and row[2] else ''
                lines.append(f'- {display_url(row[0])}: {html.escape(str(row[1]))}{reason}')
    return '\n'.join(lines), processed


def main():
    if not DB.is_file():
        raise SystemExit('Crawler database not found')
    preview = '--preview' in sys.argv
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    raw_chat_ids = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    chat_ids = list(dict.fromkeys(re.findall(r'-?\d+', raw_chat_ids)))
    if not preview and (not token or not chat_ids):
        raise SystemExit('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_CHAT_IDS')
    previous = None
    if STATE.is_file():
        try:
            previous = int(json.loads(STATE.read_text()).get('processed'))
        except (ValueError, TypeError, OSError):
            pass
    with sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) as conn:
        conn.execute('PRAGMA query_only = ON')
        report, processed = render_report(conn, previous)
    if preview:
        print(report)
        return
    for chat_id in chat_ids:
        data = urlencode({
            'chat_id': chat_id,
            'text': report,
            'parse_mode': 'HTML',
            'disable_web_page_preview': 'true',
        }).encode()
        request = Request('https://api.telegram.org/bot' + token + '/sendMessage', data=data, method='POST')
        try:
            with urlopen(request, timeout=15) as response:
                ok = json.load(response).get('ok')
            if not ok:
                raise RuntimeError('Telegram returned ok=false')
        except Exception as exc:
            # Never print the request URL: the token is embedded in it.
            print(f'Telegram send failed for chat {chat_id}: {type(exc).__name__}', file=sys.stderr)
            raise SystemExit(1)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'processed': processed, 'at': datetime.now(timezone.utc).isoformat()}))
    os.replace(tmp, STATE)
    print(f'Telegram periodic summary sent to {len(chat_ids)} chat(s); processed={processed}; total={report.count(chr(10))+1} lines')


if __name__ == '__main__':
    main()
