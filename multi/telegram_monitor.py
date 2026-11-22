#!/usr/bin/env python3
"""Telegram monitoring and on-demand checks."""
from telegram_checks import normalize_input_url, request_check
import json
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
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
        'awaiting_review': 'Kandidat judol - menunggu verifikasi Telegram',
        'review_approved': 'Judol terkonfirmasi - menunggu kirim BIMA',
        'review_rejected': 'Bukan judol menurut reviewer',
        'review_needs_evidence': 'Bukti belum cukup - perlu investigasi',
        'prealert_send_unknown': 'Alert BIMA tidak pasti - jangan retry otomatis',
        'prealert_error': 'Antrean pre-alert perlu pemeriksaan manual',
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
    "PANDI Crawler Monitor\n"
    "/cek <URL .id> - periksa halaman dan kirim hasil ke chat ini\n"
    "/status - total URL, progres dan antrean\n"
    "/inconclusive - alasan dan contoh gagal analisis\n"
    "/dns - ringkasan retry DNS/HTTP\n"
    "/bima - alert terkirim dan pending\n"
    "/now - domain sedang di-HTTP / antre Selenium\n"
    "/recent [1-10] - hasil crawling terbaru\n"
    "/id - tampilkan chat ID untuk otorisasi\n"
    "/pola - kasus dengan bukti berulang lintas situs\n"
    "/review - verifikasi situs (reviewer pribadi)\n"
    "/antrian - status kandidat Telegram pre-alert\n"
    "/tinjauulang #kasus - ulang verifikasi setelah investigasi\n"
    "/help - daftar perintah"
)


def handle_command(text, chat_id, allowed, db_path, update_id=None, sender_id=None,
                   *, analyst_service=None, investigation_service=None, chat_type=None):
    parts = text.strip().split()
    command = (parts[0] if parts else '').split('@')[0].lower()
    if command in ('/id', '/start'):
        return f"Chat ID Anda: {chat_id}\nBerikan ID ini kepada admin bot untuk diizinkan."
    if chat_id not in allowed:
        return "Chat ini belum diizinkan. Kirim /id lalu daftarkan ID ke administrator."
    if command in ('/analisis', '/tanya', '/bandingkan', '/lupakan'):
        if os.environ.get('BIMA_ANALYST_ENABLED') != '1' or analyst_service is None:
            return 'Fitur analisis belum diaktifkan oleh administrator.'
        return analyst_service.handle(text, chat_id, sender_id, allowed,
                                      update_id=update_id, chat_type=chat_type)
    if command == '/investigasi':
        if os.environ.get('BIMA_INVESTIGATION_ENABLED') != '1' or investigation_service is None:
            return 'Fitur investigasi belum diaktifkan oleh administrator.'
        return investigation_service.handle(text, chat_id, sender_id, allowed,
                                            chat_type=chat_type)
    if command == '/antrian':
        if os.environ.get('BIMA_PREALERT_ENABLED') != '1':
            return 'BIMA selective pre-alert belum diaktifkan.'
        from selective_prealert import pending_summary
        return pending_summary(db_path, chat_id, sender_id, allowed)
    if command == '/tinjauulang':
        if os.environ.get('BIMA_PREALERT_ENABLED') != '1':
            return 'BIMA selective pre-alert belum diaktifkan.'
        from selective_prealert import reopen_review
        case_id = parts[1] if len(parts) == 2 else None
        return reopen_review(db_path, case_id, chat_id, sender_id, allowed, chat_type)
    if command in ('/review', '/bukti', '/pola'):
        from telegram_review import handle
        return handle(text, chat_id, sender_id, allowed, db_path)
    if command == '/cek':
        if os.environ.get('TELEGRAM_CHECKS_ENABLED') != '1':
            return 'Fitur /cek belum diaktifkan.'
        if chat_id.startswith('-') and sender_id not in {
                v.strip() for v in os.environ.get('TELEGRAM_CHECK_USER_IDS', '').split(',') if v.strip()}:
            return 'Pengirim belum diizinkan memakai /cek di grup ini.'
        url = normalize_input_url(parts[1]) if len(parts) == 2 else None
        if not url or '@' in urlsplit(url).netloc or chr(92) in url:
            return 'Gunakan /cek https://domain.id/path dengan URL .id yang valid.'
        check_id = request_check(db_path, url, chat_id, update_id)
        return f'Permintaan #{check_id} diterima. Hasil akan dikirim ke chat ini.'
    if command in ('/help', ''):
        help_text = HELP
        if os.environ.get('BIMA_ANALYST_ENABLED') == '1' and analyst_service is not None:
            from telegram_analyst import HELP as ANALYST_HELP
            help_text += ANALYST_HELP
        if investigation_service is not None:
            help_text += '\n/investigasi <domain.id|#job> - HTTP on-demand reviewer pribadi'
        return help_text
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
        "allowed_updates": json.dumps(["message", "callback_query"]),
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
    data = text if isinstance(text, dict) else {"text": text}
    fields = {"chat_id": chat_id, "text": data["text"][:3900], "disable_web_page_preview": "true"}
    if "reply_markup" in data:
        fields["reply_markup"] = json.dumps(data["reply_markup"])
    params = urlencode(fields)
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage?{params}', method='GET')
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram sendMessage returned ok=false')
    return result.get('result')


