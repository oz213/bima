"""BIMA Continuous Learning v2: persistent review provenance across PDNS cycles.

No model is ever installed into the live crawler. A Pre-Alert reviewer verdict
is a SITE label only; a distinct human-approved evidence TEXT label is required
before any material contributes to training/evaluation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from . import candidate, core

V2_SCHEMA = '''
CREATE TABLE IF NOT EXISTS learning_observations_v2 (
  source_uid TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('prealert','manual')),
  source_job_id INTEGER NOT NULL,
  source_case_id INTEGER,
  hostname TEXT NOT NULL,
  domain_group TEXT NOT NULL,
  site_label TEXT NOT NULL CHECK(site_label IN ('true_positive','false_positive')),
  text_label TEXT CHECK(text_label IS NULL OR text_label IN
    ('gambling','non_gambling','not_for_training')),
  evidence_text TEXT,
  evidence_sha256 TEXT,
  evidence_origin TEXT,
  reviewer TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  text_reviewer TEXT,
  text_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learning_v2_labels
  ON learning_observations_v2(text_label,cycle_id);
CREATE TABLE IF NOT EXISTS learning_events_v2 (
  id INTEGER PRIMARY KEY,
  source_uid TEXT NOT NULL REFERENCES learning_observations_v2(source_uid),
  event_type TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL
);
'''


def _connect(learning_db):
    # Never write to crawler DB: core's connection enforces a separate path.
    return core.learning_connection(learning_db)


def _init(conn):
    conn.executescript(V2_SCHEMA)


@contextmanager
def _ro_production(production_db, learning_db):
    if core._same_file_or_path(production_db, learning_db):
        raise ValueError('Learning DB must differ from crawler production DB')
    path = Path(production_db).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only=ON')
        yield conn
    finally:
        conn.close()


def _uid(kind, *parts):
    return hashlib.sha256(('|'.join([kind] + [str(p) for p in parts])).encode()).hexdigest()


def _cycle_for_review(conn, reviewed_at):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='registry_snapshot_active_v2'").fetchone():
        return 'legacy-before-snapshots'
    row = conn.execute(
        'SELECT r.cycle_id,r.created_at FROM registry_snapshot_active_v2 a '
        'JOIN registry_snapshot_runs_v2 r ON r.cycle_id=a.cycle_id '
        'WHERE a.singleton=1').fetchone()
    if row and reviewed_at and reviewed_at >= row[1]:
        return row[0]
    return 'legacy-before-snapshots'


def _hostname(url):
    host = core.hostname(url)
    if not host or not host.endswith('.id'):
        raise ValueError('Invalid .id evidence hostname')
    return host


def sync_prealerts(production_db, learning_db):
    """Import *only explicit* human SITE verdicts; never infer text labels."""
    imported = skipped = 0
    with _ro_production(production_db, learning_db) as source:
        if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='bima_prealerts'").fetchone():
            return {'imported': 0, 'already_present': 0, 'message': 'no prealert table'}
        rows = source.execute(
            "SELECT id,job_id,url,feedback_label,reviewed_at "
            "FROM bima_prealerts WHERE feedback_label IN ('gambling','non_gambling') "
            "AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "ORDER BY id").fetchall()
        with _connect(learning_db) as dest:
            _init(dest)
            with dest:
                for case_id, job_id, url, feedback, reviewed in rows:
                    host = _hostname(url)
                    site_label = ('true_positive' if feedback == 'gambling'
                                  else 'false_positive')
                    cycle = _cycle_for_review(source, reviewed)
                    uid = _uid('prealert', Path(production_db).resolve(), case_id, job_id)
                    existing = dest.execute(
                        'SELECT site_label FROM learning_observations_v2 WHERE source_uid=?',
                        (uid,)).fetchone()
                    if existing:
                        if existing[0] != site_label:
                            raise ValueError('Conflicting human decisions for Pre-Alert #%s; manual adjudication required' % case_id)
                        skipped += 1
                        continue
                    now = core.utcnow()
                    dest.execute('INSERT INTO learning_observations_v2 '
                                 '(source_uid,cycle_id,source_kind,source_job_id,source_case_id,'
                                 'hostname,domain_group,site_label,reviewer,reviewed_at,created_at,updated_at) '
                                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (uid, cycle, 'prealert', job_id, case_id, host, host,
                                  site_label, 'telegram-human-review', reviewed, now, now))
                    dest.execute('INSERT INTO learning_events_v2 '
                                 '(source_uid,event_type,reviewer,created_at,note) '
                                 'VALUES(?,?,?,?,?)',
                                 (uid, 'site_verdict_imported', 'system', now,
                                  'Human site verdict only; no text label inferred'))
                    imported += 1
    return {'imported': imported, 'already_present': skipped,
            'text_training_approved': 0}


def add_manual(production_db, learning_db, cycle_id, job_id, site_label,
               evidence_text, text_label, reviewer, note):
    """Explicit analyst-reviewed new observation; safe across reused crawl jobs."""
    if site_label not in ('true_positive', 'false_positive'):
        raise ValueError('Invalid site label')
    if text_label not in ('gambling', 'non_gambling', 'not_for_training'):
        raise ValueError('Explicit text label required')
    if not (reviewer or '').strip() or len((note or '').strip()) < 5:
        raise ValueError('Reviewer and reason (>=5 characters) required')
    text = (evidence_text or '').strip()
    if not 40 <= len(text) <= core.MAX_TEXT_CHARS:
        raise ValueError('Evidence must be 40..120000 characters')
    with _ro_production(production_db, learning_db) as source:
        row = source.execute('SELECT url FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Source job not found')
        host = _hostname(row[0])
        if cycle_id != 'legacy-before-snapshots':
            active = _cycle_for_review(source, core.utcnow())
            if active != cycle_id:
                raise ValueError('Cycle does not match active Registry snapshot')
            member = source.execute(
                'SELECT 1 FROM registry_snapshot_queue_v2 WHERE job_id=?',
                (job_id,)).fetchone()
            if not member:
                raise ValueError('Source job is outside the active snapshot')
    digest = core.sha256_text(text)
    uid = _uid('manual', cycle_id, job_id, digest)
    with _connect(learning_db) as dest:
        _init(dest)
        with dest:
            existing = dest.execute('SELECT site_label,text_label,evidence_sha256 '
                                    'FROM learning_observations_v2 WHERE source_uid=?',
                                    (uid,)).fetchone()
            if existing:
                if existing != (site_label, text_label, digest):
                    raise ValueError('Conflicting review for identical source evidence')
                return {'status': 'already_present', 'source_uid': uid}
            now = core.utcnow()
            dest.execute('INSERT INTO learning_observations_v2 '
                         '(source_uid,cycle_id,source_kind,source_job_id,hostname,domain_group,'
                         'site_label,text_label,evidence_text,evidence_sha256,evidence_origin,'
                         'reviewer,reviewed_at,text_reviewer,text_note,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (uid, cycle_id, 'manual', job_id, host, host, site_label,
                          text_label, text, digest, 'manual_capture', reviewer.strip(), now,
                          reviewer.strip(), note.strip(), now, now))
            dest.execute('INSERT INTO learning_events_v2 '
                         '(source_uid,event_type,reviewer,created_at,note) VALUES(?,?,?,?,?)',
                         (uid, 'full_review', reviewer.strip(), now, note.strip()))
    return {'status': 'reviewed', 'source_uid': uid, 'cycle_id': cycle_id}


def attach_text(learning_db, source_uid, evidence_text, reviewer):
    text = (evidence_text or '').strip()
    if not 40 <= len(text) <= core.MAX_TEXT_CHARS:
        raise ValueError('Evidence must contain 40..120000 characters')
    if not (reviewer or '').strip():
        raise ValueError('Reviewer required')
    digest = core.sha256_text(text)
    with _connect(learning_db) as conn:
        _init(conn)
        with conn:
            case = conn.execute('SELECT evidence_sha256,text_label FROM learning_observations_v2 '
                                'WHERE source_uid=?', (source_uid,)).fetchone()
            if not case:
                raise ValueError('Source UID unknown')
            if case[0]:
                if case[0] == digest:
                    return {'status': 'already_attached', 'source_uid': source_uid}
                raise ValueError('Evidence immutable; use a new reviewed observation')
            now = core.utcnow()
            conn.execute('UPDATE learning_observations_v2 SET evidence_text=?,evidence_sha256=?, '
                         'evidence_origin=?,updated_at=? WHERE source_uid=?',
                         (text, digest, 'manual_capture', now, source_uid))
            conn.execute('INSERT INTO learning_events_v2 '
                         '(source_uid,event_type,reviewer,created_at,note) VALUES(?,?,?,?,?)',
                         (source_uid, 'evidence_attached', reviewer.strip(), now,
                          'Evidence attached; still not authorized for text training'))
    return {'status': 'attached', 'source_uid': source_uid, 'sha256': digest}


def label_text(learning_db, source_uid, text_label, reviewer, note):
    if text_label not in ('gambling', 'non_gambling', 'not_for_training'):
        raise ValueError('Invalid explicit text label')
    if not (reviewer or '').strip() or len((note or '').strip()) < 5:
        raise ValueError('Reviewer and reason required')
    with _connect(learning_db) as conn:
        _init(conn)
        with conn:
            old = conn.execute('SELECT evidence_sha256,text_label '
                               'FROM learning_observations_v2 WHERE source_uid=?',
                               (source_uid,)).fetchone()
            if not old or not old[0]:
                raise ValueError('Verified text evidence must be attached first')
            if old[1]:
                if old[1] == text_label:
                    return {'status': 'already_labeled', 'source_uid': source_uid}
                raise ValueError('Text label immutable; adjudicate via a new observation')
            now = core.utcnow()
            conn.execute('UPDATE learning_observations_v2 SET text_label=?,text_reviewer=?, '
                         'text_note=?,updated_at=? WHERE source_uid=?',
                         (text_label, reviewer.strip(), note.strip(), now, source_uid))
            conn.execute('INSERT INTO learning_events_v2 '
                         '(source_uid,event_type,reviewer,created_at,note) VALUES(?,?,?,?,?)',
                         (source_uid, 'text_labeled', reviewer.strip(), now, note.strip()))
    return {'status': 'text_labeled', 'source_uid': source_uid, 'text_label': text_label}


def stats(learning_db, limit=20):
    with _connect(learning_db) as conn:
        _init(conn)
        rows = conn.execute('SELECT source_uid,cycle_id,source_kind,source_case_id,hostname,'
                            'site_label,COALESCE(text_label,\'unlabeled\') '
                            'FROM learning_observations_v2 ORDER BY created_at DESC LIMIT ?',
                            (limit,)).fetchall()
        counts = dict(conn.execute('SELECT COALESCE(text_label,\'unlabeled\'),COUNT(*) '
                                   'FROM learning_observations_v2 GROUP BY text_label'))
        v1 = dict(conn.execute('SELECT COALESCE(text_label,\'unlabeled\'),COUNT(*) '
                               'FROM review_cases GROUP BY text_label'))
    return {'v1_text_labels': v1, 'v2_text_labels': counts,
            'v2_cases': [{'source_uid': r[0], 'cycle': r[1], 'kind': r[2],
                          'prealert_id': r[3], 'hostname': r[4], 'site': r[5],
                          'text': r[6]} for r in rows]}


def _verified_rows(conn):
    rows = []
    for id_, host, group, label, text, digest in conn.execute(
        'SELECT c.id,c.hostname,c.domain_group,c.text_label,e.text_value,e.text_sha256 '
        'FROM review_cases c JOIN case_evidence e ON e.case_id=c.id '
        "WHERE c.label IN ('true_positive','false_positive') "
        "AND c.text_label IN ('gambling','non_gambling') AND c.reviewer IS NOT NULL"):
        rows.append((str(id_), host, group, label, text, digest))
    for uid, host, group, label, text, digest in conn.execute(
        'SELECT source_uid,hostname,domain_group,text_label,evidence_text,evidence_sha256 '
        "FROM learning_observations_v2 WHERE text_label IN ('gambling','non_gambling') "
        'AND evidence_text IS NOT NULL AND text_reviewer IS NOT NULL'):
        rows.append(('v2:' + uid, host, group, label, text, digest))
    seen = {}
    unique = []
    for case_id, host, group, label, text, digest in sorted(rows, key=lambda x: x[0]):
        if not host.endswith('.id') or not group or not group.endswith('.id') or core.sha256_text(text) != digest:
            raise ValueError('Evidence hash/domain mismatch for case ' + case_id)
        prev = seen.get(digest)
        if prev:
            if prev != (label, group):
                raise ValueError('Identical evidence has conflicting class/domain-group labels')
            continue  # prevent duplicate evidence inflating training metrics
        seen[digest] = (label, group)
        unique.append({'case_id': case_id, 'group': group, 'text': text,
                       'label': 1 if label == 'gambling' else 0, 'sha256': digest})
    return unique


def learn(learning_db, runs, *, min_total=100, min_per_class=25, min_groups=20,
          seed='bima-v2-2026'):
    """Offline candidate ONLY. No activation, no model weights overwrite."""
    runs = Path(runs)
    if runs.is_symlink():
        raise ValueError('Model runs path must not be a symlink')
    old_umask = os.umask(0o077)
    try:
        runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(old_umask)
    with (runs / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with _connect(learning_db) as conn:
            _init(conn)
            rows = _verified_rows(conn)
        counts = Counter(r['label'] for r in rows)
        if (len(rows) < min_total or min(counts.get(0, 0), counts.get(1, 0)) < min_per_class
                or len({r['group'] for r in rows}) < min_groups):
            return {'status': 'waiting_for_labels', 'unique_verified': len(rows),
                    'non_gambling': counts.get(0, 0), 'gambling': counts.get(1, 0),
                    'distinct_groups': len({r['group'] for r in rows}),
                    'minimum': {'total': min_total, 'per_class': min_per_class,
                                'groups': min_groups}}
        splits = {'train': [], 'validation': [], 'test': []}
        for row in rows:
            bucket = int(core.sha256_text(seed + '|' + row['group'])[:12], 16) % 100
            key = 'train' if bucket < 80 else 'validation' if bucket < 90 else 'test'
            splits[key].append(row)
        if any(set(r['label'] for r in part) != {0, 1} for part in splits.values()):
            return {'status': 'waiting_for_labels', 'reason': 'Each split needs both classes',
                    'unique_verified': len(rows)}
        fingerprint = hashlib.sha256(json.dumps(
            [(r['case_id'], r['sha256'], r['label'], r['group']) for r in rows],
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        run_id = hashlib.sha256(('continuous-v2|' + candidate.VERSION + '|' + seed + '|'
                                 + fingerprint).encode()).hexdigest()
        output = runs / run_id
        if output.exists():
            return {'status': 'up_to_date', 'run': str(output)}
        model = candidate.fit(splits['train'])
        report = {'version': 'bima-continuous-learning-v2',
                  'algorithm': candidate.VERSION, 'training_started': True,
                  'production_activated': False, 'fingerprint': fingerprint,
                  'split': {k: {'count': len(v), 'groups': len({r['group'] for r in v}),
                                'labels': dict(Counter(r['label'] for r in v))}
                            for k, v in splits.items()},
                  'validation': candidate.evaluate(model, splits['validation']),
                  'test': candidate.evaluate(model, splits['test'])}
        with TemporaryDirectory(dir=runs) as temporary:
            for name, payload in (('model.json', model), ('report.json', report)):
                target = Path(temporary) / name
                target.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding='utf-8')
                os.chmod(target, 0o600)
            Path(temporary).rename(output)
        return {'status': 'candidate_ready', 'run': str(output), **report}
