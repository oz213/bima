"""BIMA dataset v2.3: bounded live HTTP evidence acquisition for an existing pack.

Read-only SQLite, no model/alert/Telegram changes. Live web text is NEVER the
historical text that led to the old site verdict; it needs independent review.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from .dataset_collection_v22 import redact

MAX_ROWS = 10_000
MAX_LINE_BYTES = 100_000
MAX_HTML_BYTES = 512 * 1024
MAX_CAPTURE_CHARS = 1000
GROUPS = ('reviewed_prealert', 'alert', 'complete', 'uncertain')


def utcnow():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def hostname(url):
    try:
        return (urlsplit(url or '').hostname or '').lower().rstrip('.')
    except (ValueError, TypeError):
        return ''


def _read_candidates(pack):
    pack = Path(pack).expanduser().absolute()
    if pack.is_symlink() or not pack.is_dir():
        raise ValueError('Existing dataset pack must be a non-symlink directory')
    manifest_file = pack / 'manifest.json'
    candidates_file = pack / 'review_candidates.jsonl'
    if any(p.is_symlink() or not p.is_file() for p in (manifest_file, candidates_file)):
        raise ValueError('Manifest/candidates must be regular, non-symlink files')
    if manifest_file.stat().st_size > 1_000_000:
        raise ValueError('Dataset manifest too large')
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    if manifest.get('version') != 'bima-dataset-collection-v2.2':
        raise ValueError('Expected a BIMA dataset v2.2 source pack')
    cases = []
    unique_ids = set()
    with candidates_file.open('r', encoding='utf-8') as inp:
        for line in inp:
            if len(cases) >= MAX_ROWS or len(line.encode('utf-8')) > MAX_LINE_BYTES:
                raise ValueError('Dataset input exceeds safety size limit')
            if not line.strip():
                continue
            case = json.loads(line)
            if (not isinstance(case, dict) or not isinstance(case.get('job_id'), int)
                    or case['job_id'] <= 0 or not isinstance(case.get('hostname'), str)
                    or not case.get('observation_uid')):
                raise ValueError('Invalid BIMA review candidate')
            if case['job_id'] in unique_ids:
                raise ValueError('Duplicate job_id in candidate pack')
            unique_ids.add(case['job_id'])
            cases.append(case)
    if not cases:
        raise ValueError('No candidate records in dataset pack')
    return cases, hashlib.sha256(candidates_file.read_bytes()).hexdigest()


def choose_cases(cases, limit, offset=0):
    """Round robin across existing candidate groups, one request per hostname."""
    if not 1 <= limit <= 250 or not 0 <= offset <= MAX_ROWS:
        raise ValueError('Limit must be 1..250 and offset 0..10000')
    grouped = {kind: [] for kind in GROUPS}
    seen_jobs = set()
    for case in cases:
        job_id = case['job_id']
        if job_id in seen_jobs:
            continue
        seen_jobs.add(job_id)
        if case.get('evidence_available'):
            continue
        host = case.get('hostname', '').lower().rstrip('.')
        if not host.endswith('.id'):
            continue
        bucket = case.get('stratum')
        if bucket in grouped:
            grouped[bucket].append(case)
    selected = []
    seen_hosts = set()
    while len(selected) < limit + offset:
        added = False
        for group in GROUPS:
            while grouped[group]:
                case = grouped[group].pop(0)
                host = case['hostname'].lower().rstrip('.')
                if host in seen_hosts:
                    continue
                selected.append(case)
                seen_hosts.add(host)
                added = True
                break
            if len(selected) >= limit + offset:
                break
        if not added:
            break
    return selected[offset:offset + limit]


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._parts = []
        self._chars = 0
        self._title = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'):
            self._hidden += 1
        if tag == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'):
            self._hidden = max(0, self._hidden - 1)
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._hidden:
            return
        if self._in_title:
            self._title.append(data[:200])
        if self._chars >= 20_000:
            return
        fragment = data[:20_000 - self._chars]
        self._parts.append(fragment)
        self._chars += len(fragment)

    def extracted(self):
        return ' '.join(' '.join(self._parts).split()), ' '.join(self._title)[:200]


class _TextOnlyEngine:
    """HTTP extraction adapter; deliberately does not load IndoBERT or classify."""
    @staticmethod
    def extract_text_from_html(html, page_url):
        parser = _PageText()
        parser.feed(html)
        text, title = parser.extracted()
        return text, title, False, [], []


def safe_fetch_evidence(url):
    """Reuse the crawler's redirect-safe HTTP-only fetch, never send alerts."""
    # Lazy import keeps dry-run/test/manifest inspection completely offline.
    from crawler_multi import fetch_http
    options = SimpleNamespace(http_connect_timeout=5, http_read_timeout=9,
                              http_max_bytes=MAX_HTML_BYTES, min_host_interval=1.0)
    result = fetch_http(_TextOnlyEngine(), url, options)
    text = result.get('text') or ''
    if result.get('ok') and len(text.strip()) >= 40:
        excerpt = redact(' '.join(text[:3500].split()))[:MAX_CAPTURE_CHARS]
        if len(excerpt.strip()) >= 40:
            return {'excerpt': excerpt,
                    'final_host': hostname(result.get('final_url')),
                    'http_status': result.get('status_code'),
                    'result': 'evidence_captured'}
    return {'excerpt': None,
            'http_status': result.get('status_code'),
            'result': 'no_usable_text',
            'fetch_reason': redact(result.get('reason') or '')[:80]}


