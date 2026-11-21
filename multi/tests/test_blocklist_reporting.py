"""Offline replay of job 38219's decisions using representative rendered text."""
import ast
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import urljoin, urlparse, unquote

from test_anti_gambling_rejection_v440 import load_classifier_functions


PAGE = ("Cek domain Lapor situs Masuk Daftar. Blokir situs porno, judi online, malware, spyware. "
        "Contoh hasil pengecekan: slot-gacor-contoh.com Diblokir · Judi online. "
        "Domain terblokir: 998.967 Pornografi 342.664 Judi online 190.170 Malware & phishing. "
        "Menemukan situs judol, porno, spyware, atau sumber popup? Laporkan dalam hitungan detik. "
        "Daftar blokir untuk menyaring situs porno, judi online, malware dan iklan.")
HTML = '<a href="/check">Cek domain</a><a href="/register">Daftar</a>'


class BlocklistRegression(unittest.TestCase):
    def setUp(self):
        ns = self.ns = load_classifier_functions()
        source = Path(__file__).resolve().parents[2] / "crawler_url.py"
        tree = ast.parse(source.read_text())
        funcs = {"classify_text_content", "is_blocklist_reporting_context",
                 "BlocklistNavigationParser", "find_keyword_hits", "normalize_text_for_match",
                 "has_contextual_gambling_phrase"}
        nodes = [n for n in tree.body if getattr(n, "name", "") in funcs or
                 isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "STRONG_TEXT_KEYWORDS"
                                                    for t in n.targets)]
        ns.update(HTMLParser=HTMLParser, urlparse=urlparse, urljoin=urljoin, unquote=unquote,
                  ENABLE_INDO_BERT=True, log=lambda *args: None)
        for n in ast.walk(next(n for n in nodes if getattr(n, "name", "") == "classify_text_content")):
            if isinstance(n, ast.Name) and n.id.startswith(("is_safe_", "is_captcha_", "is_gambling_content_keywords")):
                ns[n.id] = lambda *args: False
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)

    def classify(self, text=PAGE, html=HTML, classic="non-gambling", bert="non-gambling"):
        self.ns["is_gambling_content_ml"] = lambda *a, **kw: (False, 0.1702055863386951, classic)
        self.ns["is_gambling_content_indobert"] = lambda *a: (False, 0.00003873273089993745, bert)
        return self.ns["classify_text_content"]("selenium", "http://sample.id", text,
                                                 "Internet bersih", "https://sample.id/", html)

    def test_recorded_model_scores_and_hits(self):
        result = self.classify(html=lambda: HTML)
        self.assertEqual((result["status"], result["reason"]), ("low_risk", "blocklist_reporting_context"))
        self.assertEqual(set(result["strong_hits"]), {"judi", "judol", "slot", "slot gacor"})

    def test_unaccounted_promotional_text_and_links(self):
        for suffix in (" Main slot gacor sekarang", " Klaim bonus member", " Deposit via QRIS"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.classify(PAGE + suffix)["status"], "medium_risk")
        for markup in ('<a href="https://other.id">Daftar</a>', '<form action="/deposit"></form>',
                       '<base href="https://other.id/"><a href="daftar">Daftar</a>'):
            self.assertEqual(self.classify(html=HTML + markup)["status"], "medium_risk")

    def test_incomplete_or_conflicting_models(self):
        for classic, bert in (("gambling","non-gambling"), ("non-gambling","gambling"),
                              ("unknown","non-gambling"), ("non-gambling","disabled")):
            self.assertNotEqual(self.classify(classic=classic, bert=bert)["status"], "low_risk")

    def test_missing_evidence_or_hidden_link(self):
        self.assertEqual(self.classify(html=None)["status"], "medium_risk")
        self.assertEqual(self.classify(text=PAGE.replace("Diblokir", "Aktif"))["status"], "medium_risk")
        self.ns["has_hidden_gambling_links"] = lambda *a, **kw: (True, ["slot"], [])
        self.assertEqual(self.classify()["status"], "medium_risk")
