"""Opt-in, private-reviewer Telegram front end for BIMA HTTP Investigation v1.

Exactly one bounded subprocess at a time. No crawler mutations, no model loading,
no browser, no new alerts. The Telegram poller never performs network crawling.
"""
from __future__ import annotations

from collections import deque
from contextlib import closing
from pathlib import Path
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from investigation_agent_v1 import InvestigationError, _validate_url
from telegram_review import authorized

LOGGER = logging.getLogger('pandi.telegram.investigation')
USAGE = ('Gunakan /investigasi domain.id, /investigasi https://domain.id/path '
         'atau /investigasi #12345. Hanya di chat pribadi reviewer.')
DENIED = 'Investigasi hanya tersedia di chat pribadi reviewer terdaftar.'
BUSY = 'Investigasi lain masih berjalan atau menunggu pengiriman. Coba lagi nanti.'
ACCEPTED = 'Investigasi HTTP diterima. Hasil akan dikirim ke chat pribadi ini.'
_JOB = re.compile(r'#([1-9][0-9]{0,17})\Z')


def _private(chat, sender, allowed, chat_type):
    return (chat_type == 'private' and isinstance(chat, str)
            and isinstance(sender, str) and authorized(chat, sender, allowed))


def _read_job_target(db_path, job_id):
    source = Path(db_path).resolve()
    if not source.is_file():
        raise InvestigationError('db_not_found')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute('SELECT url FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise InvestigationError('job_not_found')
    return _validate_url(row[0], initial=True)


def _parse_target(command, db_path):
    if not isinstance(command, str) or len(command) > 2200:
        return None
    args = command.split()
    if len(args) != 2 or args[0].split('@')[0].lower() != '/investigasi':
        return None
    match = _JOB.fullmatch(args[1])
    if match:
        job_id = int(match[1])
        return _read_job_target(db_path, job_id), job_id
    return _validate_url(args[1], initial=True), None


def _one_line(value, limit=160):
    if value is None:
        return '-'
    text = ' '.join(str(value).split())
    return ''.join(ch if ch.isprintable() else '?' for ch in text)[:limit] or '-'


def _plain_url(value):
    """Presentation only: do not display URL query/fragment or userinfo."""
    if not isinstance(value, str):
        return '-'
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return '-'
        return _one_line(parts.hostname + (parts.path or '/'), 170)
    except ValueError:
        return '-'


def format_report(report):
    """Only summarize bounded structured fields. Never echo raw scraped HTML."""
    if not isinstance(report, dict) or not isinstance(report.get('observation'), dict):
        raise ValueError('invalid_report')
    observation = report['observation']
    job = report.get('saved_job') if isinstance(report.get('saved_job'), dict) else {}
    assessment = observation.get('assessment')
    category = observation.get('category')
    reason = observation.get('reason')
    if assessment not in ('strong_indicator_review_required', 'insufficient_evidence', 'inconclusive'):
        raise ValueError('invalid_assessment')
    if not isinstance(category, str) or not isinstance(reason, str):
        raise ValueError('invalid_fields')
    lines = [
        'BIMA | INVESTIGASI HTTP (ON-DEMAND)',
        'Target: ' + _plain_url(report.get('target')),
        'Tujuan: ' + _plain_url(observation.get('final_url')),
        'HTTP: ' + _one_line(observation.get('http_status'), 8),
        'Kategori: ' + _one_line(category, 64),
        'Penilaian: ' + _one_line(assessment, 64),
        'Alasan: ' + _one_line(reason, 96),
        'Tautan judi tersembunyi: ' + _one_line(observation.get('hidden_gambling_links_count'), 8),
        'Tautan promosi terlihat: ' + _one_line(observation.get('outbound_promotion_links_count'), 8),
    ]
    chain = observation.get('redirect_chain')
    if isinstance(chain, list) and chain:
        lines.append('Redirect: ' + ' -> '.join(_plain_url(u) for u in chain[:5]))
    links = observation.get('hidden_gambling_links_sample')
    if isinstance(links, list) and links:
        lines.append('Contoh tautan tersembunyi:')
        for item in links[:3]:
            if isinstance(item, dict):
                lines.append('- ' + _one_line(item.get('anchor_text'), 45)
                             + ' -> ' + _one_line(item.get('destination_host'), 75)
                             + ' (' + _one_line(item.get('hidden_method'), 35) + ')')
    if job:
        lines.append('Job tersimpan: #' + _one_line(job.get('job_id'), 20)
                     + '; risiko=' + _one_line(job.get('risk'), 40))
        if job.get('model_disagreement') is True:
            lines.append('PERBEDAAN MODEL: Classic ML=' + _one_line(job.get('ml_pred'), 28)
                         + ', IndoBERT=' + _one_line(job.get('indobert_pred'), 28))
        lines.append('Catatan: bukti job dan HTTP saat ini dapat berasal dari waktu berbeda.')
    lines.extend([
        '',
        'Hasil ini memerlukan review analis; bukan keputusan pelanggaran atau perintah takedown.',
        'HTTP-only; tidak menjalankan JS/Selenium, tidak mengirim alert, tidak mengubah DB.',
        'Tidak ditemukannya indikator bukan bukti website aman.',
    ])
    return '\n'.join(lines)[:3400]


class InvestigationService:
    def __init__(self, db_path, *, runner=None, max_seconds=30):
        if not 5 <= max_seconds <= 45:
            raise ValueError('invalid_investigation_timeout')
        self.db_path = db_path
        self.runner = runner if runner is not None else _run_cli
        self.max_seconds = max_seconds
        self._lock = threading.Lock()
        self._busy = False
        self._pending = None
        self._ready = deque()
        self._closed = False

    def handle(self, text, chat, sender, allowed, *, chat_type=None):
        if not _private(chat, sender, allowed, chat_type):
            return DENIED
        try:
            parsed = _parse_target(text, self.db_path)
        except (InvestigationError, sqlite3.Error, OSError, ValueError):
            return 'Target tidak valid atau job tidak ditemukan. ' + USAGE
        if parsed is None:
            return USAGE
        target, job_id = parsed
        with self._lock:
            if self._closed:
                return 'Layanan investigasi tidak tersedia.'
            if self._busy or self._ready:
                return BUSY
            self._busy = True
            self._pending = (chat, sender, target, job_id)
        thread = threading.Thread(target=self._worker, daemon=True,
                                  name='bima-investigation-telegram')
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._busy = False
                self._pending = None
            return 'Investigator belum dapat memulai pemeriksaan.'
        return ACCEPTED

    def _worker(self):
        with self._lock:
            item = self._pending
        if not item:
            return
        chat, sender, target, job_id = item
        try:
            report = self.runner(target, self.db_path, job_id, self.max_seconds)
            answer = format_report(report)
        except subprocess.TimeoutExpired:
            answer = ('BIMA | INVESTIGASI HTTP\nPemeriksaan melewati batas waktu. '
                      'Status inconclusive; bukan bukti domain aman.')
        except Exception as exc:
            LOGGER.warning('Investigation worker unavailable category=%s', type(exc).__name__)
            answer = ('BIMA | INVESTIGASI HTTP\nPemeriksaan belum dapat diselesaikan. '
                      'Status inconclusive; tidak ada keputusan abuse baru.')
        with self._lock:
            if not self._closed:
                self._ready.append((chat, sender, answer))
            self._busy = False
            self._pending = None

    def deliver(self, sender_fn, allowed):
        # Called by the regular Telegram polling thread; delivery errors propagate
        # so that a subsequent poll can retry without losing the result.
        with self._lock:
            if not self._ready:
                return 0
            item = self._ready[0]
        chat, sender, answer = item
        if authorized(chat, sender, allowed):
            sender_fn(chat, answer)
        with self._lock:
            if self._ready and self._ready[0] == item:
                self._ready.popleft()
        return 1

    def close(self):
        with self._lock:
            self._closed = True
            self._ready.clear()


def _run_cli(target, db_path, job_id, max_seconds):
    """Run the existing safe .id-only HTTP engine in a time-bounded subprocess."""
    script = Path(__file__).with_name('investigation_agent_v1.py')
    argv = [sys.executable, '-B', str(script), '--target', target]
    if job_id is not None:
        argv += ['--db', db_path, '--job-id', str(job_id)]
    nice = Path('/usr/bin/nice')
    if nice.exists():
        argv = [str(nice), '-n', '15', *argv]
    # Do not pass Telegram token, API keys or arbitrary service secrets to child.
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HOME': str(Path(__file__).resolve().parents[1])}
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=max_seconds, check=False,
                            env=env, start_new_session=True)
    if result.returncode or len(result.stdout) > 100_000:
        raise RuntimeError('investigator_execution_failed')
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise ValueError('invalid_json_report')
    return report
