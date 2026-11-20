#!/usr/bin/env python3
"""PANDI crawler progress bot: read-only SQLite, on-demand Telegram commands.

Requires only Python 3.9+ stdlib. Never modifies crawler jobs or sends BIMA alerts.
"""
import json
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("pandi.telegram.monitor")
DB_DEFAULT = "/home/fauzi/crawler/multi/state/crawler_production.sqlite3"
OFFSET_DEFAULT = "/home/fauzi/crawler/multi/state/.telegram_monitor_offset"


def fmt_int(n):
    return f"{int(n):,}".replace(",", ".")


def fmt_pct(num, den):
    return f"{(100.0 * num / den) if den else 0:.4f}%".replace(".", ",")


def open_db(path):
    if not Path(path).is_file():
        raise FileNotFoundError(f"Database tidak ditemukan: {path}")
    uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA query_only=ON")
    return connection


def status_counts(connection):
    rows = connection.execute(
        "SELECT status, COUNT(*) AS count, "
        "COALESCE(SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END), 0) AS attempted "
        "FROM jobs GROUP BY status"
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    attempted = sum(int(row["attempted"]) for row in rows)
    total = sum(counts.values())
    return counts, total, attempted


def status_report(connection):
    counts, total, attempted = status_counts(connection)
    completed = counts.get("complete", 0) + counts.get("alerted", 0)
    other = total - sum(counts.get(key, 0) for key in (
        "pending", "complete", "alerted", "browser_queued", "retry_http",
        "inconclusive", "pending_alert", "dry_run_alert"
    ))
    msg = (
        "PANDI CRAWLER | PROGRESS\n"
        f"Total URL       : {fmt_int(total)}\n"
        f"Sudah dicoba    : {fmt_int(attempted)} ({fmt_pct(attempted, total)})\n"
        f"Selesai         : {fmt_int(completed)} ({fmt_pct(completed, total)})\n"
        f"Belum dicoba    : {fmt_int(counts.get('pending', 0))}\n"
        f"Antrean browser : {fmt_int(counts.get('browser_queued', 0))}\n"
        f"Retry HTTP/DNS  : {fmt_int(counts.get('retry_http', 0))}\n"
        f"Inconclusive    : {fmt_int(counts.get('inconclusive', 0))}\n"
        f"Alert BIMA OK   : {fmt_int(counts.get('alerted', 0))}\n"
        f"Alert pending   : {fmt_int(counts.get('pending_alert', 0))}"
    )
    if other:
        msg += f"\nStatus lain     : {fmt_int(other)}"
    msg += "\n\nCatatan: 'sudah dicoba' bukan berarti selesai dianalisis."
    return msg


def inconclusive_report(connection):
    rows = connection.execute(
        "SELECT COALESCE(reason, 'unknown') AS reason, COUNT(*) AS n "
        "FROM jobs WHERE status = 'inconclusive' "
        "GROUP BY reason ORDER BY n DESC LIMIT 10"
    ).fetchall()
    total = int(connection.execute("SELECT COUNT(*) FROM jobs WHERE status='inconclusive'").fetchone()[0])
    msg = f"INCONCLUSIVE | {fmt_int(total)} URL (hingga 10 alasan)\n"
    if not rows:
        return msg + "Tidak ada URL berstatus inconclusive."
    for r in rows:
        msg += f"- {r['reason']}: {fmt_int(r['n'])}\n"
    examples = connection.execute(
        "SELECT url, reason FROM jobs WHERE status='inconclusive' LIMIT 5"
    ).fetchall()
    if examples:
        msg += "\nContoh URL:\n"
        for r in examples:
            msg += f"- {str(r['url'])[:110]} ({r['reason'] or 'unknown'})\n"
    return msg.strip()


def bima_report(connection):
    counts, total, attempted = status_counts(connection)
    return (
        "BIMA | STATUS ALERT\n"
        f"Terkirim (alerted) : {fmt_int(counts.get('alerted', 0))}\n"
        f"Menunggu retry    : {fmt_int(counts.get('pending_alert', 0))}\n"
        f"Dry-run saja      : {fmt_int(counts.get('dry_run_alert', 0))}\n"
        "\nStatus 'alerted' adalah sukses menurut crawler; pastikan penerimaan akhir di BIMA."
    )


def dns_report(connection):
    rows = connection.execute(
        "SELECT COALESCE(dns_result,'none') AS dns_result, COUNT(*) AS n "
        "FROM jobs WHERE status='retry_http' GROUP BY dns_result ORDER BY n DESC LIMIT 12"
    ).fetchall()
    if not rows:
        return "DNS/HTTP RETRY\nTidak ada URL berstatus retry_http."
    return "DNS/HTTP RETRY\n" + "\n".join(
        f"- {r['dns_result']}: {fmt_int(r['n'])}" for r in rows
    )


def safe_domain(url):
    """Display hostname and short path, never URLs' query credentials or fragments."""
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(str(url))
        hostname = parsed.hostname or '(unknown)'
        path = parsed.path[:44]
        return hostname + (path if path and path != '/' else '')
    except ValueError:
        return '(invalid URL)'


def status_label(status, risk=None, reason=None):
    labels = {
        'alerted': 'JUDOL -> BIMA terkirim',
        'pending_alert': 'JUDOL -> BIMA tertunda',
        'dry_run_alert': 'Judol (dry-run)',
        'complete': 'Selesai diperiksa',
        'inconclusive': 'Belum dapat disimpulkan',
        'retry_http': 'Menunggu retry HTTP/DNS',
        'http_inflight': 'Sedang diperiksa via HTTP',
        'browser_queued': 'Antrean/pemeriksaan browser',
        'failed': 'Gagal diproses',
    }
    label = labels.get(str(status), str(status))
    if status == 'complete' and risk:
        label += f' [{str(risk)[:20]}]'
    if status in ('inconclusive', 'retry_http', 'failed') and reason:
        label += f' ({str(reason)[:46]})'
    return label


def latest_rows(connection, limit=5):
    """Last saved results; NOT live HTTP activity. No index changes in production."""
    limit = max(1, min(int(limit), 10))
    return connection.execute(
        "SELECT url, status, reason, risk, updated_at FROM jobs "
        "WHERE attempts > 0 AND status NOT IN ('pending','http_inflight','browser_queued') "
        "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def active_report(connection):
    msg = ['PANDI CRAWLER | AKTIVITAS TERDAFTAR']
    groups = (
        ('http_inflight', 'HTTP inflight (belum tentu sudah selesai)'),
        ('browser_queued', 'Antrean / proses Selenium'),
    )
    for status, label in groups:
        total = int(connection.execute('SELECT COUNT(*) FROM jobs WHERE status=?', (status,)).fetchone()[0])
        msg.append(f'\n{label}: {fmt_int(total)} URL')
        rows = connection.execute(
            'SELECT url, updated_at FROM jobs WHERE status=? ORDER BY id DESC LIMIT 4',
            (status,)
        ).fetchall()
        for row in rows:
            msg.append(f'- {safe_domain(row["url"])} ({row["updated_at"] or "-"} UTC)')
    msg.append('\nCatatan: status dari SQLite; jika proses mati, data inflight bisa tertinggal. Pantau watchdog untuk verifikasi PID.')
    return '\n'.join(msg)


def recent_report(connection, limit=5):
    rows = latest_rows(connection, limit)
    msg = [f'PANDI CRAWLER | {len(rows)} HASIL TERBARU']
    if not rows:
        return '\n'.join(msg + ['Belum ada hasil yang tersimpan.'])
    for row in rows:
        msg.append(f'- {safe_domain(row["url"])}: {status_label(row["status"],row["risk"],row["reason"])}')
    msg.append('Hasil berasal dari SQLite, bukan notifikasi per URL. Waktu pembaruan berbeda dari waktu mulai crawl.')
    return '\n'.join(msg)


HELP = (
    "PANDI Crawler Monitor (hanya baca)\n"
    "/status - total URL, progres dan antrean\n"
    "/inconclusive - alasan dan contoh gagal analisis\n"
    "/dns - ringkasan retry DNS/HTTP\n"
    "/bima - alert terkirim dan pending\n"
    "/now - domain sedang di-HTTP / antre Selenium\n"
    "/recent [1-10] - hasil crawling terbaru\n"
    "/id - tampilkan chat ID untuk otorisasi\n"
    "/help - daftar perintah"
)


def handle_command(text, chat_id, allowed, db_path):
    parts = text.strip().split()
    command = (parts[0] if parts else '').split('@')[0].lower()
    if command in ('/id', '/start'):
        return f"Chat ID Anda: {chat_id}\nBerikan ID ini kepada admin bot untuk diizinkan."
    if chat_id not in allowed:
        return "Chat ini belum diizinkan. Kirim /id lalu daftarkan ID ke administrator."
    if command in ('/help', ''):
        return HELP
    if command not in ('/status','/inconclusive','/dns','/bima','/now','/recent'):
        return HELP
    limit = 5
    if command == '/recent' and len(parts) > 1:
        try:
            limit = int(parts[1])
        except ValueError:
            return 'Gunakan /recent atau /recent 1-10.'
        if not 1 <= limit <= 10:
            return 'Gunakan /recent atau /recent 1-10.'
    with closing(open_db(db_path)) as connection:
        if command == '/status':
            return status_report(connection)
        if command == '/inconclusive':
            return inconclusive_report(connection)
        if command == '/dns':
            return dns_report(connection)
        if command == '/bima':
            return bima_report(connection)
        if command == '/now':
            return active_report(connection)
        return recent_report(connection, limit)


def telegram_api(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=40) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} gagal: {result.get('description', 'unknown error')}")
    return result.get("result")


def get_updates(token, offset, poll_seconds=5, http_timeout=15):
    """Poll Telegram via HTTPS GET, which also works behind GET-only proxies.

    Never log the URL: Telegram tokens are embedded in the URL path.
    """
    params = urlencode({
        "offset": int(offset),
        "timeout": int(poll_seconds),
        "limit": 20,
        "allowed_updates": json.dumps(["message"]),
    })
    req = Request(f"https://api.telegram.org/bot{token}/getUpdates?{params}", method="GET")
    with urlopen(req, timeout=max(int(http_timeout), int(poll_seconds) + 10)) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError("Telegram getUpdates returned ok=false")
    return result.get("result") or []


def send_message(token, chat_id, text):
    """GET sendMessage, matching Telegram connectivity proven on the user's VM.

    Do not print exception URLs: Bot token is part of the request path.
    """
    params = urlencode({
        'chat_id': chat_id,
        'text': text[:3900],
        'disable_web_page_preview': 'true',
    })
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage?{params}', method='GET')
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram sendMessage returned ok=false')
    return result.get('result')


def safe_network_error(error):
    """No URL, path or Telegram token must appear in systemd logs."""
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        return f"URLError/{type(reason).__name__ if reason is not None else 'unknown'}"
    return type(error).__name__


def load_offset(path):
    try:
        return int(Path(path).read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(path, n):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(n))
    tmp.replace(path)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOGGER.error("TELEGRAM_BOT_TOKEN harus diisi melalui file environment yang aman")
        return 2
    allowed = {x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
    db_path = os.environ.get("CRAWLER_DB_PATH", DB_DEFAULT)
    offset_path = os.environ.get("TELEGRAM_OFFSET_PATH", OFFSET_DEFAULT)
    LOGGER.info("PANDI monitoring bot aktif (hanya baca). Allowed chats: %s", len(allowed))
    if not allowed:
        LOGGER.warning("Belum ada chat yang diizinkan; hanya /id dan /start yang dijawab")
    offset = load_offset(offset_path)
    try:
        poll_seconds = int(os.environ.get("TELEGRAM_POLL_SECONDS", "5"))
    except ValueError:
        LOGGER.error("TELEGRAM_POLL_SECONDS harus bilangan bulat 0-20")
        return 2
    if not 0 <= poll_seconds <= 20:
        LOGGER.error("TELEGRAM_POLL_SECONDS harus antara 0-20")
        return 2
    LOGGER.info("Telegram GET polling dimulai: wait=%ss; offset=%s", poll_seconds, offset)
    failures = 0
    fallback_short_poll = poll_seconds == 0
    while True:
        try:
            wait_seconds = 0 if fallback_short_poll else poll_seconds
            updates = get_updates(token, offset, poll_seconds=wait_seconds,
                                  http_timeout=max(15, wait_seconds + 10))
            failures = 0
            if updates:
                LOGGER.info("Telegram updates diterima: %d", len(updates))
            for update in updates:
                update_id = int(update["update_id"])
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                command = message.get("text", "")
                if chat_id and isinstance(command, str) and command.startswith("/"):
                    try:
                        reply = handle_command(command, chat_id, allowed, db_path)
                    except (sqlite3.Error, OSError) as error:
                        LOGGER.error("Database query failed: %s", type(error).__name__)
                        reply = "Database crawler belum bisa dibaca. Periksa path, izin, dan SQLite."
                    send_message(token, chat_id, reply)
                    LOGGER.info("Telegram command handled: %s", command.split()[0].split('@')[0][:30])
                offset = max(offset, update_id + 1)
                save_offset(offset_path, offset)
            if fallback_short_poll and not updates:
                time.sleep(3)  # avoid aggressive short polling
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
            # Never print exception text: urllib exceptions can embed the token URL.
            failures += 1
            LOGGER.warning("Telegram poll/send gagal (%s), consecutive=%d", safe_network_error(error), failures)
            if failures >= 2 and not fallback_short_poll:
                fallback_short_poll = True
                LOGGER.warning("Switching to GET short polling (timeout=0) due to repeated errors")
            time.sleep(min(5 * failures, 15))


if __name__ == "__main__":
    sys.exit(main())
