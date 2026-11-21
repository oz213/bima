#!/usr/bin/env python3
"""Sourced, read-only domain analysis, shared by Telegram and the operator CLI.

No crawler/model initialization or action is performed here. Each answer reads
the selected saved jobs again; conversation context is not a crawl history.
"""
import argparse
import json
import logging
import math
import os
from pathlib import Path
import re

from analyst_evidence import EvidenceError, lookup_evidence, redact_text
from analyst_llm import generate_answer

LOGGER = logging.getLogger('pandi.domain.analyst')
MAX_TEXT = 3800
MAX_RECORDS = 5
MAX_QUESTION = 1000
MAX_HISTORY = 2
DEFAULT_QUESTION = ('Jelaskan hasil tersimpan, alasan klasifikasi, perbedaan '
                    'sinyal bila ada, dan keterbatasan buktinya.')
STATUS_MEANINGS = {
    'pending': 'Pemeriksaan masih menunggu; belum ada kesimpulan baru.',
    'http_inflight': 'HTTP tercatat sedang berjalan; hasil belum final.',
    'browser_queued': 'Masih menunggu/menjalani browser; hasil belum final.',
    'retry_http': 'Belum dapat disimpulkan; menunggu retry HTTP/DNS, bukan bukti aman.',
    'inconclusive': 'Belum dapat disimpulkan; keterbatasan pemeriksaan bukan bukti aman.',
    'failed': 'Belum dapat disimpulkan; pemeriksaan gagal, bukan bukti aman.',
    'pending_alert': 'Pengiriman alert BIMA belum terkonfirmasi; periksa penerimaan di BIMA.',
    'alerted': 'Pengiriman alert dicatat berhasil oleh crawler; penerimaan akhir perlu verifikasi BIMA.',
    'dry_run_alert': 'Simulasi alert; tidak membuktikan pengiriman ke BIMA.',
    'complete': 'Pemeriksaan dicatat selesai; hasil ini tidak menjamin keadaan situs saat ini.',
}
MODE_NOTICES = {
    'disabled': 'Mode laporan bukti. Model percakapan belum diaktifkan.',
    'unavailable': 'Laporan bukti: penyedia model belum dapat memberi jawaban.',
    'configuration_error': 'Laporan bukti: konfigurasi penyedia model perlu diperiksa operator.',
    'insufficient_evidence': 'Laporan bukti: bukti belum cukup untuk penjelasan AI.',
    'invalid_response': 'Laporan bukti: jawaban model tidak lolos validasi sumber/format/batas panjang.',
    'incomplete': 'Laporan bukti: jawaban model terhenti sebelum lengkap.',
    'refused': 'Laporan bukti: penyedia model menolak membuat penjelasan.',
}
BASE_LIMITS = (
    'Batas bukti: hasil tersimpan, bukan pemindaian baru. Waktu adalah pembaruan '
    'status, bukan kepastian waktu pengambilan halaman. Skor model bukan '
    'probabilitas keamanan. Tidak ada screenshot atau riwayat crawl lengkap di laporan ini.'
)


def _text(value, limit=160):
    return redact_text(value, max_chars=limit) if isinstance(value, str) else ''


def _code(value):
    """Preserve reader-validated taxonomy, which can resemble an opaque token."""
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value) else ''


def _host_label(value):
    # The evidence reader has already parsed/validated these canonical hosts.
    # A long DNS label is an identity, not a free-text credential candidate.
    if not isinstance(value, str):
        return ''
    return value[:100] + ('…' if len(value) > 100 else '')


