import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import telegram_monitor as bot
import telegram_review
from learning import core
from proactive_discovery import SCHEMA


class PatternReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "review.sqlite3"
        self.prod = Path(tmp.name) / "crawler.sqlite3"
        env = patch.dict(os.environ, {"BIMA_LEARNING_DB": str(self.db),
                                      "TELEGRAM_REVIEW_USER_IDS": "42"})
        env.start()
        self.addCleanup(env.stop)
        with closing(sqlite3.connect(self.prod)) as conn, conn:
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT)")
            conn.executescript(SCHEMA)

    def add(self, key, domain, method=None, evidence=None):
        host = "site%d.id" % key
        url = "https://" + host + "/promo"
        with closing(sqlite3.connect(self.prod)) as conn, conn:
            conn.execute("INSERT INTO jobs VALUES(?,?)", (key, url))
            if method:
                conn.execute("INSERT INTO proactive_redirect_observations VALUES(?,?,?,?,?,1,'a','b','new')",
                             (host, url, "https://example.com/path?token=secret", method, 302))
        with core.learning_connection(self.db) as conn, conn:
            conn.execute("INSERT INTO review_cases(id,source_job_id,hostname,domain_group,classifier_status,"
                         "candidate_kind,model_metadata_json,snapshot_sha256,created_at,updated_at) "
                         "VALUES(?,?,?,?, 'medium_risk','model_rule_disagreement','{}','abc','a','b')",
                         (key, key, host, domain))
        if evidence:
            core.attach_evidence_text(self.db, key, evidence, "manual_capture")

    def cmd(self, command):
        return bot.handle_command(command, "42", {"42"}, self.prod, 1, "42")

    def click(self, key, action):
        return telegram_review.handle(f"br:{key}:{action}", "42", "42", {"42"}, self.prod, True)

    def test_combined_observations_prioritize_only_related_case(self):
        self.add(1, "one.id")
        for key in (2,3,4):
            self.add(key, f"s{key}.id", "http", "Klaim bonus anggota sekarang dengan penawaran khusus.")
        text = self.cmd("/review")["text"]
        self.assertIn("Kasus #2", text)
        self.assertIn("Redirect HTTP ke example.com + Frasa", text)
        self.assertNotIn("token", text)
        self.click(2, "bothpos")
        self.assertIn("Judol: 1", self.cmd("/pola")["text"])
        self.click(3, "defer")
        self.click(3, "defer")
        self.assertIn("Kasus #4", self.cmd("/pola")["text"])
        with core.learning_connection(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM review_events").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT label,text_label FROM review_cases WHERE id=4").fetchone(), (None,None))

    def test_separate_group_count_and_static_method(self):
        for key in (1,2,3):
            self.add(key, "same.id", "js_literal")
        self.assertIn("Belum ada", self.cmd("/pola"))
        self.add(4, "two.id", "js_literal")
        self.add(5, "three.id", "js_literal")
        self.assertIn("Petunjuk JS statis", self.cmd("/pola")["text"])
        self.assertNotIn("Redirect HTTP", self.cmd("/pola")["text"])

    def test_legacy_host_and_text_independent_of_discovery_table(self):
        for key in (1,2,3):
            self.add(key, f"s{key}.id", evidence="Penawaran spesial pekan ini khusus anggota aktif.")
        with closing(sqlite3.connect(self.prod)) as conn, conn:
            conn.execute("DROP TABLE proactive_redirect_observations")
        self.assertIn("Frasa", self.cmd("/pola")["text"])
        with core.learning_connection(self.db) as conn, conn:
            conn.execute("UPDATE review_cases SET final_hostname='target.id'")
            conn.execute("DELETE FROM case_evidence")
        self.assertIn("Host akhir crawl target.id", self.cmd("/pola")["text"])
        self.assertIn("reviewer", bot.handle_command("/pola", "-9", {"-9"}, self.prod, 0, "42"))