def handle_review_callback(token, query, allowed, db_path):
    from telegram_review import handle
    chat = (query.get("message") or {}).get("chat") or {}
    sender = str((query.get("from") or {}).get("id", ""))
    data = query.get("data") or ""
    # A stale or already acknowledged callback must not block update replay.
    params = urlencode({"callback_query_id": query.get("id", "")})
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/answerCallbackQuery?{params}", timeout=10):
            pass
    except (OSError, TimeoutError):
        pass
    if chat.get("type") == "private" and data.startswith("br:"):
        chat_id = str(chat["id"])
        reply = handle(data, chat_id, sender, allowed, db_path, callback=True)
        send_message(token, chat_id, reply)
    elif (chat.get("type") == "private" and data.startswith("pa:") and
          os.environ.get('BIMA_PREALERT_ENABLED') == '1'):
        from selective_prealert import decide
        chat_id = str(chat["id"])
        reply = decide(db_path, data, chat_id, sender, allowed, chat.get('type'))
        send_message(token, chat_id, reply)


def deliver_checks(token, db_path, allowed):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=rw', uri=True)) as conn:
        rows = conn.execute("""SELECT t.id,t.chat_id,j.url,t.result,t.risk,t.reason,t.checked_at
            FROM telegram_checks t JOIN jobs j ON j.id=t.job_id
            WHERE t.delivered=0 AND t.result IS NOT NULL ORDER BY t.id LIMIT 20""").fetchall()
        for number, chat, url, status, risk, reason, checked in rows:
            if chat in allowed:
                label = status_label(status, risk, reason)
                if status == 'retry_http':
                    label = 'Belum dapat disimpulkan. ' + label
                message = f'#{number} {safe_domain(url)}\n{label}\n{reason or "-"}\n{checked or "-"} UTC'
                try:
                    send_message(token, chat, message)
                except HTTPError as error:
                    if error.code not in (400, 403):
                        raise
                    LOGGER.warning('Telegram menolak hasil #%d (HTTP %d)', number, error.code)
            with conn:
                conn.execute('UPDATE telegram_checks SET delivered=1 WHERE id=?', (number,))

        active = conn.execute("""SELECT t.id,t.chat_id,j.url
            FROM telegram_checks t CROSS JOIN jobs j ON j.id=t.job_id
            WHERE t.result IS NULL AND t.progress_sent=0
              AND j.status IN ('http_inflight','browser_queued')
            ORDER BY t.id LIMIT 20""").fetchall()
        for number, chat, url in active:
            if chat in allowed:
                try:
                    send_message(token, chat, f'#{number} {safe_domain(url)} — Sedang diperiksa.')
                except HTTPError as error:
                    if error.code not in (400, 403):
                        raise
                    LOGGER.warning('Telegram menolak status #%d (HTTP %d)', number, error.code)
            with conn:
                conn.execute('UPDATE telegram_checks SET progress_sent=1 WHERE id=?', (number,))



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
    from bima_private_config import apply_private_config
    apply_private_config('telegram')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOGGER.error("TELEGRAM_BOT_TOKEN harus diisi melalui file environment yang aman")
        return 2
    allowed = {x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
    db_path = os.environ.get("CRAWLER_DB_PATH", DB_DEFAULT)
    offset_path = os.environ.get("TELEGRAM_OFFSET_PATH", OFFSET_DEFAULT)
    LOGGER.info("PANDI monitoring bot aktif. Allowed chats: %s", len(allowed))
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
    analyst_service = None
    if os.environ.get('BIMA_ANALYST_ENABLED') == '1':
        from telegram_analyst import AnalystService
        analyst_service = AnalystService(db_path, learning_db=os.environ.get('BIMA_LEARNING_DB'))
    investigation_service = None
    if os.environ.get('BIMA_INVESTIGATION_ENABLED') == '1':
        from telegram_investigation import InvestigationService
        investigation_service = InvestigationService(db_path)
    try:
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
                    if update.get("callback_query"):
                        handle_review_callback(token, update["callback_query"], allowed, db_path)
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    chat_id = str(chat.get("id", ""))
                    sender_id = str((message.get("from") or {}).get("id", ""))
                    command = message.get("text", "")
                    reply = None
                    if chat_id and isinstance(command, str) and command.startswith("/"):
                        try:
                            reply = handle_command(command, chat_id, allowed, db_path, update_id,
                                                   sender_id, analyst_service=analyst_service,
                                                   investigation_service=investigation_service,
                                                   chat_type=chat.get('type'))
                        except (sqlite3.Error, OSError) as error:
                            LOGGER.error("Database query failed: %s", type(error).__name__)
                            reply = "Database crawler belum bisa dibaca. Periksa path, izin, dan SQLite."
                        LOGGER.info("Telegram command handled: %s", command.split()[0].split('@')[0][:30])
                    elif analyst_service is not None and chat_id and isinstance(command, str) and command.strip():
                        reply = analyst_service.handle(command, chat_id, sender_id, allowed,
                            update_id=update_id, chat_type=chat.get('type'), plain=True)
                    if reply is not None:
                        send_message(token, chat_id, reply)
                    offset = max(offset, update_id + 1)
                    save_offset(offset_path, offset)
                if os.environ.get('TELEGRAM_CHECKS_ENABLED') == '1':
                    deliver_checks(token, db_path, allowed)
                if analyst_service is not None:
                    analyst_service.deliver(lambda chat_id, text: send_message(token, chat_id, text), allowed)
                if investigation_service is not None:
                    investigation_service.deliver(
                        lambda chat_id, text: send_message(token, chat_id, text), allowed)
                if os.environ.get('BIMA_PREALERT_ENABLED') == '1':
                    from selective_prealert import deliver_one, dispatch_one
                    deliver_one(token, db_path, allowed)
                    outcome = dispatch_one(db_path)
                    if outcome is not None:
                        reviewer = os.environ.get('BIMA_PREALERT_REVIEWER_CHAT_ID', '').strip()
                        if reviewer in allowed:
                            result = ('terkirim ke sistem BIMA' if outcome['ok'] else
                                      'BELUM terkonfirmasi; cek BIMA sebelum retry manual')
                            send_message(token, reviewer,
                                         f"Pre-Alert #{outcome['id']}: {result}.")
                if fallback_short_poll and not updates:
                    time.sleep(3)  # avoid aggressive short polling
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                # Never print exception text: urllib exceptions can embed the token URL.
                failures += 1
                LOGGER.warning("Telegram poll/send gagal (%s), consecutive=%d", safe_network_error(error), failures)
                if failures >= 2 and not fallback_short_poll:
                    fallback_short_poll = True
                    LOGGER.warning("Switching to GET short polling (timeout=0) due to repeated errors")
                time.sleep(min(5 * failures, 15))
    finally:
        if analyst_service is not None:
            analyst_service.close()
        if investigation_service is not None:
            investigation_service.close()


if __name__ == "__main__":
    sys.exit(main())
