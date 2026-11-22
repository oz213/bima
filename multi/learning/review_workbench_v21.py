"""BIMA learning v2.1: human-review workload and dataset quality, strictly read-only.

This module never copies screenshot bytes, exports evidence text, creates a database,
trains models, or sends BIMA alerts. All selections are metadata-only.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import continuous_v2, core

REQUIRED_LEARNING_TABLES = ('review_cases', 'case_evidence', 'learning_observations_v2')
VALID_FOCUS = ('all', 'needs-evidence', 'needs-text-label', 'needs-site-review')
DEFAULT_SEED = 'bima-v2-2026'


def _file_uri(path):
    original = Path(path).expanduser()
    if original.is_symlink():
        raise ValueError('Refusing symlink database')
    resolved = original.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError('SQLite path must be an existing regular file')
    return resolved.as_uri() + '?mode=ro'


@contextmanager
def open_learning(path):
    """Never create/migrate/modify a learning database in reporting commands."""
    conn = sqlite3.connect(_file_uri(path), uri=True, timeout=10)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA busy_timeout=3000')
        conn.execute('BEGIN')  # stable snapshot; avoids mixing review states
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in REQUIRED_LEARNING_TABLES if name not in existing]
        if missing:
            raise ValueError('Learning v2 schema missing: ' + ', '.join(missing))
        yield conn
    finally:
        conn.close()


def _counts(conn, table, column):
    return dict(conn.execute(
        "SELECT COALESCE(%s,'unlabeled'),COUNT(*) FROM %s GROUP BY %s" %
        (column, table, column)))


def readiness(conn, *, seed=DEFAULT_SEED):
    """Mirror the exact v2 learner's evidence-hash, dedupe and split gates."""
    issues = []
    try:
        # Same gate as continuous_v2.learn(): verifies hashes, groups, collisions
        # and discards identical text within a matching class/domain group.
        rows = continuous_v2._verified_rows(conn)
    except ValueError as error:
        issues.append('Evidence integrity blocked: ' + str(error))
        rows = []
    counts = Counter(row['label'] for row in rows)
    groups = {row['group'] for row in rows}
    splits = {name: Counter() for name in ('train', 'validation', 'test')}
    for row in rows:
        bucket = int(core.sha256_text(seed + '|' + row['group'])[:12], 16) % 100
        part = 'train' if bucket < 80 else 'validation' if bucket < 90 else 'test'
        splits[part][row['label']] += 1
    minimum = {'verified_texts': 100, 'per_class': 25, 'domain_groups': 20}
    deficits = {'verified_texts': max(0, 100-len(rows)),
                'gambling': max(0, 25-counts[1]),
                'non_gambling': max(0, 25-counts[0]),
                'domain_groups': max(0, 20-len(groups))}
    split_balance = all(set(c) == {0, 1} for c in splits.values())
    ready = not issues and not any(deficits.values()) and split_balance
    return {'status': 'candidate_eligible' if ready else 'not_ready',
            'unique_verified_texts': len(rows), 'gambling': counts[1],
            'non_gambling': counts[0], 'distinct_domain_groups': len(groups),
            'minimum': minimum, 'remaining': deficits,
            'splits': {k: {'gambling': v[1], 'non_gambling': v[0]}
                       for k, v in splits.items()},
            'splits_have_both_classes': split_balance,
            'integrity_issues': issues,
            'note': 'Candidate eligibility only. Not approved for production or IndoBERT fine-tuning.'}


def status(learning_db):
    with open_learning(learning_db) as conn:
        v1_site = _counts(conn, 'review_cases', 'label')
        v1_text = _counts(conn, 'review_cases', 'text_label')
        v2_site = _counts(conn, 'learning_observations_v2', 'site_label')
        v2_text = _counts(conn, 'learning_observations_v2', 'text_label')
        v1_evidence = conn.execute('SELECT COUNT(*) FROM case_evidence').fetchone()[0]
        v2_evidence = conn.execute('SELECT COUNT(*) FROM learning_observations_v2 '
                                   'WHERE evidence_sha256 IS NOT NULL').fetchone()[0]
        v2_sources = dict(conn.execute('SELECT source_kind,COUNT(*) '
                                       'FROM learning_observations_v2 GROUP BY source_kind'))
        return {'pipeline': 'BIMA Continuous Learning v2.1',
                'production_model_unchanged': True,
                'v1': {'site_labels': v1_site, 'text_labels': v1_text,
                       'evidence_attached': v1_evidence},
                'v2': {'site_labels': v2_site, 'text_labels': v2_text,
                       'sources': v2_sources, 'evidence_attached': v2_evidence},
                'readiness': readiness(conn)}


