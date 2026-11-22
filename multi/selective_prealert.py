"""Selective, durable human review for existing BIMA gambling alert candidates.

Off by default. Only already-positive legacy crawler candidates are eligible.
Nothing in this module recategorizes a page or changes IndoBERT/Classic ML.

Never silently resend an ambiguous BIMA POST. Screenshots are temporarily
stored as owner-only files solely for Telegram review and approved BIMA alert.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sqlite3
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from telegram_review import authorized
from telegram_checks import save_results

LOGGER = logging.getLogger('pandi.prealert')
PNG_HEADER = b'\x89PNG\r\n\x1a\n'
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PENDING_BYTES = 512 * 1024 * 1024
BUCKET_LIMITS = {'strong': 6, 'disagreement': 3, 'sample': 1}
FILE_NAME = re.compile(r'\A[0-9a-f]{32}\.png\Z')
CALLBACK = re.compile(r'\Apa:([1-9][0-9]{0,17}):(yes|no|defer)\Z')

PREALERT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS bima_prealerts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
 url TEXT NOT NULL,
 final_url TEXT,
 day_utc TEXT NOT NULL,
 bucket TEXT NOT NULL CHECK (bucket IN ('strong','disagreement','sample')),
 confidence REAL NOT NULL,
 classifier_json TEXT,
 evidence_sha256 TEXT NOT NULL,
 image_filename TEXT NOT NULL,
 image_bytes INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending_telegram',
 notify_attempt_at TEXT,
 notify_attempts INTEGER NOT NULL DEFAULT 0,
 telegram_chat_id TEXT,
 telegram_message_id INTEGER,
 telegram_file_id TEXT,
 reviewer_id TEXT,
 feedback_label TEXT,
 reviewed_at TEXT,
 bima_attempted_at TEXT,
 bima_result_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bima_prealerts_delivery
 ON bima_prealerts(status,notify_attempt_at,id);
CREATE INDEX IF NOT EXISTS idx_bima_prealerts_day
 ON bima_prealerts(day_utc,bucket);
CREATE TABLE IF NOT EXISTS bima_prealert_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 prealert_id INTEGER NOT NULL REFERENCES bima_prealerts(id),
 event_type TEXT NOT NULL,
 actor TEXT NOT NULL,
 created_at TEXT NOT NULL
);
'''


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def db_connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.execute('PRAGMA busy_timeout=20000')
    conn.execute('PRAGMA foreign_keys=ON')
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bima_prealerts'").fetchone()
    if not exists:
        conn.executescript(PREALERT_SCHEMA)
    return conn


def _normalize_prediction(value):
    v = str(value or '').strip().lower().replace('_', '-')
    if v in ('gambling', 'judol', 'online-gambling'):
        return 'gambling'
    if v in ('non-gambling', 'non gambling', 'nongambling', 'not-gambling'):
        return 'non-gambling'
    return None


def _classifier(raw):
    try:
        data = json.loads(raw or '{}')
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return {}
    # Browser classification wraps the earlier HTTP classification, which
    # may contain the only available model metadata in the HTTP-positive path.
    return data


def _disagreement(meta):
    sources = [meta, meta.get('http_classification')]
    for data in sources:
        if not isinstance(data, dict):
            continue
        a = _normalize_prediction(data.get('indobert_pred'))
        b = _normalize_prediction(data.get('ml_pred'))
        if a and b and a != b:
            return True
    return False


def selection_bucket(job_id, url, confidence, risk, reason, classifier_json, day=None):
    """Choose review bucket; return None to retain *legacy* immediate alert.

    All inputs already passed the existing BIMA-positive alert decision.
    A stable hash gives an unbiased-ish small sample opportunity without RNG
    or additional model calls. This is not a statistically uniform reservoir.
    """
    meta = _classifier(classifier_json)
    if _disagreement(meta):
        return 'disagreement'
    if ('hidden' in (reason or '').lower() or
            'hidden' in (risk or '').lower() or
            (risk == 'high_risk' and float(confidence) >= 0.85)):
        return 'strong'
    key = f'{day or utc_now()[:10]}:{job_id}:{url}'.encode('utf-8')
    if int.from_bytes(hashlib.sha256(key).digest()[:8], 'big') % 8 == 0:
        return 'sample'
    return None


def _image_folder(db_path):
    folder = Path(db_path).resolve().parent / 'prealert_evidence'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def _read_image(db_path, name, digest):
    if not FILE_NAME.fullmatch(str(name)):
        raise ValueError('invalid_evidence_file_name')
    path = _image_folder(db_path) / name
    # Never follow a symlink substituted for the selected screenshot.
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ValueError('evidence_not_regular_file')
        data = reader.read(MAX_IMAGE_BYTES + 1)
    if (not data.startswith(PNG_HEADER) or len(data) > MAX_IMAGE_BYTES or
            hashlib.sha256(data).hexdigest() != digest):
        raise ValueError('evidence_integrity_failure')
    return data


def _delete_image(db_path, name):
    if FILE_NAME.fullmatch(str(name)):
        try:
            (_image_folder(db_path) / name).unlink(missing_ok=True)
        except OSError:
            LOGGER.warning('prealert screenshot cleanup failed')


def enqueue_candidate(conn, job_id, url, confidence, screenshot_png):
    """Return True ONLY when a candidate is durably selected for review.

    False means legacy caller sends the alert immediately, unchanged. If this
    function raises, caller MUST NOT send; a partially committed selected case
    might exist and must be reconciled instead of bypassing reviewer approval.
    """
    if os.environ.get('BIMA_PREALERT_ENABLED') != '1':
        return False
    if not isinstance(screenshot_png, bytes) or not screenshot_png.startswith(PNG_HEADER):
        return False  # Legacy sender will treat malformed evidence as inconclusive.
    if not 0 < len(screenshot_png) <= MAX_IMAGE_BYTES:
        return False
    job = conn.execute('SELECT classifier_json,risk,reason,final_url FROM jobs WHERE id=?',
                       (job_id,)).fetchone()
    if not job:
        raise ValueError('job_missing_for_prealert')
    day = utc_now()[:10]
    bucket = selection_bucket(job_id, url, confidence, job[1], job[2], job[0], day)
    if not bucket:
        return False
    db_path = conn.execute('PRAGMA database_list').fetchone()[2]
    existing = conn.execute('SELECT id FROM bima_prealerts WHERE job_id=?', (job_id,)).fetchone()
    if existing:
        return True  # Never fall through to an automatic BIMA resend.
    # Atomic quota check and insert across processes. Screenshots are saved only
    # after all quota/size preconditions pass.
    name = None
    now = utc_now()
    conn.execute('BEGIN IMMEDIATE')
    try:
        counts = dict(conn.execute('SELECT bucket, COUNT(*) FROM bima_prealerts '
                                   'WHERE day_utc=? GROUP BY bucket', (day,)).fetchall())
        if sum(counts.values()) >= 10 or counts.get(bucket, 0) >= BUCKET_LIMITS[bucket]:
            conn.rollback()
            return False
        pending = conn.execute("SELECT COALESCE(SUM(image_bytes),0) FROM bima_prealerts "
                               "WHERE status NOT IN ('sent','rejected')").fetchone()[0]
        if pending + len(screenshot_png) > MAX_PENDING_BYTES:
            conn.rollback()
            LOGGER.warning('prealert storage cap reached: candidate follows legacy direct alert')
            return False
        name = secrets.token_hex(16) + '.png'
        path = _image_folder(db_path) / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(screenshot_png)
            out.flush()
            os.fsync(out.fileno())
        digest = hashlib.sha256(screenshot_png).hexdigest()
        cur = conn.execute(
            'INSERT INTO bima_prealerts(job_id,url,final_url,day_utc,bucket,confidence,'
            'classifier_json,evidence_sha256,image_filename,image_bytes,status,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, url, job[3], day, bucket, float(confidence), job[0],
             digest, name, len(screenshot_png), 'pending_telegram', now, now))
        conn.execute('INSERT INTO bima_prealert_events(prealert_id,event_type,actor,created_at) '
                     'VALUES(?,?,?,?)', (cur.lastrowid, 'selected', 'crawler', now))
        updated = conn.execute("UPDATE jobs SET status='awaiting_review', "
                               "reason='telegram_prealert_pending',updated_at=? "
                               "WHERE id=? AND status='pending_alert'", (now, job_id))
        if updated.rowcount != 1:
            raise RuntimeError('prealert_job_state_mismatch')
        save_results(conn, job_id, 'awaiting_review', 'telegram_prealert_pending')
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        if name:
            _delete_image(db_path, name)
        raise


def _reviewer_chat(allowed):
    chat = os.environ.get('BIMA_PREALERT_REVIEWER_CHAT_ID', '').strip()
    return chat if chat and authorized(chat, chat, allowed) else None


def pending_summary(db_path, chat, sender, allowed):
    if not authorized(chat, sender, allowed):
        return 'Antrean hanya tersedia untuk reviewer dalam chat pribadi.'
    with closing(db_connect(db_path)) as conn:
        rows = conn.execute('SELECT status,COUNT(*) FROM bima_prealerts GROUP BY status').fetchall()
        counts = dict(rows)
        samples = conn.execute("SELECT id,url,bucket FROM bima_prealerts "
                               "WHERE status IN ('pending_telegram','awaiting_review','needs_review') "
                               "ORDER BY id DESC LIMIT 5").fetchall()
    lines = ['BIMA | ANTREAN PRE-ALERT',
             'Menunggu Telegram: ' + str(counts.get('pending_telegram', 0)),
             'Menunggu keputusan: ' + str(counts.get('awaiting_review', 0)),
             'Perlu investigasi: ' + str(counts.get('needs_review', 0)),
             'Disetujui (proses): ' + str(counts.get('approved', 0)),
             'Alert terkirim: ' + str(counts.get('sent', 0)),
             'Pengiriman tidak pasti: ' + str(counts.get('send_unknown', 0))]
    for cid, url, bucket in samples:
        lines.append(f'#{cid} {urlsplit(url).hostname or "-"} [{bucket}]')
    return '\n'.join(lines)


def _send_document(token, chat, png, caption, reply_markup):
    """Upload unchanged original PNG using Telegram multipart POST (not photo)."""
    boundary = 'BIMA' + secrets.token_hex(12)
    b = boundary.encode('ascii')
    fields = {'chat_id': str(chat), 'caption': caption,
              'reply_markup': json.dumps(reply_markup, ensure_ascii=False)}
    chunks = []
    for name, value in fields.items():
        chunks.extend([b'--' + b + b'\r\n',
                       f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                       str(value).encode('utf-8'), b'\r\n'])
    chunks.extend([b'--' + b + b'\r\n',
                   b'Content-Disposition: form-data; name="document"; filename="bima-evidence.png"\r\n',
                   b'Content-Type: image/png\r\n\r\n', png, b'\r\n', b'--' + b + b'--\r\n'])
    request = Request('https://api.telegram.org/bot' + token + '/sendDocument',
                      data=b''.join(chunks),
                      headers={'Content-Type': 'multipart/form-data; boundary=' + boundary},
                      method='POST')
    with urlopen(request, timeout=35) as response:
        payload = json.load(response)
    if not payload.get('ok'):
        raise RuntimeError('telegram_document_send_rejected')
    return payload.get('result') or {}


def deliver_one(token, db_path, allowed, *, uploader=None):
    """Try at most one screenshot per poll; failure leaves durable case queued."""
    if os.environ.get('BIMA_PREALERT_ENABLED') != '1':
        return False
    chat = _reviewer_chat(allowed)
    if not chat:
        LOGGER.warning('prealert reviewer private chat is not configured/authorized')
        return False
    upload = uploader or _send_document
    now = datetime.now(timezone.utc)
    at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    retry_before = (now - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with closing(db_connect(db_path)) as conn:
        row = conn.execute("SELECT id,job_id,url,final_url,bucket,confidence,evidence_sha256,"
                           "image_filename,classifier_json FROM bima_prealerts "
                           "WHERE status='pending_telegram' AND "
                           "(notify_attempt_at IS NULL OR notify_attempt_at<=?) "
                           "ORDER BY id LIMIT 1", (retry_before,)).fetchone()
        if not row:
            return False
        cid, job_id, url, final_url, bucket, confidence, digest, filename, raw = row
        # Persist a five-minute retry delay BEFORE calling a remote service.
        with conn:
            conn.execute('UPDATE bima_prealerts SET notify_attempt_at=?,notify_attempts=notify_attempts+1 '
                         'WHERE id=? AND status=?', (at, cid, 'pending_telegram'))
    try:
        png = _read_image(db_path, filename, digest)
        meta = _classifier(raw)
        fallback = meta.get('http_classification') or {}
        if not isinstance(fallback, dict):
            fallback = {}
        indobert = str(meta.get('indobert_pred') or fallback.get('indobert_pred') or '-').replace('\n',' ')[:50]
        ml = str(meta.get('ml_pred') or fallback.get('ml_pred') or '-').replace('\n',' ')[:50]
        reason = str(meta.get('reason') or fallback.get('reason') or '-').replace('\n',' ')[:110]
        caption = ('BIMA | VERIFIKASI PRE-ALERT\n'
                   f'Kasus #{cid} | Job #{job_id}\n'
                   f'Domain: {urlsplit(url).hostname or "-"}\n'
                   f'Tujuan: {urlsplit(final_url or url).hostname or "-"}\n'
                   f'Pilihan: {bucket} | Confidence: {confidence:.2f}\n'
                   f'IndoBERT: {indobert} | Classic ML: {ml}\n'
                   f'Alasan: {reason}\n'
                   'Screenshot asli crawler. Verifikasi sebelum alert ke BIMA.')
        reply = {'inline_keyboard': [
            [{'text': 'Judol - kirim BIMA', 'callback_data': f'pa:{cid}:yes'}],
            [{'text': 'Bukan Judol', 'callback_data': f'pa:{cid}:no'},
             {'text': 'Bukti kurang', 'callback_data': f'pa:{cid}:defer'}]]}
        result = upload(token, chat, png, caption, reply)
        document = result.get('document') or {}
        if not document.get('file_id') or not result.get('message_id'):
            raise ValueError('telegram_missing_document_confirmation')
    except Exception as exc:
        # Do not log the URL/error text: it may contain the bot token.
        LOGGER.warning('prealert document not confirmed: %s', type(exc).__name__)
        return False
    with closing(db_connect(db_path)) as conn, conn:
        conn.execute("UPDATE bima_prealerts SET status='awaiting_review',telegram_chat_id=?,"
                     "telegram_message_id=?,telegram_file_id=?,updated_at=? "
                     "WHERE id=? AND status='pending_telegram'",
                     (chat, result['message_id'], document['file_id'], utc_now(), cid))
        conn.execute('INSERT INTO bima_prealert_events(prealert_id,event_type,actor,created_at) '
                     'VALUES(?,?,?,?)', (cid, 'telegram_delivered', 'telegram', utc_now()))
    return True


def decide(db_path, callback_data, chat, sender, allowed, chat_type):
    """Record exactly one private reviewer decision, with durable human label."""
    match = CALLBACK.fullmatch(str(callback_data or ''))
    if not match:
        return 'Pilihan verifikasi tidak valid.'
    if (chat_type != 'private' or chat != _reviewer_chat(allowed)
            or not authorized(chat, sender, allowed)):
        return 'Hanya reviewer terdaftar di chat pribadi yang dapat memutuskan.'
    cid, action = int(match[1]), match[2]
    status = {'yes': 'approved', 'no': 'rejected', 'defer': 'needs_review'}[action]
    label = {'yes': 'gambling', 'no': 'non_gambling', 'defer': 'inconclusive'}[action]
    job_status = {'yes': 'review_approved', 'no': 'review_rejected',
                  'defer': 'review_needs_evidence'}[action]
    now = utc_now()
    with closing(db_connect(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT job_id,image_filename,status FROM bima_prealerts WHERE id=?',
                               (cid,)).fetchone()
            if not row:
                conn.rollback()
                return 'Kasus tidak ditemukan.'
            job, filename, previous = row
            if previous != 'awaiting_review':
                conn.rollback()
                return f'Kasus #{cid} sudah berstatus {previous}; keputusan tidak diulang.'
            updated = conn.execute('UPDATE jobs SET status=?,updated_at=? '
                                   "WHERE id=? AND status='awaiting_review'",
                                   (job_status, now, job))
            if updated.rowcount != 1:
                raise RuntimeError('crawler_job_state_mismatch')
            conn.execute('UPDATE bima_prealerts SET status=?,feedback_label=?,reviewer_id=?, '
                         'reviewed_at=?,updated_at=? WHERE id=?',
                         (status, label, sender, now, now, cid))
            conn.execute('INSERT INTO bima_prealert_events(prealert_id,event_type,actor,created_at) '
                         'VALUES(?,?,?,?)', (cid, 'human_' + label, 'telegram:' + sender, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if action == 'no':
        _delete_image(db_path, filename)
    return {'yes':f'Kasus #{cid} disetujui. Alert BIMA masuk antrean pengiriman.',
            'no':f'Kasus #{cid} ditolak. Tidak ada alert BIMA. Label disimpan.',
            'defer':f'Kasus #{cid} ditunda. Tidak ada alert BIMA sampai ditinjau ulang.'}[action]


def _sender_config(config_path=None):
    path = (config_path or os.environ.get('BIMA_PREALERT_CONFIG_PATH') or
            str(Path(os.environ.get('PANDI_CRAWLER_BASE_DIR',
                        str(Path(__file__).resolve().parents[1]))) / 'config.json'))
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(cfg, dict) or not cfg.get('ENDPOINT_URL') or not cfg.get('X_API_KEY'):
        raise ValueError('missing_bima_endpoint_or_key')
    url = urlsplit(cfg['ENDPOINT_URL'])
    if url.scheme not in ('https','http') or not url.hostname or url.username or url.password:
        raise ValueError('invalid_bima_endpoint')
    return cfg


def _payload(url, png, confidence, cfg):
    if not isinstance(url, str) or not urlsplit(url).hostname or not urlsplit(url).hostname.endswith('.id'):
        raise ValueError('only_id_alert_url_is_supported')
    safe_confidence = max(float(confidence or 0), float(cfg.get('MIN_ALERT_CONFIDENCE', 0.60)))
    percent = int(round(safe_confidence * 100))
    if percent <= 50:
        percent = 60
    return {'url': url, 'brand': '-', 'discovered': utc_now(), 'confidence_level': percent,
            'status':'0', 'source':'crawling', 'resolved':'0', 'category':['online gambling'],
            'screenshots':['data:image/png;base64,'+base64.b64encode(png).decode('ascii')]}


def _post_bima(payload, cfg):
    """Same wire contract as crawler_url.send_alert(): ONE POST, no redirects/retry."""
    import requests
    with requests.Session() as session:
        response = session.post(
            cfg['ENDPOINT_URL'],
            headers={'Content-Type':'application/json','X-API-key':cfg['X_API_KEY'],
                     'Connection':'close'},
            json=payload, allow_redirects=False,
            timeout=(float(cfg.get('ALERT_CONNECT_TIMEOUT',5)),
                     float(cfg.get('ALERT_READ_TIMEOUT',30))))
        try:
            return response.status_code == 200
        finally:
            response.close()


def dispatch_one(db_path, *, sender=None, config_path=None):
    """Dispatch exactly one approved case, never replay uncertain sends.

    status='sending' is durably committed BEFORE network I/O. A process crash,
    timeout or HTTP !=200 leaves 'send_unknown' or 'sending' for reconciliation.
    """
    if os.environ.get('BIMA_PREALERT_ENABLED') != '1':
        return None
    with closing(db_connect(db_path)) as conn:
        row = conn.execute("SELECT id,job_id,url,confidence,image_filename,evidence_sha256 "
                           "FROM bima_prealerts WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        cid, job_id, url, confidence, filename, digest = row
        # Check local prerequisites before claiming the irreversible send intent.
        try:
            cfg = _sender_config(config_path)
            png = _read_image(db_path, filename, digest)
            payload = _payload(url, png, confidence, cfg)
        except Exception as exc:
            LOGGER.warning('approved case cannot be dispatched: %s', type(exc).__name__)
            return False
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = conn.execute('UPDATE bima_prealerts SET status=?,bima_attempted_at=?,updated_at=? '
                                  "WHERE id=? AND status='approved'",
                                  ('sending',utc_now(),utc_now(),cid))
            if result.rowcount != 1:
                conn.rollback()
                return False
            conn.execute('INSERT INTO bima_prealert_events(prealert_id,event_type,actor,created_at) '
                         'VALUES(?,?,?,?)', (cid,'bima_send_started','dispatcher',utc_now()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    try:
        ok = (sender or _post_bima)(payload, cfg)
    except Exception as exc:
        LOGGER.warning('BIMA alert delivery not confirmed: %s', type(exc).__name__)
        ok = False
    with closing(db_connect(db_path)) as conn, conn:
        next_status = 'sent' if ok else 'send_unknown'
        job_status = 'alerted' if ok else 'prealert_send_unknown'
        conn.execute('UPDATE bima_prealerts SET status=?,bima_result_at=?,updated_at=? '
                     "WHERE id=? AND status='sending'", (next_status,utc_now(),utc_now(),cid))
        conn.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=? AND status=?',
                     (job_status, 'bima_alert_sent_after_review' if ok else
                      'bima_prealert_send_unknown_manual_reconcile', utc_now(),job_id,'review_approved'))
        conn.execute('INSERT INTO bima_prealert_events(prealert_id,event_type,actor,created_at) '
                     'VALUES(?,?,?,?)', (cid,'bima_sent' if ok else 'bima_send_unknown',
                                         'dispatcher',utc_now()))
    if ok:
        _delete_image(db_path, filename)
    return {'id':cid, 'ok':bool(ok)}
