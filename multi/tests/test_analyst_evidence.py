"""Read-only analyst evidence tests using real, isolated SQLite databases."""

from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


class AnalystEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.production = self.root / "production.sqlite3"
        self.learning = self.root / "learning.sqlite3"
        with closing(sqlite3.connect(self.production)) as conn:
            conn.executescript("""
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    status TEXT, risk TEXT, reason TEXT, route_reason TEXT,
                    updated_at TEXT, final_url TEXT, http_status INTEGER,
                    attempts INTEGER, error_category TEXT, dns_result TEXT,
                    next_retry_at TEXT, classifier_json TEXT,
                    error TEXT, evidence_path TEXT, alert_confidence REAL
                );
                CREATE INDEX idx_jobs_status ON jobs(status,id);
            """)

    def reader(self):
        # Missing implementation is an ordinary RED assertion, not an import error.
        self.assertIsNotNone(importlib.util.find_spec("analyst_evidence"),
                             "The read-only evidence reader is not implemented")
        return importlib.import_module("analyst_evidence")

    def add_job(self, key, url, **values):
        row = {
            "id": key, "url": url, "status": "complete", "risk": "low_risk",
            "reason": "low_signal", "route_reason": "legacy_selenium_followup",
            "updated_at": "2026-11-21T10:00:00Z", "final_url": url,
            "http_status": 200, "attempts": 1,
            "classifier_json": json.dumps({
                "status": "low_risk", "reason": "low_signal",
                "ml_source": "classic_ml", "ml_pred": "non-gambling", "ml_conf": 0.17,
                "bert_used": True, "indobert_pred": "non-gambling", "indobert_conf": 0.00004,
            }),
        }
        row.update(values)
        if isinstance(row.get("classifier_json"), dict):
            row["classifier_json"] = json.dumps(row["classifier_json"])
        with closing(sqlite3.connect(self.production)) as conn, conn:
            conn.execute("INSERT INTO jobs (%s) VALUES (%s)" %
                         (",".join(row), ",".join("?" for _ in row)), tuple(row.values()))

    def test_job_and_exact_url_keep_query_identity_without_exposing_secrets(self):
        self.add_job(7, "https://example.id/reset/private-path-token?token=private-query-token")
        self.add_job(8, "https://example.id/reset/private-path-token?token=different-query")
        reader = self.reader()
        before = hashlib.sha256(self.production.read_bytes()).digest()
        by_id = reader.lookup_evidence(self.production, "#7")
        by_url = reader.lookup_evidence(
            self.production, "https://example.id/reset/private-path-token?token=private-query-token#local")
        self.assertEqual([r["id"] for r in by_id["records"]], [7])
        self.assertEqual([r["id"] for r in by_url["records"]], [7])
        self.assertEqual(by_id["records"][0]["ref"], "job#7")
        self.assertEqual(by_url["coverage"]["mode"], "exact_url")
        missing = reader.lookup_evidence(self.production, "https://example.id/reset/private-path-token")
        self.assertEqual(missing["records"], [])
        output = json.dumps([by_id, by_url, missing])
        for private in ("private-path-token", "private-query-token", "different-query", "?token="):
            self.assertNotIn(private, output)
        self.assertEqual(hashlib.sha256(self.production.read_bytes()).digest(), before)

    def test_exact_host_does_not_merge_subdomains_www_or_lookalikes(self):
        for key, url in enumerate((
            "https://ample.id/a", "http://ample.id?x=private", "https://ample.id:443/b",
            "https://example.id/", "https://ample.id.evil.id/", "https://www.ample.id/",
            "https://sub.ample.id/", "https://ample.id@evil.id/", "https://ampleXid/",
        ), 1):
            self.add_job(key, url)
        bundle = self.reader().lookup_evidence(self.production, "ample.id", max_jobs=20)
        self.assertEqual({r["id"] for r in bundle["records"]}, {1, 2, 3})
        self.assertTrue(bundle["coverage"]["limited"])
        self.assertEqual(bundle["coverage"]["hostname_scope"], "exact")
        subdomain = self.reader().lookup_evidence(self.production, "sub.ample.id")
        self.assertEqual([r["id"] for r in subdomain["records"]], [7])

    def test_idna_is_nontransitional_and_accepts_both_stored_spellings(self):
        self.add_job(1, "https://xn--fa-hia.id/")
        self.add_job(2, "https://faß.id/path")
        self.add_job(3, "https://fass.id/")
        self.add_job(4, "https://www.xn--fa-hia.id/")
        for target in ("faß.id", "xn--fa-hia.id", "FAß.ID."):
            with self.subTest(target=target):
                result = self.reader().lookup_evidence(self.production, target)
                self.assertEqual({r["id"] for r in result["records"]}, {1, 2})
                self.assertEqual(result["selector"]["hostname"], "xn--fa-hia.id")

    def test_host_selection_is_indexed_and_candidate_limited(self):
        with closing(sqlite3.connect(self.production)) as conn, conn:
            conn.executemany("INSERT INTO jobs(id,url,status) VALUES(?,?,'complete')",
                             ((key, "https://bulk.id/%06d" % key) for key in range(1, 2001)))
            conn.executemany("INSERT INTO jobs(id,url,status) VALUES(?,?,'complete')",
                             ((3000 + key, "https://unrelated-%d.id/" % key)
                              for key in range(1000)))
        reader = self.reader()
        plans = []
        original_connect = sqlite3.connect

        class InspectedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("SELECT") and "FROM jobs" in sql:
                    plan = sqlite3.Connection.execute(
                        self, "EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
                    plans.extend(row[3] for row in plan)
                return sqlite3.Connection.execute(self, sql, parameters)

        def inspected_connect(*args, **kwargs):
            kwargs["factory"] = InspectedConnection
            return original_connect(*args, **kwargs)

        with mock.patch.object(reader.sqlite3, "connect", side_effect=inspected_connect):
            result = reader.lookup_evidence(self.production, "bulk.id", max_jobs=3, max_candidates=8)
        self.assertEqual(len(result["records"]), 3)
        self.assertLessEqual(result["coverage"]["candidates_examined"], 8)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertTrue(plans)
        self.assertFalse(any("SCAN jobs" in plan for plan in plans), plans)
        self.assertTrue(all("SEARCH jobs" in plan for plan in plans), plans)

    def test_malformed_targets_reject_without_database_creation_or_private_errors(self):
        reader = self.reader()
        for target in ("id", ".id", "*.example.id", "bad%name.id", "bad_name.id", "a.id.evil.com",
                       "https://user:private-password@a.id/", "https://a.id:99999/",
                       "https://a.id:8080/", "a.id\\@evil.id", "a.id\n", "#0", "#-1",
                       "#9223372036854775808", "a..id"):
            with self.subTest(target=target):
                with self.assertRaises(reader.EvidenceError) as caught:
                    reader.lookup_evidence(self.production, target)
                self.assertNotIn("private-password", str(caught.exception))
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(reader.EvidenceError):
            reader.lookup_evidence(missing, "valid.id")
        self.assertFalse(missing.exists())

    def test_invalid_limits_are_rejected_before_reading(self):
        reader = self.reader()
        for kwargs in ({"max_jobs": 0}, {"max_jobs": True}, {"max_jobs": 500},
                       {"max_candidates": 0}, {"max_candidates": True}, {"max_candidates": 50000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(reader.EvidenceError):
                reader.lookup_evidence(self.production, "valid.id", **kwargs)

    def test_classifier_and_http_evidence_preserve_measured_score_semantics(self):
        self.add_job(7, "https://models.id/a", status="alerted", risk="high_risk",
                     reason="bima_alert_sent", classifier_json={
                         "status": "high_risk", "reason": "ml_and_real_gambling_context",
                         "ml_source": "indobert", "ml_pred": "gambling", "ml_conf": 0.96,
                         "ml_hit": True, "kw_hit": True, "bert_used": True,
                         "indobert_pred": "gambling", "indobert_conf": 0.96, "bert_ms": 42,
                         "strong_hits": ["deposit", "judi"],
                         "review_excerpt": "Bukti halaman: https://user:private-password@models.id/private-path?token=private-query",
                         "http_classification": {
                             "status": "low_risk", "reason": "safe_news_media_context",
                             "ml_conf": 0.0, "ml_pred": "non-gambling", "bert_used": False,
                         },
                         "page_html": "private-html", "unknown_secret": "private-extra",
                     }, error="private-error", evidence_path="/private/screenshot.png", alert_confidence=0.99)
        row = self.reader().lookup_evidence(self.production, "#7")["records"][0]
        self.assertEqual(row["classification"].get("ml_source"), "indobert")
        self.assertEqual(row["classification"]["ml_conf"], 0.96)
        self.assertEqual(row["classification"]["indobert_conf"], 0.96)
        self.assertEqual(row["http_classification"]["reason"], "safe_news_media_context")
        self.assertNotIn("ml_conf", row["http_classification"])
        self.assertNotIn("ml_pred", row["http_classification"])
        self.assertTrue(any("klasik" in gap.lower() for gap in row["data_gaps"]))
        for private in ("private-password", "private-path", "private-query", "private-html", "private-extra",
                        "private-error", "/private/", "alert_confidence"):
            self.assertNotIn(private, json.dumps(row))
        self.assertNotIn("http_classification", row["classification"])

    def test_safe_rule_and_failed_indobert_never_become_measured_zero_scores(self):
        self.add_job(1, "https://safe.id/", classifier_json={
            "status": "low_risk", "reason": "safe_news_media_context",
            "ml_pred": "non-gambling", "ml_conf": 0, "ml_hit": False, "bert_used": False,
        })
        self.add_job(2, "https://failed.id/", classifier_json={
            "status": "inconclusive", "reason": "low_signal", "bert_used": True,
            "indobert_pred": "unknown", "indobert_conf": 0.0,
        })
        for key in (1, 2):
            row = self.reader().lookup_evidence(self.production, "#%d" % key)["records"][0]
            self.assertIn("bert_used", row["classification"])
            self.assertNotIn("ml_conf", row["classification"])
            self.assertNotIn("ml_pred", row["classification"])
            self.assertNotIn("indobert_conf", row["classification"])
            self.assertTrue(row["data_gaps"])
        failed = self.reader().lookup_evidence(self.production, "#2")["records"][0]
        self.assertEqual(failed["classification"]["indobert_pred"], "unknown")

    def test_malformed_oversized_or_nonfinite_json_is_a_data_gap(self):
        malformed = ("{broken", "[]", "NaN", '{"ml_conf": NaN}',
                     json.dumps({"review_excerpt": "private-oversized" * 6000}))
        for key, raw in enumerate(malformed, 1):
            self.add_job(key, "https://bad-%d.id/" % key, classifier_json=raw)
            row = self.reader().lookup_evidence(self.production, "#%d" % key)["records"][0]
            self.assertEqual(row["classification"], {})
            self.assertIsNone(row["review_excerpt"])
            self.assertTrue(any("klasifikasi" in gap.lower() for gap in row["data_gaps"]))
            self.assertNotIn("private-oversized", json.dumps(row))

    def test_wrong_metadata_types_and_out_of_range_scores_are_omitted(self):
        self.add_job(9, "https://types.id/", classifier_json={
            "status": ["high_risk"], "reason": "https://private.id/?token=private-secret",
            "ml_source": "classic_ml", "ml_pred": "non-gambling", "ml_conf": True,
            "ml_hit": "false", "bert_used": True, "indobert_pred": "gambling",
            "indobert_conf": 1.5, "bert_ms": -1,
            "strong_hits": ["judi", {"secret": "private-extra"}, "deposit"] + ["slot"] * 100,
        })
        row = self.reader().lookup_evidence(self.production, "#9")["records"][0]
        saved = row["classification"]
        self.assertEqual(saved.get("ml_pred"), "non-gambling")
        for field in ("status", "reason", "ml_conf", "ml_hit", "indobert_conf", "bert_ms"):
            self.assertNotIn(field, saved)
        self.assertEqual(saved["strong_hits"][:2], ["judi", "deposit"])
        self.assertLessEqual(len(saved["strong_hits"]), 10)
        self.assertNotIn("private-", json.dumps(row))

    def test_text_redaction_removes_url_credentials_paths_queries_and_tokens(self):
        reader = self.reader()
        self.assertTrue(callable(getattr(reader, "redact_text", None)), "A public text redactor is required")
        text = (
            "[job#7] https://alice:private-password@example.id/reset/private-path?secret=private-query#private-fragment "
            "api_key='private-key-value' Authorization: Bearer private-bearer-token "
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature0123456789 "
            "postgres://alice:private-db-password@private-db.local/private-database "
            "\u202eJudol\x00 " + "kata " * 300
        )
        clean = reader.redact_text(text, max_chars=600)
        self.assertLessEqual(len(clean), 600)
        self.assertIn("[job#7]", clean)
        self.assertIn("example.id", clean)
        for private in ("private-password", "private-path", "private-query", "private-fragment",
                        "private-key-value", "private-bearer-token", "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                        "eyJhbGciOiJIUzI1NiJ9", "private-db-password", "private-database", "\u202e", "\x00"):
            self.assertNotIn(private, clean)

    def make_review(self, *, indexed=True, evidence=True):
        with closing(sqlite3.connect(self.learning)) as conn, conn:
            conn.executescript("""
                CREATE TABLE review_cases (
                    id INTEGER PRIMARY KEY, source_job_id INTEGER NOT NULL,
                    hostname TEXT, final_hostname TEXT, source_updated_at TEXT, created_at TEXT,
                    source_risk TEXT, classifier_status TEXT, classifier_reason TEXT, route_reason TEXT,
                    model_metadata_json TEXT, snapshot_sha256 TEXT, label TEXT, text_label TEXT,
                    reviewer TEXT, review_note TEXT, reviewed_at TEXT
                );
            """)
            if indexed:
                conn.execute("CREATE UNIQUE INDEX source_job ON review_cases(source_job_id)")
            conn.execute("INSERT INTO review_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                11, 7, "review.id", "destination.com", "2026-10-01T10:00:00Z", "2026-10-01T10:05:00Z",
                "medium_risk", "medium_risk", "contextual_gambling_phrase_detected", "risky_http",
                json.dumps({"status": "medium_risk", "ml_source": "classic_ml", "ml_pred": "non-gambling",
                            "ml_conf": 0.17, "bert_used": True, "indobert_pred": "non-gambling",
                            "indobert_conf": 0.00004}),
                "a" * 64, "false_positive", "non_gambling", "private-reviewer", "private-review-note",
                "2026-10-02T12:00:00Z",
            ))
            if evidence:
                conn.executescript("""
                    CREATE TABLE case_evidence(case_id INTEGER PRIMARY KEY, text_value TEXT,
                        text_sha256 TEXT, origin TEXT, attached_at TEXT);
                    CREATE TABLE telegram_incidents(case_id INTEGER PRIMARY KEY, verdict TEXT,
                        reviewer TEXT, decided_at TEXT);
                """)
                conn.execute("INSERT INTO case_evidence VALUES(?,?,?,?,?)", (
                    11, "Kutipan manual https://review.id/private-evidence?token=private-token " + "bukti " * 300,
                    "b" * 64, "manual_capture", "2026-10-02T11:00:00Z"))
                conn.execute("INSERT INTO telegram_incidents VALUES(?,?,?,?)", (
                    11, "false_positive", "private-incident-reviewer", "2026-10-02T11:30:00Z"))

    def test_review_is_a_separate_frozen_snapshot_and_databases_remain_unchanged(self):
        self.add_job(7, "https://review.id/", risk="low_risk", status="complete")
        self.make_review()
        before = [hashlib.sha256(path.read_bytes()).digest() for path in (self.production, self.learning)]
        row = self.reader().lookup_evidence(self.production, "#7", learning_db=self.learning)["records"][0]
        self.assertEqual(row["risk"], "low_risk")
        review = row["review"]
        self.assertIsNotNone(review)
        self.assertEqual(review["case_id"], 11)
        self.assertEqual(review["source_job_id"], 7)
        self.assertEqual(review["source_updated_at"], "2026-10-01T10:00:00Z")
        self.assertEqual(review["created_at"], "2026-10-01T10:05:00Z")
        self.assertEqual(review["classification"]["status"], "medium_risk")
        self.assertEqual(review["label"], "false_positive")
        self.assertEqual(review["incident_verdict"], "false_positive")
        self.assertEqual(review["text_label"], "non_gambling")
        self.assertEqual(review["excerpt_origin"], "manual_capture")
        self.assertLessEqual(len(review["excerpt"]), 600)
        self.assertEqual(review["snapshot_sha256"], "a" * 64)
        self.assertIsNone(row["review_excerpt"])
        self.assertNotIn("private-", json.dumps(row))
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest()
                                  for path in (self.production, self.learning)])

    def test_optional_review_schema_does_not_create_tables_or_require_evidence(self):
        self.add_job(7, "https://review.id/")
        missing = self.root / "not-created.sqlite3"
        self.assertIsNone(self.reader().lookup_evidence(
            self.production, "#7", learning_db=missing)["records"][0]["review"])
        self.assertFalse(missing.exists())
        self.make_review(evidence=False)
        row = self.reader().lookup_evidence(self.production, "#7", learning_db=self.learning)["records"][0]
        self.assertIsNotNone(row["review"])
        self.assertIsNone(row["review"]["excerpt"])
        self.assertIsNone(row["review"]["incident_verdict"])
        with closing(sqlite3.connect(self.learning)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"review_cases"})

    def test_unindexed_optional_review_is_skipped_without_scanning(self):
        self.add_job(7, "https://review.id/")
        self.make_review(indexed=False)
        row = self.reader().lookup_evidence(self.production, "#7", learning_db=self.learning)["records"][0]
        self.assertIsNone(row["review"])
        self.assertTrue(any("indeks" in gap.lower() for gap in row["data_gaps"]))

    def test_large_numeric_metadata_is_omitted_without_crashing(self):
        self.add_job(3, "https://large.id/", classifier_json={
            "status": "low_risk", "ml_source": "classic_ml", "ml_pred": "non-gambling",
            "ml_conf": 10 ** 1000, "bert_used": True, "indobert_pred": "gambling", "indobert_conf": 0.8,
        })
        try:
            row = self.reader().lookup_evidence(self.production, "#3")["records"][0]
        except Exception as error:
            self.fail("Invalid bounded metadata must become a data gap, not %s" % type(error).__name__)
        self.assertNotIn("ml_conf", row["classification"])
        self.assertEqual(row["classification"]["indobert_conf"], 0.8)

    def test_very_long_job_number_has_a_safe_evidence_error(self):
        reader = self.reader()
        try:
            reader.lookup_evidence(self.production, "#" + "9" * 6000)
        except reader.EvidenceError as error:
            self.assertLess(len(str(error)), 200)
        except Exception as error:
            self.fail("Job validation must raise EvidenceError, not %s" % type(error).__name__)
        else:
            self.fail("An out-of-range job must be rejected")

    def test_redaction_handles_scheme_relative_and_bare_urls(self):
        clean = self.reader().redact_text(
            "Lihat //user:private-relative-password@example.id/private-relative-path?x=private-relative-query "
            "dan example.id/private-bare-path?x=private-bare-query dan contoh.id?x=private-bare-query2")
        self.assertNotIn("private-", clean)
        self.assertIn("example.id", clean)
        self.assertIn("contoh.id", clean)

    def test_sql_deadline_is_enforced_with_a_safe_error(self):
        with closing(sqlite3.connect(self.production)) as conn, conn:
            conn.executemany("INSERT INTO jobs(id,url,status) VALUES(?,?,'complete')",
                             ((key, "https://deadline.id/%06d" % key) for key in range(1, 2001)))
        reader = self.reader()
        with mock.patch.object(reader.time, "monotonic", side_effect=[0] + [10] * 100):
            with self.assertRaises(reader.EvidenceError) as caught:
                reader.lookup_evidence(self.production, "deadline.id", max_candidates=1024)
        self.assertNotIn(str(self.production), str(caught.exception))
        self.assertNotIn("interrupted", str(caught.exception))

    def test_missing_url_index_refuses_domain_scan_but_job_lookup_still_works(self):
        separate = self.root / "unindexed.sqlite3"
        with closing(sqlite3.connect(separate)) as conn, conn:
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT,status TEXT)")
            conn.execute("INSERT INTO jobs VALUES(1,'https://minimal.id/','pending')")
        reader = self.reader()
        with self.assertRaises(reader.EvidenceError):
            reader.lookup_evidence(separate, "minimal.id")
        row = reader.lookup_evidence(separate, "#1")["records"][0]
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["risk"])
        self.assertEqual(row["classification"], {})
        self.assertTrue(row["data_gaps"])

    def test_utc_overflow_timestamps_remain_missing_in_current_and_review_evidence(self):
        self.add_job(7, "https://review.id/")
        self.make_review()
        reader = self.reader()
        for stamp in ("9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"):
            with self.subTest(timestamp=stamp):
                with closing(sqlite3.connect(self.production)) as conn, conn:
                    conn.execute("UPDATE jobs SET updated_at=?,next_retry_at=? WHERE id=7", (stamp, stamp))
                with closing(sqlite3.connect(self.learning)) as conn, conn:
                    conn.execute("UPDATE review_cases SET source_updated_at=?,created_at=?,reviewed_at=? WHERE id=11",
                                 (stamp, stamp, stamp))
                    conn.execute("UPDATE case_evidence SET attached_at=? WHERE case_id=11", (stamp,))
                for target in ("#7", "review.id"):
                    try:
                        result = reader.lookup_evidence(self.production, target, learning_db=self.learning)
                    except Exception as error:
                        self.fail("An invalid UTC timestamp must be omitted, not raise %s" % type(error).__name__)
                    self.assertEqual(len(result["records"]), 1)
                    row = result["records"][0]
                    self.assertEqual(row["risk"], "low_risk")
                    self.assertIsNone(row["updated_at"])
                    self.assertIsNone(row["next_retry_at"])
                    self.assertIsNotNone(row["review"])
                    for field in ("source_updated_at", "created_at", "reviewed_at", "excerpt_attached_at"):
                        self.assertIsNone(row["review"][field])


if __name__ == "__main__":
    unittest.main()