def queue(learning_db, *, limit=25, focus='all'):
    """Pick next human actions. Never include raw text or full URL paths."""
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('Review queue limit must be 1..100')
    if focus not in VALID_FOCUS:
        raise ValueError('Unknown queue focus')
    with open_learning(learning_db) as conn:
        pending = []
        v2_rows = conn.execute(
            "SELECT source_uid,source_case_id,source_job_id,hostname,source_kind,cycle_id,"
            "site_label,text_label,evidence_sha256,LENGTH(evidence_text) "
            "FROM learning_observations_v2 WHERE text_label IS NULL "
            "ORDER BY created_at ASC LIMIT 5000").fetchall()
        for uid, case_id, job_id, host, origin, cycle, site_label, text_label, digest, chars in v2_rows:
            has_evidence = bool(digest and chars)
            action = 'label-text' if has_evidence else 'attach-text'
            pending.append({'origin': 'v2', 'source_uid': uid,
                            'prealert_id': case_id if origin == 'prealert' else None,
                            'job_id': job_id, 'hostname': host, 'cycle': cycle,
                            'site_label': site_label, 'has_evidence': has_evidence,
                            'evidence_chars': chars or 0, 'next_action': action,
                            '_order': 0 if has_evidence else 2})
        v1_rows = conn.execute(
            "SELECT c.id,c.source_job_id,c.hostname,c.candidate_kind,c.label,c.text_label,"
            "e.text_sha256,LENGTH(e.text_value) "
            "FROM review_cases c LEFT JOIN case_evidence e ON e.case_id=c.id "
            "WHERE (c.label IS NULL OR c.label='needs_review' "
            "OR (c.label IN ('true_positive','false_positive') AND c.text_label IS NULL)) "
            "ORDER BY c.id DESC LIMIT 5000").fetchall()
        for case_id, job_id, host, kind, site_label, text_label, digest, chars in v1_rows:
            has_evidence = bool(digest and chars)
            if site_label in ('true_positive', 'false_positive'):
                action = 'review-text' if has_evidence else 'attach-evidence'
            else:
                action = 'review-site-and-text' if has_evidence else 'attach-evidence'
            pending.append({'origin': 'v1', 'case_id': case_id,
                            'job_id': job_id, 'hostname': host,
                            'candidate_kind': kind, 'site_label': site_label,
                            'has_evidence': has_evidence,
                            'evidence_chars': chars or 0, 'next_action': action,
                            '_order': 1 if has_evidence else 3})
        pending.sort(key=lambda record: (record['_order'], record['origin'],
                                         record.get('case_id', 0) if record['origin'] == 'v1'
                                         else record['source_uid']))
        focused = []
        for item in pending:
            action = item['next_action']
            if focus == 'needs-evidence' and action not in ('attach-text', 'attach-evidence'):
                continue
            if focus == 'needs-text-label' and action not in ('label-text', 'review-text'):
                continue
            if focus == 'needs-site-review' and action != 'review-site-and-text':
                continue
            focused.append(item)
        items = [{k: v for k, v in item.items() if k != '_order'}
                 for item in focused[:limit]]
        return {'total_matching_loaded': len(focused),
                'review_items': items, 'focus': focus,
                'warning': 'Metadata-only worklist. Every site/text label requires human verification.'}


@contextmanager
def open_production(db):
    conn = sqlite3.connect(_file_uri(db), uri=True, timeout=10)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
        needed = {'id', 'url', 'status', 'risk', 'route_reason', 'updated_at'}
        if not needed.issubset(columns):
            raise ValueError('Crawler jobs schema incompatible with triage sampler')
        yield conn
    finally:
        conn.close()


def sample_production(db, *, per_status=10):
    """Efficient systematic ID-spread triage; never claim statistical randomness."""
    if not isinstance(per_status, int) or not 1 <= per_status <= 100:
        raise ValueError('per-status must be 1..100')
    with open_production(db) as conn:
        max_id = conn.execute('SELECT MAX(id) FROM jobs').fetchone()[0] or 0
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        hint = ' INDEXED BY idx_jobs_status' if 'idx_jobs_status' in indexes else ''
        result = []
        for state in ('complete', 'inconclusive'):
            seen = set()
            for i in range(per_status):
                anchor = max(1, int((i + .5) / per_status * max_id))
                record = conn.execute(
                    'SELECT id,url,status,risk,route_reason,updated_at FROM jobs' + hint +
                    ' WHERE status=? AND id>=? ORDER BY id LIMIT 1', (state, anchor)).fetchone()
                if record is None:
                    record = conn.execute(
                        'SELECT id,url,status,risk,route_reason,updated_at FROM jobs' + hint +
                        ' WHERE status=? ORDER BY id LIMIT 1', (state,)).fetchone()
                if record is None or record[0] in seen:
                    continue
                seen.add(record[0])
                try:
                    host = (urlsplit(record[1] or '').hostname or '').lower().rstrip('.')
                except (ValueError, AttributeError):
                    continue
                if not host.endswith('.id'):
                    continue
                result.append({'job_id': record[0], 'hostname': host,
                               'current_status': record[2], 'model_risk': record[3],
                               'route_reason': record[4], 'updated_at': record[5],
                               'human_label': None})
        return {'sampling': 'deterministic ID-spread triage; NOT representative random sampling',
                'source': 'existing production jobs, read-only',
                'items': result,
                'note': 'Neither complete nor inconclusive means non-gambling; analyst must check evidence.'}
