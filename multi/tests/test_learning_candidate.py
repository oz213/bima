import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from learning import candidate, core


class CandidateCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "learning.sqlite3"
        self.runs = self.root / "runs"

    def add_reviews(self, start, end):
        with core.learning_connection(self.db) as conn, conn:
            for i in range(start, end):
                positive = i % 2
                label = "gambling" if positive else "non_gambling"
                group = "domain%d.id" % i
                text = ("casino bonus taruhan" if positive else "kelas sekolah siswa") + " %d" % i
                conn.execute(
                    "INSERT INTO review_cases(id,source_job_id,hostname,domain_group,classifier_status,"
                    "candidate_kind,model_metadata_json,snapshot_sha256,label,text_label,reviewer,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, i, group, group, "medium_risk", "model_rule_disagreement", "{}", "x",
                     "true_positive" if positive else "false_positive", label, "tester", "x", "x"))
                conn.execute("INSERT INTO case_evidence VALUES(?,?,?,?,?)",
                             (i, text, core.sha256_text(text), "manual_capture", "x"))

    def test_waits_for_verified_labels(self):
        self.assertEqual(candidate.learn(self.db, self.runs)["status"], "waiting_for_labels")
        self.assertFalse(any(p.is_dir() for p in self.runs.iterdir()))

    def test_fit_evaluate_repeat_and_update(self):
        self.add_reviews(1, 201)
        report = candidate.learn(self.db, self.runs)
        self.assertEqual(report["status"], "candidate_ready")
        self.assertEqual(report["test"]["f1"], 1)
        model = json.loads((Path(report["run"]) / "model.json").read_text())
        self.assertEqual(candidate.predict(model, "casino taruhan"), 1)
        self.assertEqual(candidate.predict(model, "kelas siswa"), 0)
        with patch.object(candidate, "fit", side_effect=AssertionError):
            self.assertEqual(candidate.learn(self.db, self.runs)["status"], "up_to_date")
        self.add_reviews(201, 203)
        fresh = candidate.learn(self.db, self.runs)
        self.assertNotEqual(report["run"], fresh["run"])

    def test_interruption_leaves_no_published_model(self):
        self.add_reviews(1, 201)
        with patch.object(candidate, "evaluate", side_effect=OSError), self.assertRaises(OSError):
            candidate.learn(self.db, self.runs)
        self.assertFalse(any(p.is_dir() for p in self.runs.iterdir()))
