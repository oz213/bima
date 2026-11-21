"""BIMA v4.4.0: regression tests for negated gambling service policies.

Only extracts the selected pure functions via AST. No model download, network,
production database access, alert sending, or alterations to crawler runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "crawler_url.py"


def load_classifier_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    names = {"is_anti_gambling_rejection_context", "classify_risk"}
    selected = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names]
    if {node.name for node in selected} != names:
        raise AssertionError("BIMA anti-judol helper and classify_risk must both exist")
    namespace = {
        "re": re,
        "ML_THRESHOLD": 0.75,
        "ML_STRONG_THRESHOLD": 0.90,
        "INDO_BERT_THRESHOLD": 0.80,
        "INDO_BERT_STRONG_THRESHOLD": 0.92,
        "REQUIRE_GAMBLING_CONTEXT_FOR_ML": True,
        "STRONG_TEXT_KEYWORDS": ["judi", "judol", "slot", "deposit", "dana", "bonus",
                                 "slot gacor", "rtp slot", "togel", "casino"],
        "find_keyword_hits": lambda text, keywords: [
            keyword for keyword in keywords if re.search(r"\b" + re.escape(keyword) + r"\b", text.lower())],
        "has_real_gambling_context": lambda text: "judi online" in text.lower(),
        "has_safe_product_or_industrial_context": lambda text: False,
        "has_safe_news_or_factcheck_context": lambda text: False,
        "has_contextual_gambling_phrase": lambda text: (
            "judi online" in text.lower() or "slot gacor" in text.lower()),
        "has_strong_operator_gambling_promo": lambda text, title: False,
        "has_slot_thailand_registration": lambda *args: False,
        "has_hidden_gambling_links": lambda html, page_url=None: (False, [], []),
    }
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class AntiGamblingRejectionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = load_classifier_functions()

    def classify(self, page_text, *, ml_pred="non-gambling", ml_conf=0.23, page_html=""):
        return self.ctx["classify_risk"](
            page_text, False, ml_conf, ml_pred,
            page_title="Layanan Digital", page_html=page_html,
            current_url="https://example.id/",
        )

    def test_backlink_business_policy_no_judol_alert(self):
        sample = (
            "Marketplace artikel sponsor dan backlink Indonesia. "
            "Tanpa situs judi. Bebas judol - dicek otomatis. "
            "Order untuk judi online, pinjol ilegal, dan konten dewasa ditolak otomatis. "
            "Pemilik blog menjual slot artikel sponsor dan dana ditahan escrow sampai link tayang. "
            "Deposit hanya untuk pembayaran pesanan artikel."
        )
        status, reason, _ = self.classify(sample, ml_conf=0.23135074939286437)
        self.assertEqual((status, reason), ("low_risk", "anti_gambling_rejection_context"))

    def test_whatsapp_gateway_policy_no_judol_alert(self):
        sample = (
            "Layanan WhatsApp API untuk promosi UMKM, bonus referral untuk pekerja. "
            "Promosi judi online, pinjaman ilegal, penipuan dan pornografi tidak diterima. "
            "Judi online dan pinjaman ilegal tidak diterima oleh tim moderasi pesan."
        )
        status, reason, _ = self.classify(sample, ml_conf=0.18255034760508648)
        self.assertEqual((status, reason), ("low_risk", "anti_gambling_rejection_context"))

    def test_rejection_without_gambling_mention_does_not_bypass(self):
        sample = "Kami tidak menerima pendaftaran umum. Slot gacor deposit qris, daftar sekarang."
        self.assertFalse(self.ctx["is_anti_gambling_rejection_context"](sample))

    def test_real_gambling_promo_with_disclaimer_not_exempt(self):
        sample = (
            "Judi online tidak diterima di kanal bantuan. "
            "Tapi main slot gacor dan RTP slot terbaru bisa daftar sekarang di sini."
        )
        self.assertFalse(self.ctx["is_anti_gambling_rejection_context"](sample))
        status, reason, _ = self.classify(sample)
        self.assertEqual(status, "medium_risk")
        self.assertEqual(reason, "contextual_gambling_phrase_detected")

    def test_active_gambling_marketing_no_exemption(self):
        sample = "Main slot gacor, RTP slot, deposit qris dan bonus new member. Daftar sekarang!"
        self.assertFalse(self.ctx["is_anti_gambling_rejection_context"](sample))

    def test_hidden_gambling_link_veto(self):
        namespace = self.ctx
        original = namespace["has_hidden_gambling_links"]
        try:
            namespace["has_hidden_gambling_links"] = lambda html, page_url=None: (True, ["slot"], [])
            sample = "Kami tidak menerima judi online dan semua pesanan judi online ditolak otomatis."
            self.assertFalse(namespace["is_anti_gambling_rejection_context"](
                sample, page_html="<a href='https://judi.example'>hidden</a>"))
            self.assertEqual(self.classify(sample, page_html="<a>hidden</a>")[0], "medium_risk")
        finally:
            namespace["has_hidden_gambling_links"] = original

    def test_positive_ml_is_never_overruled_by_rejection(self):
        sample = (
            "Promosi judi online ditolak otomatis. Produk digital dan pembayaran aman. "
            "Tanpa situs judi."
        )
        status, reason, _ = self.classify(sample, ml_pred="gambling", ml_conf=0.94)
        self.assertEqual(status, "high_risk")
        self.assertEqual(reason, "ml_strong_with_real_gambling_context")

    def test_unknown_ml_is_not_treated_as_non_gambling(self):
        sample = "Promosi judi online ditolak otomatis. Tanpa situs judi."
        status, reason, _ = self.classify(sample, ml_pred="unknown", ml_conf=0.0)
        self.assertEqual((status, reason), ("medium_risk", "contextual_gambling_phrase_detected"))

    def test_exception_in_hidden_link_checker_fails_closed(self):
        namespace = self.ctx
        original = namespace["has_hidden_gambling_links"]
        try:
            namespace["has_hidden_gambling_links"] = lambda html, page_url=None: 1 / 0
            sample = "Promosi judi online ditolak otomatis. Tanpa situs judi."
            self.assertFalse(namespace["is_anti_gambling_rejection_context"](sample, page_html="<div>x</div>"))
        finally:
            namespace["has_hidden_gambling_links"] = original


if __name__ == "__main__":
    unittest.main()