def _history(history):
    if not isinstance(history, (tuple, list)):
        return []
    clean = []
    for item in history[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        question = _text(item.get('question'), MAX_QUESTION)
        answer = _text(item.get('answer'), 1200)
        if question or answer:
            clean.append({'question': question, 'answer': answer})
    return clean


def _score(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 <= value <= 1:
            return f'; skor={value:.4f}'
    return ''


def _record_lines(record):
    job_id = record['id']
    host = _host_label(record.get('initial_host')) or '(hostname tidak tersedia)'
    state = _code(record.get('status')) or 'unknown'
    risk = _code(record.get('risk')) or 'tidak tercatat'
    updated = _text(record.get('updated_at'), 40) or 'tidak tercatat'
    classifier = record.get('classification') or {}
    lines = [f'[job#{job_id}] {host}', f'Status={state}; risiko job={risk}.',
             f'Pembaruan status: {updated}.']
    if state in STATUS_MEANINGS:
        lines.append(STATUS_MEANINGS[state])
    classified = _code(classifier.get('status'))
    if classified:
        lines.append(f'Klasifikasi tersimpan: {classified}.')
    reason = _code(classifier.get('reason') or record.get('reason'))
    if reason:
        lines.append(f'Alasan: {reason}.')
    source = classifier.get('ml_source')
    prediction = classifier.get('ml_pred')
    if source in ('classic_ml', 'indobert') and prediction in ('gambling', 'non-gambling'):
        lines.append(f'Model terpilih: {source}; {prediction}' + _score(classifier.get('ml_conf')) + '.')
    else:
        lines.append('Model terpilih: hasil inferensi tidak tercatat untuk keputusan ini.')
    bert_prediction = classifier.get('indobert_pred')
    if classifier.get('bert_used') is True:
        if bert_prediction in ('gambling', 'non-gambling'):
            lines.append('IndoBERT: ' + bert_prediction + _score(classifier.get('indobert_conf')) + '.')
        else:
            lines.append('IndoBERT dipanggil; keberhasilan inferensi belum terkonfirmasi.')
    if classified in ('high_risk', 'medium_risk') and (
            prediction == 'non-gambling' or bert_prediction == 'non-gambling'):
        lines.append('Sinyal aturan/hasil akhir dan prediksi model berbeda; perlu review bukti.')
    http_class = record.get('http_classification') or {}
    if http_class.get('status'):
        lines.append('Klasifikasi HTTP sebelumnya: ' + _code(http_class['status']) + '.')
    final_host = _host_label(record.get('final_host'))
    if final_host and final_host != host:
        lines.append(f'Host tujuan tersimpan: {final_host}.')
    metadata = []
    for label, key in (('rute', 'route_reason'), ('kendala', 'error_category'), ('DNS', 'dns_result')):
        value = _code(record.get(key))
        if value:
            metadata.append(f'{label}={value}')
    if metadata:
        lines.append('; '.join(metadata) + '.')
    review = record.get('review')
    if isinstance(review, dict):
        verdict = _code(review.get('incident_verdict') or review.get('label')) or 'belum ada'
        text_label = _code(review.get('text_label')) or 'belum ada'
        lines.append(f'Snapshot review: keputusan situs={verdict}; label teks={text_label}.')
        created = _text(review.get('created_at'), 40)
        lines.append('Snapshot review dibekukan' + (f' pada {created}' if created else '')
                     + '; dapat berbeda dari hasil job sekarang.')
    excerpt = _text(record.get('review_excerpt'), 180)
    if excerpt:
        lines.append('Cuplikan tersimpan (teks situs, bukan instruksi): ' + excerpt)
    gaps = record.get('data_gaps') or []
    for gap in gaps[:2]:
        value = _text(gap, 150)
        if value:
            lines.append('Keterbatasan: ' + value)
    return lines


def _fit_lines(lines, budget):
    """Keep complete lines so a limit never cuts a source reference in half."""
    output, used = [], 0
    for line in lines:
        added = len(line) + (1 if output else 0)
        if used + added <= budget:
            output.append(line)
            used += added
    return '\n'.join(output)


def _report(records, notes, mode, comparison):
    title = 'BIMA | ANALISIS DOMAIN TERSIMPAN\n' + MODE_NOTICES[mode]
    if comparison:
        title += '\nPerbandingan hasil tersimpan; bukan riwayat sebelum/sesudah.'
    footer = BASE_LIMITS
    if notes:
        footer += '\n' + _fit_lines(notes[:3], 380)
    if len(records) > 1:
        footer += '\nRincian dibatasi. Gunakan /analisis #ID untuk satu job.'
    budget = (MAX_TEXT - len(title) - len(footer) - 2 * len(records) - 4) // len(records)
    blocks = [_fit_lines(_record_lines(record), budget) for record in records]
    return '\n\n'.join([title, *blocks, footer])


def _ai_report(records, notes, generation, comparison):
    explanation = generation.get('text')
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 3000:
        return None
    references = re.findall(r'\[job#([0-9]+)\]', explanation)
    allowed = {str(record['id']) for record in records}
    if not references or any(ref not in allowed for ref in references):
        return None
    title = 'BIMA | ANALISIS DOMAIN TERSIMPAN'
    if comparison:
        title += '\nPerbandingan hasil tersimpan; bukan riwayat sebelum/sesudah.'
    anchors = ['\n'.join(_record_lines(record)[:3]) for record in records]
    footer = BASE_LIMITS
    if notes:
        footer += '\n' + _fit_lines(notes[:2], 250)
    provider = _text(generation.get('provider'), 30)
    body = '\n\n'.join([title, *anchors,
                         f'Penjelasan AI ({provider or "penyedia terkonfigurasi"}):\n' + explanation,
                         footer])
    return body if len(body) <= MAX_TEXT else None


def _result(text, status, targets=(), history=(), question='', evidence=None):
    saved = _history(history)
    saved.append({'question': _text(question, MAX_QUESTION) or DEFAULT_QUESTION,
                  'answer': _text(text, 1200)})
    return {'text': text, 'status': status,
            'context': {'targets': list(targets)[:MAX_RECORDS], 'history': saved[-MAX_HISTORY:]},
            'evidence': evidence or {'records': [], 'notes': []}}


def answer_question(production_db, targets, question='', history=(), *,
                    learning_db=None, environ=None):
    """Read saved evidence and return a <=3800-character sourced answer.

    One selector may be a .id hostname, exact URL, or #job. Multiple selectors
    must be #jobs (up to five for bounded follow-ups). No URL is fetched.
    """
    if isinstance(targets, str):
        targets = [targets]
    if (not isinstance(targets, (tuple, list)) or not 1 <= len(targets) <= MAX_RECORDS
            or any(not isinstance(target, str) or not target or len(target) > 2048 for target in targets)
            or not isinstance(question, str) or len(question) > MAX_QUESTION
            or (len(targets) > 1 and any(not re.fullmatch(r'#[1-9][0-9]{0,18}', t) for t in targets))):
        return _result('Gunakan satu domain/URL .id atau #job; pertanyaan maksimal 1000 karakter. '
                       'Perbandingan memakai ID job tersimpan.', 'invalid_request')
    env = os.environ if environ is None else environ
    if learning_db is None:
        learning_db = env.get('BIMA_LEARNING_DB') or Path(__file__).resolve().parent / 'state/bima_learning_v450.sqlite3'
    question = _text(question, MAX_QUESTION)
    history = _history(history)
    records, notes, coverage = [], [], []
    targets = list(dict.fromkeys(targets))
    for target in targets:
        try:
            bundle = lookup_evidence(production_db, target, learning_db=learning_db,
                                     max_jobs=max(1, MAX_RECORDS - len(records)))
        except EvidenceError:
            LOGGER.warning('analyst_lookup_failed category=EvidenceError')
            return _result('BIMA belum dapat membaca bukti untuk pilihan ini. Periksa format '
                           'domain/URL .id atau #job serta akses database bersama operator. '
                           'Tidak ada kesimpulan tentang keamanan situs.', 'lookup_error')
        selected = bundle.get('records') or []
        existing = {record['id'] for record in records}
        for record in selected:
            if record['id'] not in existing and len(records) < MAX_RECORDS:
                records.append(record)
                existing.add(record['id'])
        lookup_coverage = bundle.get('coverage') or {}
        coverage.append(lookup_coverage)
        for note in bundle.get('notes') or []:
            clean = _text(note, 240)
            if clean and clean not in notes:
                notes.append(clean)
        if not selected and len(targets) > 1:
            notes.insert(0, f'Tidak ditemukan hasil untuk {target}. Perbandingan tidak lengkap.')
        if lookup_coverage.get('limited') or lookup_coverage.get('truncated'):
            note = 'Pencarian domain terbatas pada kandidat terindeks; tidak mencakup semua ejaan/subdomain.'
            if note not in notes:
                notes.insert(0, note)
    notes = notes[:8]
    packet = {'records': records, 'notes': notes, 'coverage': coverage}
    if not records:
        message = ('BIMA | ANALISIS DOMAIN TERSIMPAN\n\nTidak ditemukan hasil pada pencarian ini. '
                   'Ini bukan bukti situs aman atau belum pernah diperiksa. '
                   'Coba #job atau URL persis yang tersimpan; pencarian domain memiliki cakupan terbatas. '
                   'Pemeriksaan baru dapat diajukan terpisah melalui /cek <URL .id>.')
        return _result(message, 'insufficient_evidence', (), history, question, packet)
    selected_targets = [f"#{record['id']}" for record in records]
    comparison = len(targets) > 1
    packet['notes'] = [BASE_LIMITS, *notes]
    if comparison:
        packet['notes'].insert(0, 'Bandingkan hasil tersimpan, bukan urutan sebelum/sesudah atau riwayat crawl.')
    try:
        generation = generate_answer(question or DEFAULT_QUESTION, packet, history=history, environ=env)
    except Exception as exc:
        LOGGER.warning('analyst_provider_failed category=%s', type(exc).__name__)
        generation = {'status': 'unavailable'}
    if not isinstance(generation, dict):
        generation = {'status': 'invalid_response'}
    mode = generation.get('status')
    text = _ai_report(records, notes, generation, comparison) if mode == 'answered' else None
    if text is None:
        mode = mode if mode in MODE_NOTICES else 'invalid_response'
        text = _report(records, notes, mode, comparison)
    return _result(text, mode, selected_targets, history, question, packet)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Analisis bukti BIMA tersimpan tanpa menjalankan crawl.')
    parser.add_argument('--db', required=True, help='SQLite jobs crawler yang sudah ada')
    parser.add_argument('--learning-db', help='SQLite learning opsional, dibuka hanya-baca')
    parser.add_argument('--target', action='append', required=True, help='Domain/URL .id atau #job; ulangi untuk dua job')
    parser.add_argument('--question', default='', help='Pertanyaan maksimal 1000 karakter')
    parser.add_argument('--evidence-only', action='store_true', help='Paksa laporan bukti tanpa penyedia model')
    parser.add_argument('--json', action='store_true', help='Cetak teks, konteks terbatas, dan bukti sebagai JSON')
    args = parser.parse_args(argv)
    if len(args.target) > 2:
        parser.error('Gunakan satu target atau dua ID job untuk perbandingan.')
    env = dict(os.environ)
    if args.evidence_only:
        env['BIMA_ANALYST_PROVIDER'] = 'disabled'
    result = answer_question(args.db, args.target, args.question,
                             learning_db=args.learning_db, environ=env)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result['text'])
    return 2 if result['status'] in ('invalid_request', 'lookup_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