def _open_production_ro(path):
    path = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    conn.execute('PRAGMA query_only=ON')
    return conn


def enrich(pack, production_db, out, *, limit=60, offset=0, fetcher=safe_fetch_evidence):
    """Create immutable private *new* output; no source pack, DB or model writes."""
    cases, source_sha = _read_candidates(pack)
    chosen = choose_cases(cases, limit, offset)
    out = Path(out).expanduser().absolute()
    prod = Path(production_db).expanduser().resolve(strict=True)
    if out.is_symlink() or out.exists() or out == prod or out == pack:
        raise ValueError('Output directory must be new and separate from input/source')
    if prod in out.parents or Path(pack).absolute() in out.parents:
        raise ValueError('Output cannot be inside a database or source pack directory')
    if not out.parent.is_dir():
        raise ValueError('Output parent directory must already exist')
    if not chosen:
        raise ValueError('No unresolved candidates available')
    selected_by_job = {case['job_id']: case for case in chosen}
    original = {case['job_id']: dict(case) for case in cases}
    report = {'attempted': 0, 'captured': 0, 'skipped': 0, 'failed': 0,
              'selected': len(chosen), 'source_cases': len(cases),
              'selection_offset': offset}
    for job in selected_by_job:
        original[job]['evidence_enrichment_attempted'] = False
    # Look up URLs from source production SQLite so the exact URL/path is used;
    # never trust an arbitrary URL in a JSONL pack as a network target.
    with closing(_open_production_ro(prod)) as db:
        for job_id, case in selected_by_job.items():
            row = db.execute('SELECT url FROM jobs WHERE id=?', (job_id,)).fetchone()
            result = original[job_id]
            result['evidence_enrichment_attempted'] = True
            result['evidence_source'] = 'live_http_refetch_v23'
            result['evidence_observed_at'] = utcnow()
            result['historical_evidence_equivalence'] = False
            if not row or hostname(row[0]) != case['hostname'].lower().rstrip('.'):
                result['enrichment_status'] = 'source_mismatch_or_missing'
                report['skipped'] += 1
                continue
            if not hostname(row[0]).endswith('.id'):
                result['enrichment_status'] = 'invalid_registry_hostname'
                report['skipped'] += 1
                continue
            report['attempted'] += 1
            try:
                observation = fetcher(row[0])
                result['enrichment_status'] = observation.get('result', 'unknown')
                result['evidence_excerpt'] = observation.get('excerpt') or None
                result['evidence_available'] = bool(result['evidence_excerpt'])
                result['evidence_http_status'] = observation.get('http_status')
                if observation.get('final_host'):
                    result['evidence_final_host'] = observation['final_host']
                if result['evidence_available']:
                    report['captured'] += 1
                else:
                    report['failed'] += 1
            except Exception as exc:
                result['enrichment_status'] = 'fetch_exception'
                result['enrichment_error_type'] = type(exc).__name__
                report['failed'] += 1
    previous_umask = os.umask(0o077)
    try:
        out.mkdir(mode=0o700)
    finally:
        os.umask(previous_umask)
    try:
        target = out / 'enriched_review_candidates.jsonl'
        with target.open('x', encoding='utf-8') as f:
            for case in cases:
                record = original[case['job_id']]
                # Refetched text is always an unverified observation even if
                # a previous human SITE decision exists for this same hostname.
                record['training_approved'] = False
                record['text_label'] = None
                record['text_label_source'] = None
                f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        os.chmod(target, 0o600)
        report.update({'version': 'bima-evidence-enrichment-v2.3',
                       'source_pack': str(Path(pack).absolute()),
                       'source_candidates_sha256': source_sha,
                       'generated_at_utc': utcnow(),
                       'model_or_alert_modified': False,
                       'automatic_text_labels': False,
                       'warning': 'Live refetch is new evidence, NOT proof of old site-label text'})
        summary = out / 'enrichment_manifest.json'
        with summary.open('x', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.chmod(summary, 0o600)
        return report
    except BaseException:
        for file in out.iterdir():
            if file.is_file():
                file.unlink()
        out.rmdir()
        raise
