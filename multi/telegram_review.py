"""Private Telegram incident decisions and independent text labels."""
import os
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit
from pathlib import Path

from learning import core

INCIDENT = {"yes": "true_positive", "no": "false_positive"}
TEXT = {"pos": "gambling", "neg": "non_gambling", "skip": "not_for_training"}


def authorized(chat, sender, allowed):
    return (chat == sender and chat in allowed and chat.isascii() and chat.isdecimal()
            and sender in os.getenv("TELEGRAM_REVIEW_USER_IDS", "").replace(",", " ").split())


def card(db, production, case_id=None, site_only=False):
    with core.learning_connection(db, production) as conn:
        sql = ("SELECT c.id,c.source_job_id,c.hostname,c.final_hostname,c.source_updated_at,"
               "c.label,t.verdict,e.text_value FROM review_cases c "
               "LEFT JOIN telegram_incidents t ON t.case_id=c.id "
               "LEFT JOIN case_evidence e ON e.case_id=c.id ")
        if case_id is None:
            row = conn.execute(sql + "WHERE c.label IS NULL AND t.case_id IS NULL "
                               "ORDER BY c.id LIMIT 1").fetchone()
        else:
            row = conn.execute(sql + "WHERE c.id=?", (case_id,)).fetchone()
    if not row:
        return "Kasus tidak ditemukan." if case_id else "Antrean keputusan situs kosong."
    key, job, host, final, when, label, verdict, evidence = row
    header = f"Kasus #{key} · job #{job}\nSitus: {host}\nTujuan: {final or host}\nWaktu crawl: {when or '-'}"
    source = Path(production).resolve()
    if source.is_file():
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
            urlrow = conn.execute("SELECT url FROM jobs WHERE id=?", (job,)).fetchone()
        if urlrow and core.hostname(urlrow[0]) == host:
            path = urlsplit(urlrow[0]).path or "/"
            header += "\nPath awal: " + path[:200]
    if label in core.FINAL_LABELS:
        decision = "Judol" if label == "true_positive" else "Bukan judol"
        return header + "\nKeputusan tersimpan: " + decision + ". /review untuk kasus berikutnya."
    body = header
    if evidence:
        body += "\n\nCuplikan bukti:\n" + evidence[:600]
    if not verdict:
        if not evidence:
            body += "\n\nBukti teks belum tersedia. Periksa bukti job sebelum memilih."
        if evidence and len(evidence) <= 600 and not site_only:
            body += ("\n\nTombol Judol/Bukan judol memberi label yang sama untuk situs dan teks "
                     "di atas. Jika berbeda atau teks kurang jelas, pilih Nilai situs saja.")
            options = [("Judol", "bothpos"), ("Bukan judol", "bothneg"), ("Nilai situs saja", "site")]
        else:
            body += "\n\nMenurut Anda, situs ini judol?"
            options = [("Judol", "yes"), ("Bukan judol", "no")]
    else:
        body += "\n\nKeputusan situs: " + ("Judol" if verdict == "true_positive" else "Bukan judol")
        if not evidence:
            return body + f"\nUntuk melatih dari teks, kirim /bukti {key} diikuti kutipan halaman (40–600 karakter).\n/review untuk situs berikutnya."
        if len(evidence) > 600:
            return body + "\nBukti lengkap lebih panjang. Tinjau seluruh teks lewat alur CLI sebelum memberi label teks."
        body += "\nApakah teks bukti itu sendiri mempromosikan judi?"
        options = [("Ya", "pos"), ("Tidak", "neg"), ("Abaikan teks", "skip")]
    buttons = [[{"text": name, "callback_data": f"br:{key}:{action}"} for name, action in options]]
    return {"text": body, "reply_markup": {"inline_keyboard": buttons}}


def handle(text, chat, sender, allowed, production, callback=False):
    if not authorized(chat, sender, allowed):
        return "Review hanya tersedia di chat pribadi reviewer terdaftar."
    db = os.getenv("BIMA_LEARNING_DB") or str(Path(__file__).resolve().parent /
                                             "state/bima_learning_v450.sqlite3")
    if core._same_file_or_path(db, production):
        return "Konfigurasi database learning harus terpisah dari database crawler."
    parts = text.split(maxsplit=2)
    if not callback:
        if parts[0].split("@")[0].lower() == "/review":
            if len(parts) == 1:
                return card(db, production)
            if len(parts) == 2 and re.fullmatch(r"[1-9][0-9]{0,17}", parts[1]):
                return card(db, production, int(parts[1]))
            return "Gunakan /review atau /review ID."
        if len(parts) != 3 or not re.fullmatch(r"[1-9][0-9]{0,17}", parts[1]) or not 40 <= len(parts[2].strip()) <= 600:
            return "Gunakan /bukti ID diikuti 40–600 karakter teks asli halaman."
        case_id = int(parts[1])
        try:
            core.attach_evidence_text(db, case_id, parts[2], "manual_capture")
        except ValueError:
            return "Bukti tidak dapat disimpan. Periksa ID, bukti terdahulu, atau status final."
        return card(db, production, case_id)
    match = re.fullmatch(r"br:([1-9][0-9]{0,17}):(yes|no|pos|neg|skip|bothpos|bothneg|site)", text)
    if not match:
        return "Tombol review tidak valid."
    case_id, action = int(match[1]), match[2]
    if action == "site":
        return card(db, production, case_id, site_only=True)
    reviewer = "telegram:" + sender
    with core.learning_connection(db, production) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT label FROM review_cases WHERE id=?", (case_id,)).fetchone()
        if not row or row[0] in core.FINAL_LABELS:
            return "Kasus sudah final atau tidak ditemukan. Kirim /review."
        if action in ("bothpos", "bothneg"):
            evidence = conn.execute("SELECT length(text_value) FROM case_evidence WHERE case_id=?",
                                    (case_id,)).fetchone()
            if not evidence or not 40 <= evidence[0] <= 600:
                return "Buka kembali kasus untuk melihat bukti teks."
            # review_case stores both decisions atomically and checks prior incident decisions.
            combined = action == "bothpos"
        if action in INCIDENT:
            conn.execute("INSERT OR IGNORE INTO telegram_incidents VALUES(?,?,?,?)",
                         (case_id, INCIDENT[action], reviewer, core.utcnow()))
        incident = conn.execute("SELECT verdict FROM telegram_incidents WHERE case_id=?",
                                (case_id,)).fetchone()
        if action in TEXT:
            evidence = conn.execute("SELECT length(text_value) FROM case_evidence WHERE case_id=?",
                                    (case_id,)).fetchone()
            if not incident or not evidence or evidence[0] > 600:
                return "Buka kembali kasus dengan /review ID untuk meninjau bukti."
    if action in ("bothpos", "bothneg"):
        try:
            core.review_case(db, case_id, "true_positive" if combined else "false_positive",
                             reviewer, "Keputusan situs dan teks dikonfirmasi di Telegram",
                             text_label="gambling" if combined else "non_gambling")
        except ValueError:
            return "Keputusan sudah tercatat. Lihat /review " + str(case_id)
    if action in TEXT:
        try:
            core.review_case(db, case_id, incident[0], reviewer, "Label teks dikonfirmasi di Telegram",
                             text_label=TEXT[action])
        except ValueError:
            return "Keputusan tidak dapat diubah. Lihat /review " + str(case_id)
    return card(db, production, case_id)
