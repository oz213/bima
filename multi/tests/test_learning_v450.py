"""Offline BIMA v4.5.0 learning pilot tests; isolated, temporary SQLite only.

No production state, network, crawlers, IndoBERT, Telegram, or BIMA API.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest import mock

from learning import core
from learning import cli


def cls(status="medium_risk", reason="contextual_gambling_phrase_detected",
        ml="non-gambling", bert="non-gambling", used=True, http=None):
    obj = {"status": status, "reason": reason, "ml_pred": ml,
           "indobert_pred": bert, "bert_used": used,
           "ml_conf": 0.23, "indobert_conf": 0.00004,
           "strong_hits": ["judol", "deposit"]}
    if http is not None:
        obj["http_classification"] = http
    return obj


class LearningFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.production = self.root / "crawler_production.sqlite3"
        self.learning = self.root / "learning.sqlite3"
        with closing(sqlite3.connect(self.production)) as conn:
            conn.execute("""CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, url TEXT, final_url TEXT,
                status TEXT, risk TEXT, route_reason TEXT,
                reason TEXT, updated_at TEXT, classifier_json TEXT
            )""")
            conn.execute("CREATE INDEX idx_jobs_status ON jobs(status,id)")
            self.add_job(conn, 1, "https://a.id/p", "alerted", "risky_http", cls())
            self.add_job(conn, 2, "https://b.my.id/", "alerted", "legacy_selenium_followup",
                         cls("high_risk", "ml_and_real_gambling_context",
                             ml="gambling", http={"status": "low_risk", "reason": "low_signal"}))
            self.add_job(conn, 3, "https://c.id/", "pending_alert", "risky_http", cls())
            self.add_job(conn, 4, "https://d.id/", "alerted", "risky_http",
                         cls(ml="gambling", bert="gambling"))
            self.add_job(conn, 5, "https://e.id/", "alerted", "risky_http",
                         cls(used=False))
            self.add_job(conn, 6, "https://x.com/", "alerted", "risky_http", cls())
            self.add_job(conn, 7, "https://bad.id/", "alerted", "risky_http", "{ invalid")
            conn.commit()

    def add_job(self, conn, job_id, url, status, route, classifier):
        raw = (json.dumps(classifier) if isinstance(classifier, dict) else classifier)
        conn.execute(
            "INSERT INTO jobs (id,url,final_url,status,risk,route_reason,reason,"
            "updated_at,classifier_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, url, url, status, "medium_risk", route,
             "test_job_reason", "2026-11-21T00:00:00Z", raw),
        )

    def seed(self):
        return core.seed_review_queue(self.production, self.learning, limit=20)

    def test_seed_only_review_candidates_and_never_production(self):
        with closing(sqlite3.connect(self.production)) as conn:
            before = list(conn.execute("SELECT id,status,risk FROM jobs ORDER BY id"))
        data = self.seed()
        self.assertEqual(data, {"scanned_alerts": 6, "candidates": 2,
                                "new_cases": 2, "already_present": 0})
        with closing(sqlite3.connect(self.production)) as conn:
            self.assertEqual(before, list(conn.execute(
                "SELECT id,status,risk FROM jobs ORDER BY id")))
            self.assertEqual({r[0] for r in conn.execute("SELECT name FROM sqlite_master")},
                             {"jobs", "idx_jobs_status"})
        with core.learning_connection(self.learning) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_cases").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0)

    def test_seed_idempotence_and_existing_review_not_overwritten(self):
        self.seed()
        core.attach_evidence_text(self.learning, 1, "Non-gambling business rejects judi: " * 8,
                                  "manual_capture")
        core.review_case(self.learning, 1, "false_positive", "analis", "Manual evidence verified", text_label="non_gambling")
        result = self.seed()
        self.assertEqual(result["new_cases"], 0)
        with core.learning_connection(self.learning) as conn:
            row = conn.execute("SELECT label,reviewer FROM review_cases WHERE id=1").fetchone()
            self.assertEqual(row, ("false_positive", "analis"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 1)

    def test_read_only_source_scan_bounded(self):
        with self.assertRaises(ValueError):
            core.load_candidates(self.production, 50001)
        with self.assertRaises(ValueError):
            core.load_candidates(self.production, -1)
        with mock.patch.object(core, "sqlite3") as sqlite_mock:
            sqlite_mock.connect.side_effect = RuntimeError("stop after asserting URI")
            with self.assertRaises(RuntimeError):
                core.load_candidates(self.production, 5)
            args, kwargs = sqlite_mock.connect.call_args
            self.assertTrue(args[0].endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])

    def test_protect_production_even_when_samefile_symlink(self):
        with self.assertRaisesRegex(ValueError, "Refusing"):
            core.seed_review_queue(self.production, self.production)
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(self.production)
        with self.assertRaises(ValueError):
            with core.learning_connection(alias, self.production):
                pass

    def test_final_classifier_not_intermediate_http(self):
        self.seed()
        cases = core.list_cases(self.learning, include_reviewed=True)
        self.assertEqual({c["candidate_kind"] for c in cases}, set(core.KINDS))
        self.assertEqual(next(c for c in cases if c["source_job_id"] == 2)["classifier_status"],
                         "high_risk")

    def test_queue_starts_unlabeled_and_list_never_exposes_text(self):
        self.seed()
        self.assertEqual(core.review_stats(self.learning)["labels"], {"pending": 2})
        self.assertTrue(all("text" not in r for r in core.list_cases(self.learning)))
        with self.assertRaisesRegex(ValueError, "Attach verified evidence"):
            core.review_case(self.learning, 1, "true_positive", "analis", "Reviewed as judol", text_label="gambling")

    def test_verified_review_locked_and_audited(self):
        self.seed()
        text = "No judol accepted for this business service. " * 8
        self.assertFalse(core.attach_evidence_text(
            self.learning, 1, text, "approved_archive")["already_attached"])
        self.assertTrue(core.attach_evidence_text(
            self.learning, 1, text, "approved_archive")["already_attached"])
        reviewed = core.review_case(self.learning, 1, "false_positive", "analis",
                                    "Site denies judol and verified no gambling", "a.id", "non_gambling")
        self.assertEqual(reviewed["label"], "false_positive")
        with core.learning_connection(self.learning) as conn:
            self.assertEqual(conn.execute("SELECT label FROM review_cases WHERE id=1").fetchone()[0],
                             "false_positive")
            self.assertEqual(conn.execute("SELECT new_label FROM review_events").fetchone()[0],
                             "false_positive")
        with self.assertRaisesRegex(ValueError, "immutable"):
            core.review_case(self.learning, 1, "true_positive", "other", "Try silently changing it", text_label="gambling")
        with self.assertRaisesRegex(ValueError, "locked"):
            core.attach_evidence_text(self.learning, 1, "Changed text " * 10, "manual_capture")

    def test_evidence_immutable_before_review_too(self):
        self.seed()
        core.attach_evidence_text(self.learning, 1, "first version " * 10, "manual_capture")
        with self.assertRaisesRegex(ValueError, "immutable"):
            core.attach_evidence_text(self.learning, 1, "second version " * 10, "manual_capture")
        self.assertTrue(core.review_stats(self.learning)["evidence_attached"] == 1)

    def test_needs_review_is_not_a_training_label(self):
        self.seed()
        core.review_case(self.learning, 2, "needs_review", "analyst2",
                         "No independent evidence yet")
        pending = core.list_cases(self.learning)
        self.assertIn(2, [r["id"] for r in pending])
        with self.assertRaisesRegex(ValueError, "Not enough"):
            core.export_dataset(self.learning, self.root / "no-export", min_total=1,
                                min_per_class=1, min_groups=1)
        self.assertFalse((self.root / "no-export").exists())

    def test_manual_label_can_be_final_after_needs_review(self):
        self.seed()
        core.review_case(self.learning, 2, "needs_review", "analyst2",
                         "Needs confirmation from analyst")
        core.attach_evidence_text(self.learning, 2, "confirmed promo judi " * 10,
                                  "screenshot_transcript")
        core.review_case(self.learning, 2, "true_positive", "analyst2",
                         "Verified with archived screenshot", text_label="gambling")
        with core.learning_connection(self.learning) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_events WHERE case_id=2").fetchone()[0], 2)

    def test_export_group_split_and_label_mapping(self):
        # Populate a larger, balanced, independently labeled synthetic gold set.
        with core.learning_connection(self.learning) as conn:
            with conn:
                for i in range(1, 61):
                    host = "domain%d.id" % i
                    meta = core.dumps({"status": "medium_risk"})
                    conn.execute(
                        "INSERT INTO review_cases(source_job_id,hostname,domain_group,"
                        "classifier_status,candidate_kind,model_metadata_json,snapshot_sha256,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (i, host, host, "medium_risk", "model_rule_disagreement",
                         meta, "dummy", core.utcnow(), core.utcnow()),
                    )
        for i in range(1, 61):
            text = ("Verified detailed unique evidence for domain%d.id " % i) * 4
            core.attach_evidence_text(self.learning, i, text, "manual_capture")
            label = "true_positive" if i % 2 == 0 else "false_positive"
            core.review_case(self.learning, i, label, "analis", "Independent reviewed evidence", text_label=(
                "gambling" if i % 2 == 0 else "non_gambling"))
        out = self.root / "pilot-export"
        manifest = core.export_dataset(self.learning, out, min_total=60,
                                       min_per_class=25, min_groups=50)
        self.assertEqual(sum(m["cases"] for m in manifest["splits"].values()), 60)
        groups = {}
        for split in ("train", "validation", "test"):
            items = [json.loads(l) for l in (out / (split + ".jsonl")).read_text().splitlines()]
            self.assertTrue(items)
            for item in items:
                self.assertEqual(item["label"], 1 if item["case_id"] % 2 == 0 else 0)
                self.assertNotIn("hostname", item)
                self.assertNotIn("url", item)
                group = "domain%d.id" % item["case_id"]
                self.assertNotIn(group, groups)
                groups[group] = split
        self.assertEqual(len(groups), 60)
        self.assertFalse(manifest["training_started"])
        self.assertEqual(json.loads((out / "manifest.json").read_text()), manifest)
        with self.assertRaisesRegex(ValueError, "new path"):
            core.export_dataset(self.learning, out)

    def test_conflicting_identical_text_labels_block_export(self):
        self.seed()
        text = "identical approved source text for a test " * 6
        for i, label in ((1, "true_positive"), (2, "false_positive")):
            core.attach_evidence_text(self.learning, i, text, "manual_capture")
            core.review_case(self.learning, i, label, "analis", "Reviewed evidence as %s" % label,
                             text_label=("gambling" if label == "true_positive" else "non_gambling"))
        out = self.root / "conflict"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            core.export_dataset(self.learning, out, min_total=2, min_per_class=1, min_groups=2)
        self.assertFalse(out.exists())

    def test_same_text_in_different_domain_groups_is_rejected(self):
        self.seed()
        text = "same reused syndication page not independent evidence " * 5
        for i in (1, 2):
            core.attach_evidence_text(self.learning, i, text, "approved_archive")
            core.review_case(self.learning, i, "false_positive", "analyst",
                             "Explicitly verified non-gambling page", text_label="non_gambling")
        with core.learning_connection(self.learning) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO review_cases(source_job_id,hostname,domain_group,"
                    "classifier_status,candidate_kind,model_metadata_json,snapshot_sha256,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (8, "other.id", "other.id", "high_risk", "http_browser_escalation",
                     core.dumps({"status": "high_risk"}), "other-digest", core.utcnow(), core.utcnow()),
                )
        core.attach_evidence_text(self.learning, 3, "distinct gambling promotion " * 10,
                                  "approved_archive")
        core.review_case(self.learning, 3, "true_positive", "analyst",
                         "Independent positive text evidence", text_label="gambling")
        with self.assertRaisesRegex(ValueError, "Identical evidence across"):
            core.export_dataset(self.learning, self.root / "cross-domain-leak",
                                min_total=3, min_per_class=1, min_groups=3)
        self.assertFalse((self.root / "cross-domain-leak").exists())

    def test_incident_label_must_not_label_text_by_itself(self):
        self.seed()
        text = "This page is bland but redirects to a judol elsewhere. " * 5
        core.attach_evidence_text(self.learning, 2, text, "approved_archive")
        with self.assertRaisesRegex(ValueError, "text-label"):
            core.review_case(self.learning, 2, "true_positive", "analyst",
                             "Verified gambling redirect but not text")
        core.review_case(self.learning, 2, "true_positive", "analyst",
                         "Verified redirect signal, not gambling text", text_label="not_for_training")
        self.assertEqual(core.review_stats(self.learning)["text_labels"]["not_for_training"], 1)
        with self.assertRaisesRegex(ValueError, "Not enough"):
            core.export_dataset(self.learning, self.root / "no-source-text",
                                min_total=1, min_per_class=1, min_groups=1)
        self.assertFalse((self.root / "no-source-text").exists())

    def test_cli_seed_stats_without_network(self):
        with mock.patch.object(cli, "seed_review_queue", wraps=core.seed_review_queue) as seed_spy:
            with mock.patch("sys.stdout"):
                result = cli.main(["--learning-db", str(self.learning), "seed",
                                   "--production-db", str(self.production), "--limit", "50"])
            self.assertEqual(result, 0)
            seed_spy.assert_called_once()
        with mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["--learning-db", str(self.learning), "stats"]), 0)
        self.assertFalse((self.root / "unused-source").exists())


if __name__ == "__main__":
    unittest.main()
