"""Read-only discovery of review candidates and isolated human feedback storage.

No crawling, network requests, automatic labeling, or model loading occurs here.
Compatible with Python 3.9+ and the BIMA v4.4.1 jobs schema.
"""

import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


LABELS = ("true_positive", "false_positive", "needs_review")
FINAL_LABELS = ("true_positive", "false_positive")
TEXT_LABELS = ("gambling", "non_gambling", "not_for_training")
KINDS = ("model_rule_disagreement", "http_browser_escalation")
MAX_SCAN = 50000
MAX_TEXT_CHARS = 120000

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cases (
    id INTEGER PRIMARY KEY,
    source_job_id INTEGER NOT NULL UNIQUE,
    hostname TEXT NOT NULL,
    final_hostname TEXT,
    domain_group TEXT NOT NULL,
    source_updated_at TEXT,
    source_risk TEXT,
    classifier_status TEXT NOT NULL,
    classifier_reason TEXT,
    route_reason TEXT,
    candidate_kind TEXT NOT NULL,
    model_metadata_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    label TEXT CHECK (label IS NULL OR label IN
         ('true_positive', 'false_positive', 'needs_review')),
    text_label TEXT CHECK (text_label IS NULL OR text_label IN
         ('gambling', 'non_gambling', 'not_for_training')),
    reviewer TEXT,
    review_note TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_cases_queue
    ON review_cases(label, candidate_kind, id);
CREATE TABLE IF NOT EXISTS case_evidence (
    case_id INTEGER PRIMARY KEY REFERENCES review_cases(id) ON DELETE RESTRICT,
    text_value TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    origin TEXT NOT NULL,
    attached_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES review_cases(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    old_label TEXT,
    new_label TEXT,
    new_text_label TEXT,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hostname(url):
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except (ValueError, AttributeError):
        return ""


def safe_metadata(data):
    """Persist only known classification fields, not raw page/URL/token content."""
    fields = (
        "status", "reason", "ml_source", "ml_pred", "ml_conf",
        "bert_used", "indobert_pred", "indobert_conf", "bert_ms",
        "strong_hits",
    )
    return {key: data[key] for key in fields if key in data}


def candidate_from_job(row):
    """Return a review suggestion; NEVER assume that an alert is ground truth."""
    (job_id, url, final_url, state, risk, route, reason, updated_at, raw) = row
    if state != "alerted":
        return None
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    final = data
    if final.get("status") not in ("medium_risk", "high_risk"):
        return None
    http = data.get("http_classification")
    if not isinstance(http, dict):
        http = {}

    if (final.get("bert_used") is True
            and final.get("ml_pred") == "non-gambling"
            and final.get("indobert_pred") == "non-gambling"
            and final.get("reason") == "contextual_gambling_phrase_detected"):
        kind = "model_rule_disagreement"
    elif (route == "legacy_selenium_followup"
          and http.get("status") in ("low_risk", "inconclusive")
          and final.get("status") in ("medium_risk", "high_risk")):
        kind = "http_browser_escalation"
    else:
        return None

    host = hostname(url)
    if not host.endswith(".id") or not host:
        return None
    meta = safe_metadata(final)
    if http:
        meta["http_classification"] = safe_metadata(http)
    snapshot = {
        "job_id": job_id,
        "hostname": host,
        "final_hostname": hostname(final_url),
        "updated_at": updated_at,
        "source_risk": risk,
        "route": route,
        "classify": meta,
        "kind": kind,
    }
    return {
        "source_job_id": job_id,
        "hostname": host,
        "final_hostname": hostname(final_url) or None,
        "domain_group": host,
        "source_updated_at": updated_at,
        "source_risk": risk,
        "classifier_status": final["status"],
        "classifier_reason": final.get("reason") or reason,
        "route_reason": route,
        "candidate_kind": kind,
        "model_metadata_json": dumps(meta),
        "snapshot_sha256": sha256_text(dumps(snapshot)),
    }


def _same_file_or_path(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or (a.exists() and b.exists() and os.path.samefile(a, b))


def load_candidates(production_db, limit=5000):
    """Read a bounded, indexed sample without modifying BIMA's DB."""
    if not isinstance(limit, int) or not (1 <= limit <= MAX_SCAN):
        raise ValueError("--limit must be between 1 and %s" % MAX_SCAN)
    source = Path(production_db).expanduser().resolve()
    if not source.is_file():
        raise ValueError("Production SQLite database was not found: %s" % source)
    uri = source.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=1000")
        rows = conn.execute(
            "SELECT id,url,final_url,status,risk,route_reason,reason,"
            "updated_at,classifier_json FROM jobs "
            "WHERE status='alerted' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    candidates = [candidate_from_job(r) for r in rows]
    return [c for c in candidates if c], len(rows)


@contextmanager
def learning_connection(learning_db, production_db=None):
    """Connect ONLY to the separate learning database; never create on source."""
    dest = Path(learning_db).expanduser().absolute()
    if dest.is_symlink():
        raise ValueError("Learning database must not be a symlink")
    if production_db and _same_file_or_path(dest, production_db):
        raise ValueError("Refusing to write to the production crawler database")
    if dest.suffix not in (".db", ".sqlite3"):
        raise ValueError("Learning database must end in .db or .sqlite3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(str(dest), timeout=5)
    finally:
        os.umask(old_umask)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def seed_review_queue(production_db, learning_db, limit=5000):
    """Incremental, idempotent import; existing reviews are never overwritten."""
    if _same_file_or_path(learning_db, production_db):
        raise ValueError("Refusing to write to production database")
    candidates, scanned = load_candidates(production_db, limit=limit)
    with learning_connection(learning_db, production_db) as conn:
        inserted = 0
        at = utcnow()
        with conn:
            for c in candidates:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO review_cases ("
                    "source_job_id,hostname,final_hostname,domain_group,"
                    "source_updated_at,source_risk,classifier_status,"
                    "classifier_reason,route_reason,candidate_kind,"
                    "model_metadata_json,snapshot_sha256,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        c["source_job_id"], c["hostname"], c["final_hostname"],
                        c["domain_group"], c["source_updated_at"],
                        c["source_risk"], c["classifier_status"],
                        c["classifier_reason"], c["route_reason"],
                        c["candidate_kind"], c["model_metadata_json"],
                        c["snapshot_sha256"], at, at,
                    ),
                )
                inserted += cursor.rowcount
    return {"scanned_alerts": scanned, "candidates": len(candidates),
            "new_cases": inserted, "already_present": len(candidates) - inserted}


def list_cases(learning_db, limit=30, include_reviewed=False):
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be 1..200")
    with learning_connection(learning_db) as conn:
        query = ("SELECT id,source_job_id,hostname,final_hostname,domain_group,"
                 "candidate_kind,classifier_status,classifier_reason,label,"
                 "reviewer,source_updated_at FROM review_cases ")
        if not include_reviewed:
            query += "WHERE label IS NULL OR label='needs_review' "
        query += ("ORDER BY CASE candidate_kind "
                  "WHEN 'model_rule_disagreement' THEN 0 ELSE 1 END, "
                  "id DESC LIMIT ?")
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, (limit,))]


def attach_evidence_text(learning_db, case_id, text, origin):
    """One immutable analyst-supplied text sample per case, not a fetched page."""
    if origin not in ("manual_capture", "approved_archive", "screenshot_transcript"):
        raise ValueError("Invalid evidence origin")
    if not isinstance(text, str) or not 40 <= len(text.strip()) <= MAX_TEXT_CHARS:
        raise ValueError("Evidence must be 40..%s nonblank characters" % MAX_TEXT_CHARS)
    text = text.strip()
    digest = sha256_text(text)
    with learning_connection(learning_db) as conn:
        case = conn.execute("SELECT label FROM review_cases WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise ValueError("Case not found")
        if case[0] in FINAL_LABELS:
            raise ValueError("Evidence is locked after final review; create a new case for changed evidence")
        existing = conn.execute(
            "SELECT text_sha256 FROM case_evidence WHERE case_id=?", (case_id,)
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Evidence already attached; immutable to preserve label provenance")
            return {"case_id": case_id, "sha256": digest, "already_attached": True}
        with conn:
            conn.execute(
                "INSERT INTO case_evidence(case_id,text_value,text_sha256,origin,attached_at) "
                "VALUES(?,?,?,?,?)", (case_id, text, digest, origin, utcnow())
            )
    return {"case_id": case_id, "sha256": digest, "already_attached": False}


def review_case(learning_db, case_id, label, reviewer, note, domain_group=None,
                text_label=None):
    """Human incident verdict AND independently labeled text; never infer either."""
    if label not in LABELS:
        raise ValueError("Invalid label")
    if label in FINAL_LABELS and text_label not in TEXT_LABELS:
        raise ValueError("Final review requires explicit --text-label, including not_for_training")
    if label == "needs_review" and text_label is not None:
        raise ValueError("needs_review cannot approve a text label")
    reviewer, note = (reviewer or "").strip(), (note or "").strip()
    if not 2 <= len(reviewer) <= 100 or not 5 <= len(note) <= 2000:
        raise ValueError("Reviewer (2..100) and note (5..2000) required")
    if domain_group is not None:
        domain_group = domain_group.strip().lower().rstrip(".")
        if not domain_group or not domain_group.endswith(".id") or any(
                c.isspace() for c in domain_group):
            raise ValueError("--domain-group must be an explicit .id domain")
    with learning_connection(learning_db) as conn:
        with conn:
            case = conn.execute(
                "SELECT label,domain_group FROM review_cases WHERE id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise ValueError("Case not found")
            if label in FINAL_LABELS and not conn.execute(
                "SELECT 1 FROM case_evidence WHERE case_id=?", (case_id,)
            ).fetchone():
                raise ValueError("Attach verified evidence text before assigning final label")
            if case[0] in FINAL_LABELS:
                raise ValueError("Final review is immutable; use a separate adjudication workflow")
            at = utcnow()
            conn.execute(
                "UPDATE review_cases SET label=?,text_label=?,reviewer=?,review_note=?,"
                "reviewed_at=?,domain_group=?,updated_at=? WHERE id=?",
                (label, text_label, reviewer, note, at, domain_group or case[1], at, case_id),
            )
            conn.execute(
                "INSERT INTO review_events(case_id,event_type,reviewer,old_label,"
                "new_label,new_text_label,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, "human_review", reviewer, case[0], label, text_label, note, at),
            )
    return {"case_id": case_id, "label": label, "text_label": text_label,
            "reviewer": reviewer}


def review_stats(learning_db):
    with learning_connection(learning_db) as conn:
        states = dict(conn.execute(
            "SELECT COALESCE(label,'pending'), COUNT(*) FROM review_cases "
            "GROUP BY COALESCE(label,'pending')"
        ))
        kinds = dict(conn.execute(
            "SELECT candidate_kind, COUNT(*) FROM review_cases GROUP BY candidate_kind"
        ))
        text_labels = dict(conn.execute(
            "SELECT COALESCE(text_label,'not_labeled'),COUNT(*) FROM review_cases "
            "GROUP BY COALESCE(text_label,'not_labeled')"
        ))
        evidence = conn.execute("SELECT COUNT(*) FROM case_evidence").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM review_cases").fetchone()[0]
    return {"cases": total, "labels": states, "text_labels": text_labels,
            "kinds": kinds, "evidence_attached": evidence}


class InsufficientExamples(ValueError):
    """The verified corpus is not yet large or diverse enough."""


def export_dataset(learning_db, output_dir, seed="bima-v450", min_total=100,
                   min_per_class=25, min_groups=20):
    """Export verified text only; stable domain-group split; no model training."""
    out = Path(output_dir).expanduser().absolute()
    src = Path(learning_db).expanduser().resolve()
    if out.is_symlink() or out == src or out.exists():
        raise ValueError("Output directory must be a new path, not an existing file/directory")
    if not seed or len(seed) > 80:
        raise ValueError("Seed must have 1..80 characters")
    if min(min_total, min_per_class, min_groups) < 1:
        raise ValueError("Minimum constraints must all be positive")
    with learning_connection(learning_db) as conn:
        rows = conn.execute(
            "SELECT c.id,c.hostname,c.domain_group,c.text_label,"
            "e.text_value,e.text_sha256,e.origin "
            "FROM review_cases c JOIN case_evidence e ON e.case_id=c.id "
            "WHERE c.label IN ('true_positive','false_positive') "
            "AND c.text_label IN ('gambling','non_gambling') "
            "AND c.reviewer IS NOT NULL "
            "ORDER BY c.id"
        ).fetchall()
    if len(rows) < min_total:
        raise InsufficientExamples("Not enough human-verified text cases: %d < %d" % (len(rows), min_total))

    counts = Counter(r[3] for r in rows)
    if any(counts.get(label, 0) < min_per_class for label in ("gambling", "non_gambling")):
        raise InsufficientExamples("Insufficient verified examples per class: %s" % dict(counts))
    groups = {r[2] for r in rows}
    if len(groups) < min_groups:
        raise InsufficientExamples("Insufficient distinct domain groups: %d < %d" % (len(groups), min_groups))

    # Duplicate text across opposing classes would contaminate the labels.
    seen_texts = {}
    for case_id, host, group, label, text, digest, origin in rows:
        if not group or sha256_text(text) != digest:
            raise ValueError("Invalid group or evidence hash for case %d" % case_id)
        prior = seen_texts.setdefault(digest, (label, group))
        if prior[0] != label:
            raise ValueError("Conflicting human labels for identical evidence")
        if prior[1] != group:
            raise ValueError("Identical evidence across different domain groups; "
                             "merge groups before exporting")

    files = {"train": [], "validation": [], "test": []}
    labels_by_split = {k: Counter() for k in files}
    groups_by_split = {k: set() for k in files}
    for case_id, host, group, label, text, digest, origin in rows:
        bucket = int(sha256_text(seed + "|" + group)[:12], 16) % 100
        part = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        files[part].append({"text": text, "label": 1 if label == "gambling" else 0,
                            "case_id": case_id, "evidence_sha256": digest})
        labels_by_split[part][label] += 1
        groups_by_split[part].add(group)

    if any(not files[part] for part in files):
        raise InsufficientExamples("One or more splits are empty; gather more verified domain groups")
    assert sum(len(v) for v in files.values()) == len(rows)
    assert len(set.union(*groups_by_split.values())) == sum(len(v) for v in groups_by_split.values())

    # Do not create any output until all checks have passed.
    old_umask = os.umask(0o077)
    try:
        out.mkdir(parents=True, mode=0o700)
    finally:
        os.umask(old_umask)
    manifest = {
        "schema_version": "bima-learning-v450",
        "generated_at_utc": utcnow(),
        "seed": seed,
        "label_mapping": {"non_gambling": 0, "gambling": 1},
        "label_source": "explicit analyst text_label (independent of incident verdict)",
        "grouping": "domain_group (hostname unless analyst supplied an explicit group)",
        "training_started": False,
        "splits": {},
    }
    try:
        for part, items in files.items():
            data = "".join(dumps(i) + "\n" for i in items)
            target = out / (part + ".jsonl")
            target.write_text(data, encoding="utf-8")
            os.chmod(target, 0o600)
            manifest["splits"][part] = {
                "cases": len(items),
                "domain_groups": len(groups_by_split[part]),
                "labels": dict(labels_by_split[part]),
                "sha256": sha256_text(data),
            }
        m = out / "manifest.json"
        m.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(m, 0o600)
    except Exception:
        # Never overwrite existing files; best effort cleanup of this fresh export.
        for file in out.iterdir():
            if file.is_file():
                file.unlink()
        out.rmdir()
        raise
    return manifest
