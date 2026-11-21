import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crawler_multi as crawler
import telegram_monitor as bot
import telegram_review as review
from learning import core


class TelegramReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "learn.sqlite3"
        self.prod = Path(tmp.name) / "prod.sqlite3"
        env = patch.dict(os.environ, {"TELEGRAM_REVIEW_USER_IDS": "42", "BIMA_LEARNING_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)
        with core.learning_connection(self.db) as c, c:
            c.execute("INSERT INTO review_cases(id,source_job_id,hostname,domain_group,classifier_status,"
                      "candidate_kind,model_metadata_json,snapshot_sha256,created_at,updated_at) "
                      "VALUES(1,55,'site.id','site.id','high_risk','model_rule_disagreement','{}','a','t','t')")
        self.text = "Contoh kutipan halaman yang akan diperiksa oleh analis."

    def command(self, text):
        return bot.handle_command(text, "42", {"42"}, self.prod, 88, "42")

    def click(self, action):
        return review.handle("br:1:" + action, "42", "42", {"42"}, self.prod, callback=True)

    def test_private_allowlist(self):
        for chat, sender, allowed in [("-7", "42", {"-7"}), ("41", "41", {"41"}), ("42", "42", set())]:
            self.assertIn("reviewer", review.handle("br:1:yes", chat, sender, allowed, self.prod, True))

    def test_site_then_independent_text(self):
        self.assertIn("Judol", str(self.command("/review")))
        self.assertIn("Keputusan situs: Judol", self.click("yes"))
        self.assertIn("Keputusan situs: Judol", self.click("no"))
        self.assertIn("kosong", self.command("/review"))
        self.command("/bukti 1 " + self.text)
        self.click("neg")
        with core.learning_connection(self.db) as c:
            self.assertEqual(c.execute("SELECT label,text_label,reviewer FROM review_cases").fetchone(),
                             ("true_positive", "non_gambling", "telegram:42"))

    def test_single_click_only_with_evidence_and_no_overwrite(self):
        self.assertIn("bukti", self.click("bothpos"))
        self.assertIn("bothpos", str(self.command("/bukti 1 " + self.text)))
        self.click("bothneg")
        self.click("bothpos")
        with core.learning_connection(self.db) as c:
            self.assertEqual(c.execute("SELECT label,text_label FROM review_cases").fetchone(),
                             ("false_positive", "non_gambling"))
            self.assertEqual(c.execute("SELECT count(*) FROM review_events").fetchone()[0], 1)

    def test_capture_seed_to_card(self):
        data = {"status":"medium_risk", "reason":"contextual_gambling_phrase_detected",
                "bert_used":True, "ml_pred":"non-gambling", "indobert_pred":"non-gambling"}
        with patch.dict(os.environ, {"BIMA_REVIEW_EXCERPTS":"1"}):
            crawler.capture_review_excerpt(data, self.text * 20)
        import json
        row = (56,"https://other.id/","https://other.id/","alerted","medium_risk","r","r","t",json.dumps(data))
        candidate = core.candidate_from_job(row)
        with patch.object(core, "load_candidates", return_value=([candidate], 1)):
            core.seed_review_queue(self.prod, self.db)
        card = self.command("/review 2")
        self.assertIn(self.text, card["text"])
        self.assertIn("bothpos", str(card["reply_markup"]))
