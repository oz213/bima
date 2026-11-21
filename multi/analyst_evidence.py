"""Bounded, read-only access to saved BIMA evidence; no crawler/model imports."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import re
import sqlite3
import time
import unicodedata
from urllib.parse import urlsplit

import idna


MAX_URL = 8192
MAX_JSON = 65536
MAX_SQL_STEPS = 250000
SQL_SECONDS = 0.75
MAX_TEXT_INPUT = 16384
RISK_VALUES = frozenset(("low_risk", "medium_risk", "high_risk", "inconclusive",
                         "blocked_or_captcha", "hidden_link_evidence"))
FINAL_LABELS = frozenset(("true_positive", "false_positive"))
JOB_FIELDS = (
    "id", "url", "final_url", "status", "risk", "reason", "route_reason",
    "updated_at", "http_status", "attempts", "error_category", "dns_result",
    "next_retry_at", "classifier_json",
)
NOTES = (
    "Bukti berasal dari hasil tersimpan; tidak ada crawl atau inferensi baru.",
    "updated_at adalah waktu perubahan status, bukan jaminan waktu pengambilan halaman.",
    "jobs menyimpan hasil terkini per URL, bukan riwayat lengkap; screenshot tidak disimpan.",
    "Skor model tersimpan bukan peluang pasti bahwa domain berbahaya.",
)
URL_TEXT = re.compile(r"\b(?:[a-z][a-z0-9+.-]{1,15}://|www\.)[^\s<>\"']+", re.I)
RELATIVE_URL_TEXT = re.compile(r"(?<![\w:])//[^\s<>\"']+")
BARE_URL_TEXT = re.compile(
    r"(?<![\w/@])(?:[^\s<>\"'/]+@)?(?:[\w-]+\.)+[\w-]{2,63}\.?(?::(?:80|443))?"
    r"[/?#][^\s<>\"']*", re.I)
SECRET_VALUE = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization)"
    r"\s*[:=]\s*(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.I)
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I)
TELEGRAM_TOKEN = re.compile(r"\b[0-9]{5,15}:[A-Za-z0-9_-]{20,}\b")
JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
OPAQUE_TOKEN = re.compile(r"(?<![\w])[A-Za-z0-9_-]{28,}(?![\w])")
LOCAL_PATH = re.compile(r"(?:/(?:home|Users|workspace|tmp|var|etc|root)/|[A-Z]:\\)[^\s<>\"']+", re.I)


class EvidenceError(ValueError):
    """Safe operator-facing error, without database paths or input secrets."""


def _host(value, *, require_id=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise EvidenceError("Hostname tidak valid.")
    if any(c.isspace() or ord(c) < 33 or c in "/\\@%_*" for c in value):
        raise EvidenceError("Hostname tidak valid.")
    try:
        mapped = idna.uts46_remap(value, std3_rules=True, transitional=False)
        if mapped.endswith("."):
            mapped = mapped[:-1]
        host = idna.encode(mapped, uts46=False).decode("ascii").lower()
    except (UnicodeError, ValueError, idna.IDNAError):
        raise EvidenceError("Hostname tidak valid.") from None
    if len(host) > 253 or "." not in host or (require_id and not host.endswith(".id")):
        raise EvidenceError("Gunakan hostname .id yang valid.")
    return host


def _parts(value, *, require_id=False, reject_credentials=True):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in value)):
        raise EvidenceError("URL tidak valid.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise EvidenceError("Gunakan URL HTTP(S) .id yang valid.")
        if reject_credentials and (parsed.username is not None or parsed.password is not None):
            raise EvidenceError("URL dengan kredensial tidak didukung.")
        if parsed.port not in (None, 80, 443):
            raise EvidenceError("Port URL tidak didukung.")
        return parsed, _host(parsed.hostname, require_id=require_id)
    except (ValueError, UnicodeError):
        raise EvidenceError("URL tidak valid.") from None


def _url_display(value):
    try:
        parsed, host = _parts(value, reject_credentials=False)
    except EvidenceError:
        return "(URL tersimpan tidak valid)"
    port = ":%d" % parsed.port if parsed.port is not None else ""
    path = "/[path redacted]" if parsed.path not in ("", "/") else "/"
    return parsed.scheme + "://" + host + port + path


def redact_text(value, max_chars=600):
    """Bound untrusted text and remove URL payloads and recognizable credentials.

    URL paths are hidden in full: deciding which individual path segments are
    secret is unreliable. This is conservative redaction, not a claim that an
    arbitrary passage can be proven free of every possible secret.
    """
    if not isinstance(value, str) or type(max_chars) is not int or max_chars <= 0:
        return ""
    maximum = min(max_chars, 4000)
    text = value[:MAX_TEXT_INPUT]
    text = "".join(c if unicodedata.category(c) not in ("Cc", "Cf") else " " for c in text)
    text = URL_TEXT.sub(lambda match: _url_display(
        "https://" + match[0] if match[0].lower().startswith("www.") else match[0]), text)
    text = RELATIVE_URL_TEXT.sub(lambda match: _url_display("https:" + match[0]), text)
    text = BARE_URL_TEXT.sub(lambda match: _url_display("https://" + match[0]), text)
    text = LOCAL_PATH.sub("[path redacted]", text)
    text = SECRET_VALUE.sub("[credential redacted]", text)
    text = BEARER.sub("[credential redacted]", text)
    text = TELEGRAM_TOKEN.sub("[credential redacted]", text)
    text = JWT.sub("[credential redacted]", text)
    text = OPAQUE_TOKEN.sub("[token redacted]", text)
    return " ".join(text.split())[:maximum]


def _selector(target):
    if isinstance(target, int) and not isinstance(target, bool):
        target = "#%d" % target
    if (not isinstance(target, str) or not target or len(target) > MAX_URL
            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        raise EvidenceError("Pilih domain .id, URL, atau #job yang valid.")
    value = target.strip()
    if re.fullmatch(r"#[0-9]+", value):
        if len(value) > 20:
            raise EvidenceError("Nomor job tidak valid.")
        key = int(value[1:])
        if not 1 <= key <= 9223372036854775807:
            raise EvidenceError("Nomor job tidak valid.")
        return {"kind": "job", "value": "#%d" % key, "hostname": None}, key
    if any(char in value for char in ("/", "?", "#")):
        url = ("https:" + value if value.startswith("//") else
               value if "://" in value else "https://" + value)
        _, host = _parts(url, require_id=True)
        return {"kind": "url", "value": _url_display(url), "hostname": host}, url.split("#", 1)[0]
    host = _host(value, require_id=True)
    return {"kind": "hostname", "value": host, "hostname": host}, value


@contextmanager
def _database(path):
    connection = None
    try:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise EvidenceError("Basis data bukti tidak tersedia.")
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=100")
        started = time.monotonic()
        work = 0

        def stop():
            nonlocal work
            work += 1000
            return int(work > MAX_SQL_STEPS or time.monotonic() - started > SQL_SECONDS)

        connection.set_progress_handler(stop, 1000)
        connection.execute("BEGIN")
        yield connection
    except (OSError, sqlite3.Error, TypeError):
        raise EvidenceError("Bukti tidak dapat dibaca dalam batas aman; coba #job.") from None
    finally:
        if connection is not None:
            connection.close()


def _columns(conn, table):
    return {row[1]: row for row in conn.execute('PRAGMA table_info("%s")' % table)}


def _leading_index(conn, table, column, *, binary=False):
    for row in conn.execute('PRAGMA index_list("%s")' % table):
        if row[4]:  # A partial index cannot cover all URL lookups.
            continue
        name = row[1].replace('"', '""')
        fields = [field for field in conn.execute('PRAGMA index_xinfo("%s")' % name) if field[5]]
        if (fields and fields[0][2] == column
                and (not binary or fields[0][4].upper() == "BINARY")):
            return '"' + name + '"'
    return None


def _url_index(conn):
    index = _leading_index(conn, "jobs", "url", binary=True)
    if index is not None:
        return index
    raise EvidenceError("Indeks URL yang sesuai tidak tersedia; gunakan #job.")


def _projection(columns, fields=JOB_FIELDS):
    result = []
    for name in fields:
        if name not in columns:
            result.append('NULL AS "%s"' % name)
        elif name == "classifier_json":
            result.append('CASE WHEN length(classifier_json)<=%d THEN classifier_json END AS classifier_json' % MAX_JSON)
        elif name in ("url", "final_url"):
            result.append('substr("%s",1,%d) AS "%s"' % (name, MAX_URL + 1, name))
        elif name in ("id", "http_status", "attempts"):
            result.append('"%s"' % name)
        else:
            result.append('substr("%s",1,128) AS "%s"' % (name, name))
    return ",".join(result)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 32:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _token(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) else None


def _gap(gaps, message):
    if message not in gaps and len(gaps) < 12:
        gaps.append(message)


def _score(value):
    return float(value) if type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value) else None


def _classification(data, gaps, context):
    if not isinstance(data, dict):
        return {}
    result = {}
    if isinstance(data.get("status"), str) and data["status"] in RISK_VALUES:
        result["status"] = data["status"]
    reason = _token(data.get("reason"))
    if reason is not None:
        result["reason"] = reason
    for key in ("captcha_hit", "kw_hit", "bert_used"):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    source = data.get("ml_source")
    if source in ("classic_ml", "indobert"):
        result["ml_source"] = source
        if source != "indobert" or data.get("bert_used") is not False:
            if data.get("ml_pred") in ("gambling", "non-gambling"):
                result["ml_pred"] = data["ml_pred"]
            score = _score(data.get("ml_conf"))
            if score is not None:
                result["ml_conf"] = score
            if type(data.get("ml_hit")) is bool:
                result["ml_hit"] = data["ml_hit"]
        if source == "indobert":
            _gap(gaps, context + ": hasil model klasik terpisah tidak disimpan.")
    else:
        _gap(gaps, context + ": prediksi model terukur tidak dicatat; nilai nol aturan bukan inferensi.")
    if data.get("bert_used") is True:
        prediction = data.get("indobert_pred")
        if prediction in ("gambling", "non-gambling", "unknown", "disabled"):
            result["indobert_pred"] = prediction
        if prediction in ("gambling", "non-gambling"):
            score = _score(data.get("indobert_conf"))
            if score is not None:
                result["indobert_conf"] = score
        else:
            _gap(gaps, context + ": IndoBERT dipanggil tetapi hasil inferensi tidak tersedia.")
    duration = data.get("bert_ms")
    if type(duration) is int and 0 <= duration <= 86400000:
        result["bert_ms"] = duration
    hits = data.get("strong_hits")
    if isinstance(hits, list):
        cleaned = []
        for hit in hits[:100]:
            text = redact_text(hit, 80)
            if text and text not in cleaned:
                cleaned.append(text)
            if len(cleaned) == 10:
                break
        result["strong_hits"] = cleaned
    return result


def _metadata(raw, gaps, context):
    def nonfinite(_):
        raise ValueError("invalid numeric constant")

    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_JSON:
            raise ValueError("missing or oversized")
        parsed = json.loads(raw, parse_constant=nonfinite)
        if not isinstance(parsed, dict):
            raise ValueError("expected object")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _gap(gaps, context + ": metadata klasifikasi tidak tersedia atau tidak valid.")
        return {}, {}, {}
    final = _classification(parsed, gaps, context)
    http = _classification(parsed.get("http_classification"), gaps, context + " HTTP")
    return parsed, final, http


def _record(row):
    try:
        _, initial = _parts(row["url"])
    except EvidenceError:
        initial = None
    try:
        _, final = _parts(row["final_url"])
    except EvidenceError:
        final = None
    data = {
        "id": row["id"], "ref": "job#%d" % row["id"],
        "url_display": _url_display(row["url"]), "initial_host": initial,
        "final_host": final, "updated_at": _timestamp(row["updated_at"]),
        "next_retry_at": _timestamp(row["next_retry_at"]),
        "classification": {}, "http_classification": {}, "review_excerpt": None,
        "review": None, "data_gaps": [],
    }
    for name in ("status", "risk", "reason", "route_reason", "error_category", "dns_result"):
        data[name] = _token(row[name])
    for name in ("http_status", "attempts"):
        value = row[name]
        data[name] = value if type(value) is int and 0 <= value <= 1000000 else None
    raw, final, http = _metadata(row["classifier_json"], data["data_gaps"], "Hasil terkini")
    data["classification"] = final
    data["http_classification"] = http
    data["review_excerpt"] = redact_text(raw.get("review_excerpt"), 600) or None
    if data["review_excerpt"] is None:
        _gap(data["data_gaps"], "Cuplikan teks crawl tidak disimpan.")
    if not http:
        _gap(data["data_gaps"], "Klasifikasi HTTP terpisah tidak tersedia; browser mungkin hanya mengambil screenshot.")
    return data


REVIEW_FIELDS = (
    "id", "source_job_id", "hostname", "final_hostname", "source_updated_at", "created_at",
    "source_risk", "classifier_status", "classifier_reason", "route_reason", "model_metadata_json",
    "snapshot_sha256", "label", "text_label", "reviewed_at",
)


def _review_projection(columns, fields):
    result = []
    for field in fields:
        if field not in columns:
            result.append('NULL AS "%s"' % field)
        elif field == "model_metadata_json":
            result.append('CASE WHEN length(model_metadata_json)<=%d THEN model_metadata_json END AS model_metadata_json' % MAX_JSON)
        elif field in ("id", "source_job_id", "case_id"):
            result.append('"%s"' % field)
        else:
            maximum = MAX_TEXT_INPUT if field == "text_value" else 1024 if field.endswith("hostname") else 128
            result.append('substr("%s",1,%d) AS "%s"' % (field, maximum, field))
    return ",".join(result)


def _review_side(conn, table, key, fields):
    columns = _columns(conn, table)
    if "case_id" not in columns:
        return None
    index = _leading_index(conn, table, "case_id")
    primary = columns["case_id"][5] == 1 and columns["case_id"][2].upper() == "INTEGER"
    if index is None and not primary:
        return None
    hint = " INDEXED BY " + index if index is not None else ""
    return conn.execute("SELECT %s FROM %s%s WHERE case_id=? LIMIT 1" %
                        (_review_projection(columns, fields), table, hint), (key,)).fetchone()


def _saved_host(value):
    try:
        return _host(value)
    except EvidenceError:
        return None


def _sha256(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _review_record(conn, row, gaps):
    _, final, http = _metadata(row["model_metadata_json"], gaps, "Snapshot review")
    review = {
        "case_id": row["id"], "source_job_id": row["source_job_id"],
        "hostname": _saved_host(row["hostname"]), "final_hostname": _saved_host(row["final_hostname"]),
        "source_updated_at": _timestamp(row["source_updated_at"]), "created_at": _timestamp(row["created_at"]),
        "source_risk": _token(row["source_risk"]), "classifier_status": _token(row["classifier_status"]),
        "classifier_reason": _token(row["classifier_reason"]), "route_reason": _token(row["route_reason"]),
        "classification": final, "http_classification": http,
        "label": row["label"] if row["label"] in ("true_positive", "false_positive", "needs_review") else None,
        "text_label": row["text_label"] if row["text_label"] in ("gambling", "non_gambling", "not_for_training") else None,
        "incident_verdict": None, "reviewed_at": _timestamp(row["reviewed_at"]),
        "excerpt": None, "excerpt_origin": None, "excerpt_attached_at": None,
        "snapshot_sha256": _sha256(row["snapshot_sha256"]), "evidence_sha256": None,
    }
    if type(row["id"]) is not int or not 1 <= row["id"] <= 9223372036854775807:
        _gap(gaps, "Snapshot review memiliki nomor kasus tidak valid.")
        return None
    evidence = _review_side(conn, "case_evidence", row["id"],
                            ("text_value", "text_sha256", "origin", "attached_at"))
    if evidence is not None:
        review["excerpt"] = redact_text(evidence["text_value"], 600) or None
        if evidence["origin"] in ("crawler_excerpt", "manual_capture", "approved_archive", "screenshot_transcript"):
            review["excerpt_origin"] = evidence["origin"]
        review["excerpt_attached_at"] = _timestamp(evidence["attached_at"])
        review["evidence_sha256"] = _sha256(evidence["text_sha256"])
    incident = _review_side(conn, "telegram_incidents", row["id"], ("verdict",))
    if incident is not None and incident["verdict"] in FINAL_LABELS:
        review["incident_verdict"] = incident["verdict"]
    _gap(gaps, "Review adalah snapshot terpisah; kutipan manual dapat dilampirkan setelah crawl.")
    return review


def _attach_reviews(learning_db, records):
    if not records:
        return
    if learning_db is None:
        for record in records:
            _gap(record["data_gaps"], "Snapshot review tidak tersedia.")
        return
    try:
        with _database(learning_db) as conn:
            columns = _columns(conn, "review_cases")
            if not {"id", "source_job_id"}.issubset(columns):
                raise EvidenceError("Tabel snapshot review tidak tersedia.")
            index = _leading_index(conn, "review_cases", "source_job_id")
            if index is None:
                raise EvidenceError("Snapshot review dilewati karena indeks job tidak tersedia.")
            found = []
            for record in records:
                row = conn.execute("SELECT %s FROM review_cases INDEXED BY %s WHERE source_job_id=? LIMIT 1" %
                                   (_review_projection(columns, REVIEW_FIELDS), index), (record["id"],)).fetchone()
                found.append(_review_record(conn, row, record["data_gaps"]) if row is not None else None)
        for record, review in zip(records, found):
            record["review"] = review
            if review is None:
                _gap(record["data_gaps"], "Snapshot review untuk job ini tidak tersedia.")
    except EvidenceError as exc:
        for record in records:
            _gap(record["data_gaps"], str(exc))


def lookup_evidence(production_db, target, *, learning_db=None, max_jobs=5, max_candidates=128):
    """Read exact saved jobs or a bounded sample for one exact .id hostname.

    Host searches intentionally cannot prove absence across arbitrary stored
    spellings/subdomains. They use existing BINARY URL indexes without migrations.
    """
    if type(max_jobs) is not int or not 1 <= max_jobs <= 20:
        raise EvidenceError("Batas hasil harus 1–20.")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 1024:
        raise EvidenceError("Batas kandidat harus 1–1024.")
    selector, private_value = _selector(target)
    coverage = {
        "mode": {"job": "exact_job", "url": "exact_url", "hostname": "bounded_host"}[selector["kind"]],
        "candidates_examined": 0, "candidate_limit": max_candidates, "returned": 0,
        "truncated": False, "limited": selector["kind"] == "hostname",
        "hostname_scope": "exact",
    }
    notes = list(NOTES)
    if coverage["limited"]:
        notes.append("Pencarian hostname terbatas pada ejaan URL yang didukung; subdomain dan seluruh variasi huruf tidak dicakup.")
    with _database(production_db) as conn:
        columns = _columns(conn, "jobs")
        if ("id" not in columns or "url" not in columns or columns["id"][5] != 1
                or columns["id"][2].upper() != "INTEGER"):
            raise EvidenceError("Skema jobs tidak mendukung pembacaan berindeks.")
        selected = []
        if selector["kind"] == "job":
            selected = conn.execute("SELECT id FROM jobs WHERE id=?", (private_value,)).fetchall()
            coverage["candidates_examined"] = len(selected)
        elif selector["kind"] == "url":
            index = _url_index(conn)
            selected = conn.execute("SELECT id FROM jobs INDEXED BY %s WHERE url COLLATE BINARY=? LIMIT 2" % index,
                                    (private_value,)).fetchall()
            coverage["candidates_examined"] = len(selected)
        else:
            index = _url_index(conn)
            host = selector["hostname"]
            unicode_host = idna.decode(host)
            spellings = list(dict.fromkeys((host, unicode_host, private_value.rstrip("."))))
            candidate_fields = _projection(columns, ("id", "url", "updated_at"))
            candidates = []
            for spelling in spellings:
                for scheme in ("https", "http"):
                    for port in ("", ":443", ":80"):
                        for suffix in ("", "."):
                            base = scheme + "://" + spelling + suffix + port
                            for delimiter in ("", "/", "?"):
                                remaining = max_candidates - coverage["candidates_examined"]
                                if remaining <= 0:
                                    coverage["truncated"] = True
                                    break
                                query = "SELECT %s FROM jobs INDEXED BY %s WHERE " % (candidate_fields, index)
                                if delimiter:
                                    prefix = base + delimiter
                                    end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                                    query += "url COLLATE BINARY>=? AND url COLLATE BINARY<? ORDER BY jobs.url COLLATE BINARY LIMIT ?"
                                    params = (prefix, end, remaining + 1)
                                else:
                                    query += "url COLLATE BINARY=? LIMIT ?"
                                    params = (base, remaining + 1)
                                rows = conn.execute(query, params).fetchall()
                                if len(rows) > remaining:
                                    coverage["truncated"] = True
                                for row in rows[:remaining]:
                                    coverage["candidates_examined"] += 1
                                    try:
                                        _, candidate_host = _parts(row["url"], require_id=True)
                                    except EvidenceError:
                                        continue
                                    if candidate_host == host:
                                        candidates.append(row)
            selected = sorted(candidates, key=lambda row: (_timestamp(row["updated_at"]) or "", row["id"]),
                              reverse=True)
            if len(selected) > max_jobs:
                coverage["truncated"] = True
        ids = list(dict.fromkeys(row["id"] for row in selected))[:max_jobs]
        records = []
        for key in ids:
            row = conn.execute("SELECT %s FROM jobs WHERE id=?" % _projection(columns), (key,)).fetchone()
            if row is not None:
                records.append(_record(row))
    _attach_reviews(learning_db, records)
    coverage["returned"] = len(records)
    if not records:
        notes.append("Tidak ditemukan dalam pencarian ini; ini bukan bukti aman atau belum pernah dicrawl.")
    return {"selector": selector, "coverage": coverage, "records": records, "notes": notes}
