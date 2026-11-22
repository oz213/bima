"""BIMA dataset acquisition v2.2: read-only internal sources + licensed public CSV.

Produces separate verified / unverified / third-party corpora. Nothing here
trains, writes to crawler SQLite, labels model predictions as truth, fetches
live .id pages, or posts an alert. Intended for offline analyst curation.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Pinned public CSV; no image downloads. The source describes 5,028 annotated
# social-media posts. It is NOT a labeled .id domain/site dataset.
PUBLIC_SOURCE = (
    'https://huggingface.co/datasets/0xRafie/'
    'indonesian-gambling-multimodal-dataset/resolve/'
    '9239598edc7f3d72becf7d7a105fcab6b06a22d6/dataset.csv'
)
PUBLIC_CREDIT = ('Ekaputra, D. R. (2026), Indonesian Online Gambling '
                 'Promotion Multimodal Dataset, Hugging Face; CC BY 4.0')
MAX_PUBLIC_BYTES = 12 * 1024 * 1024
MAX_EVIDENCE_CHARS = 3000
GROUPS = {
    'alerted': 'alert', 'pending_alert': 'alert', 'dry_run_alert': 'alert',
    'complete': 'complete', 'inconclusive': 'uncertain',
    'retry_http': 'uncertain', 'failed': 'uncertain',
}
EMAIL = re.compile(r'(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b')
URL = re.compile(r'(?i)\b(?:https?://|www\.)[^\s<>"\']+')
PHONE = re.compile(r'(?<!\d)(?:\+62|62|0)8(?:[\s.\-]?\d){8,12}(?!\d)')
IPV4 = re.compile(r'(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?!\w)')
TOKEN = re.compile(r'(?i)\b(?:token|password|passwd|api[_-]?key|secret)\s*[:=]\s*[^\s,;]{4,}')
HANDLE = re.compile(r'(?<![\w@])@[A-Za-z0-9_]{3,32}\b')


def sha(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def redact(text):
    """Conservative share-safe masking, NOT a full PII guarantee."""
    text = str(text or '').strip().replace('\x00', ' ')
    for pat, value in ((TOKEN, '[SECRET]'), (EMAIL, '[EMAIL]'),
                       (URL, '[URL]'), (PHONE, '[PHONE]'),
                       (IPV4, '[IP]'), (HANDLE, '[HANDLE]')):
        text = pat.sub(value, text)
    return text[:MAX_EVIDENCE_CHARS]


def _open_ro(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('Source must be a regular SQLite file')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=12)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=2000')
    return conn


def _has_table(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _host(url):
    try:
        return (urlsplit(url or '').hostname or '').lower().rstrip('.')
    except (ValueError, TypeError):
        return ''


def _excerpt(raw):
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    # Only read BIMA's explicit page excerpt, never arbitrary classifier JSON.
    http = data.get('http_classification')
    candidates = [data.get('review_excerpt')]
    if isinstance(http, dict):
        candidates.append(http.get('review_excerpt'))
    excerpt = next((x for x in candidates if isinstance(x, str) and len(x.strip()) >= 40), None)
    meta = {k: data[k] for k in (
        'status', 'reason', 'ml_pred', 'indobert_pred', 'bert_used'
    ) if k in data and isinstance(data[k], (str, bool))}
    return redact(excerpt) if excerpt else None, meta


def _sample_jobs(conn, quotas, seed):
    rng = {g: random.Random(f'{seed}:{g}') for g in quotas}
    chosen = {g: [] for g in quotas}
    totals = Counter()
    statuses = tuple(GROUPS)
    query = ('SELECT id,status FROM jobs WHERE status IN (' +
             ','.join('?' for _ in statuses) + ') ORDER BY status,id')
    for job_id, status in conn.execute(query, statuses):
        group = GROUPS[status]
        totals[group] += 1
        n = quotas[group]
        if not n:
            continue
        if len(chosen[group]) < n:
            chosen[group].append(job_id)
        else:
            idx = rng[group].randrange(totals[group])
            if idx < n:
                chosen[group][idx] = job_id
    return chosen, totals


def _get_jobs(conn, ids):
    ids = sorted(set(ids))
    out = {}
    for i in range(0, len(ids), 250):
        chunk = ids[i:i + 250]
        sql = ('SELECT id,url,final_url,status,risk,route_reason,reason,updated_at,'
               'classifier_json FROM jobs WHERE id IN (' +
               ','.join('?' for _ in chunk) + ')')
        for row in conn.execute(sql, chunk):
            out[row[0]] = row
    return out


def _candidate(row, group, *, site_label=None, human_prealert=None):
    job_id, url, final_url, status, risk, route, reason, updated_at, classifier = row
    text, metadata = _excerpt(classifier)
    host = _host(url)
    return {
        'observation_uid': sha('job|%s|%s|%s' % (job_id, updated_at, status)),
        'job_id': job_id, 'hostname': host,
        'domain_group_sha256': sha(host), 'stratum': group,
        'job_status': status, 'risk': risk, 'route_reason': route,
        'crawl_updated_at': updated_at, 'model_metadata': metadata,
        'evidence_excerpt': text, 'evidence_available': bool(text),
        'site_label': site_label, 'site_label_source': human_prealert,
        'text_label': None, 'text_label_source': None,
        'training_approved': False,
        'note': 'Candidate only; model verdict and site verdict are NOT labels for excerpt text',
    }


def collect_registry(production_db, *, seed=20261122, alert=50,
                     complete=100, uncertain=50):
    quotas = {'alert': alert, 'complete': complete, 'uncertain': uncertain}
    if any(not isinstance(v, int) or v < 0 or v > 1000 for v in quotas.values()):
        raise ValueError('Sampling quota must be 0..1000 per stratum')
    with closing(_open_ro(production_db)) as conn:
        if not _has_table(conn, 'jobs'):
            raise ValueError('Production database has no jobs table')
        conn.execute('BEGIN')
        try:
            sampled, totals = _sample_jobs(conn, quotas, seed)
            selected = {}
            for group, ids in sampled.items():
                for job in ids:
                    selected[job] = (group, None, None)
            reviewer_count = 0
            if _has_table(conn, 'bima_prealerts'):
                for job_id, feedback in conn.execute(
                    'SELECT job_id,feedback_label FROM bima_prealerts '
                    "WHERE feedback_label IN ('gambling','non_gambling') "
                    'AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL'
                ):
                    reviewer_count += 1
                    site = 'true_positive' if feedback == 'gambling' else 'false_positive'
                    selected[job_id] = ('reviewed_prealert', site, 'telegram_human_site')
            records = _get_jobs(conn, list(selected))
            results = [_candidate(records[job_id], group, site_label=label,
                                  human_prealert=source)
                       for job_id, (group, label, source) in sorted(selected.items())
                       if job_id in records]
            cycle = None
            if _has_table(conn, 'registry_snapshot_active_v2'):
                row = conn.execute('SELECT cycle_id FROM registry_snapshot_active_v2 '
                                   'WHERE singleton=1').fetchone()
                cycle = row[0] if row else None
        finally:
            conn.rollback()
    return results, {'sample_eligible_by_stratum': dict(totals),
                     'reviewed_prealert_included': reviewer_count,
                     'active_cycle': cycle,
                     'candidates': len(results),
                     'candidates_with_excerpt': sum(bool(x['evidence_available']) for x in results)}


def collect_verified(learning_db):
    """Only explicit HUMAN text labels + immutable matching evidence hashes."""
    path = Path(learning_db)
    if not path.is_file():
        return [], {'learning_db_present': False, 'verified_conflicts': 0}
    results = []
    with closing(_open_ro(path)) as conn:
        if _has_table(conn, 'review_cases') and _has_table(conn, 'case_evidence'):
            rows = conn.execute(
                'SELECT c.id,c.hostname,c.domain_group,c.text_label,e.text_value,'
                'e.text_sha256 FROM review_cases c JOIN case_evidence e '
                'ON c.id=e.case_id WHERE c.label IN '
                "('true_positive','false_positive') "
                "AND c.text_label IN ('gambling','non_gambling') "
                'AND c.reviewer IS NOT NULL'
            )
            for case_id, host, group, label, text, digest in rows:
                results.append((f'v1:{case_id}', host, group, label, text, digest, 'BIMA_human_v1'))
        if _has_table(conn, 'learning_observations_v2'):
            rows = conn.execute(
                'SELECT source_uid,hostname,domain_group,text_label,evidence_text,'
                'evidence_sha256 FROM learning_observations_v2 '
                "WHERE text_label IN ('gambling','non_gambling') "
                'AND evidence_text IS NOT NULL AND text_reviewer IS NOT NULL'
            )
            for uid, host, group, label, text, digest in rows:
                results.append((f'v2:{uid}', host, group, label, text, digest, 'BIMA_human_v2'))
    dedupe = {}
    conflicts = 0
    rejected = 0
    for source_id, host, group, label, text, digest, provenance in results:
        if not isinstance(text, str) or len(text.strip()) < 40 or sha(text) != digest:
            rejected += 1
            continue
        if not group or not group.endswith('.id') or not host or not host.endswith('.id'):
            rejected += 1
            continue
        original_digest = sha(text)
        if original_digest in dedupe:
            if dedupe[original_digest]['label'] != label:
                conflicts += 1
                dedupe[original_digest]['training_approved'] = False
                dedupe[original_digest]['conflicting_labels'] = True
            continue
        dedupe[original_digest] = {
            'source_uid': source_id, 'provenance': provenance,
            'hostname': host, 'domain_group_sha256': sha(group),
            'text': redact(text), 'original_text_sha256': digest,
            'label': label, 'training_approved': True,
            'label_source': 'explicit_human_verified_text',
        }
    clean = [row for row in dedupe.values() if row['training_approved']]
    return clean, {'learning_db_present': True, 'verified_records': len(results),
                   'verified_deduplicated': len(clean), 'verified_conflicts': conflicts,
                   'verified_rejected': rejected}


def public_csv_from_web():
    """Bounded, pinned, opt-in DOWNLOAD of CSV only; never untrusted pickle/code."""
    req = Request(PUBLIC_SOURCE, headers={'User-Agent': 'BIMA-DatasetCollector/2.2',
                                          'Accept': 'text/csv,text/plain'})
    with urlopen(req, timeout=25) as r:
        data = r.read(MAX_PUBLIC_BYTES + 1)
    if len(data) > MAX_PUBLIC_BYTES:
        raise ValueError('Public CSV exceeded the allowed size limit')
    return data


def collect_external_csv(data):
    """Third-party annotation is auxiliary, never an independently verified BIMA label."""
    if isinstance(data, bytes):
        if len(data) > MAX_PUBLIC_BYTES:
            raise ValueError('Public CSV too large')
        content = data.decode('utf-8-sig')
    else:
        content = str(data)
    reader = csv.DictReader(io.StringIO(content))
    required = {'id', 'text_caption', 'platform', 'label'}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError('Public CSV columns do not match the documented source')
    out = []
    seen = {}
    conflicting = set()
    errors = 0
    for row in reader:
        raw_label = str(row['label']).strip()
        if raw_label not in ('0', '0.0', '1', '1.0'):
            errors += 1
            continue
        raw_text = str(row.get('text_caption') or '').strip()
        if not 40 <= len(raw_text) <= 20000:
            errors += 1
            continue
        label = 'gambling' if float(raw_label) == 1 else 'non_gambling'
        digest = sha(raw_text.casefold())
        if digest in seen:
            if seen[digest] != label:
                # Conflicting duplicate: keep neither; quarantine by hash.
                conflicting.add(digest)
                errors += 1
            continue
        seen[digest] = label
        out.append({
            'source_uid': f'ext:0xrafie:{str(row["id"]).strip()}',
            'dataset': '0xRafie/indonesian-gambling-multimodal-dataset',
            'source_license': 'CC-BY-4.0 (source metadata, verify for intended reuse)',
            'source_platform': str(row['platform'])[:30],
            'label': label, 'text': redact(raw_text),
            'original_text_sha256': sha(raw_text),
            'text_fingerprint_sha256': digest,
            'label_source': 'third_party_author_annotation',
            'training_approved': False,
            'evaluation_approved': False,
            'purpose': 'auxiliary candidate corpus; never mix into BIMA held-out test',
        })
    # Neutralize any contradictory duplicate encountered in any input order.
    if conflicting:
        out = [r for r in out if r['text_fingerprint_sha256'] not in conflicting]
    return out, {'public_rows_accepted': len(out), 'public_rows_skipped': errors,
                 'public_labels': dict(Counter(x['label'] for x in out))}


def _write_new(folder, name, rows):
    target = folder / name
    with target.open('x', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    os.chmod(target, 0o600)
    return target


def publish_pack(out_dir, *, candidates, verified, external, summary):
    out = Path(out_dir).expanduser().absolute()
    if out.is_symlink() or out.exists():
        raise ValueError('Output must be a NEW directory; never overwrite dataset packs')
    # Avoid creating packs under source tree accidentally via caller validation.
    previous = os.umask(0o077)
    try:
        out.mkdir(parents=True, mode=0o700)
    finally:
        os.umask(previous)
    try:
        files = {
            'review_candidates.jsonl': candidates,
            'verified_bima.jsonl': verified,
            'external_auxiliary.jsonl': external,
        }
        for name, rows in files.items():
            _write_new(out, name, rows)
        manifest = {
            'version': 'bima-dataset-collection-v2.2',
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'counts': {'unverified_review_candidates': len(candidates),
                       'verified_bima_examples': len(verified),
                       'external_auxiliary_examples': len(external)},
            'sources': {'public_csv': PUBLIC_SOURCE if external else None,
                        'public_attribution': PUBLIC_CREDIT if external else None},
            'summary': summary,
            'policy': {'crawler_sqlite_modified': False,
                       'learning_sqlite_modified': False,
                       'active_model_modified': False,
                       'automatic_text_labels': False,
                       'unverified_as_ground_truth': False,
                       'external_data_for_bima_holdout': False},
            'privacy_warning': ('Text has limited automatic redaction, not a full '
                                'PII guarantee. Review before sharing.'),
        }
        target = out / 'manifest.json'
        with target.open('x', encoding='utf-8') as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
        os.chmod(target, 0o600)
        return manifest
    except BaseException:
        for file in out.iterdir():
            if file.is_file():
                file.unlink()
        out.rmdir()
        raise
