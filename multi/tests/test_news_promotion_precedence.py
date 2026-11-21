"""Exercise the shared classifier without starting the live crawler or loading ML models."""
import ast
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler_multi import classify_shared


def load_engine():
    source = Path(__file__).resolve().parents[2] / "crawler_url.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    engine = SimpleNamespace(
        re=re, HTMLParser=HTMLParser, BeautifulSoup=BeautifulSoup, urljoin=urljoin,
        urlparse=urlparse, unquote=unquote, parse_qsl=parse_qsl, urlencode=urlencode,
        ML_THRESHOLD=0.75, ML_STRONG_THRESHOLD=0.90, ENABLE_INDO_BERT=True,
        INDO_BERT_THRESHOLD=0.80, INDO_BERT_STRONG_THRESHOLD=0.92,
        REQUIRE_GAMBLING_CONTEXT_FOR_ML=True,
        log=lambda message: None,
        KEYWORD_RE=None,
        is_gambling_content_ml=lambda text, threshold: (False, 0.20, "non-gambling"),
        is_gambling_content_indobert=lambda text: (True, 0.99, "gambling"),
    )
    # Load only reachable definitions and literal data. Importing the module
    # itself reads the deployment config and initializes the model runtime.
    definitions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    literals = {target.id: n.value for n in tree.body if isinstance(n, ast.Assign)
                for target in n.targets if isinstance(target, ast.Name) and target.id.isupper()}
    needed = set()
    pending = ["classify_text_content", "compile_keyword_regex"]
    while pending:
        name = pending.pop()
        if name in needed or name in vars(engine):
            continue
        if name in literals:
            setattr(engine, name, ast.literal_eval(literals[name]))
        elif name in definitions:
            needed.add(name)
            pending.extend(node.id for node in ast.walk(definitions[name])
                           if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))
    nodes = [n for n in tree.body if getattr(n, "name", None) in needed]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), vars(engine))
    engine.KEYWORD_RE = engine.compile_keyword_regex(["deposit", "jackpot", "rtp", "taruhan"])
    return engine


class NewsPromotionPrecedence(unittest.TestCase):
    OPERATOR_TEXT = ("Daftar, login, deposit, jackpot dan RTP. Permainan kami menerima taruhan. "
                     "Klik untuk memulai putaran.")
    OPERATOR_TITLE = "Spin dan Menang"
    STORE_TEXT = " Harga produk dan metode pembayaran."
    REGISTRATION_TEXT = "Berita terbaru tentang permainan ini. Daftar untuk membuat akun Anda."
    REGISTRATION_TITLE = "Slot Thailand"
    REGISTRATION_HTML = '<main><a href="/join">Daftar</a></main>'

    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()

    def classify(self, text, title, path="/page", html=None):
        url = "https://example.id" + path
        return classify_shared(self.engine, url, url, text, title, page_html=html)

    def test_editorial_markers_do_not_mask_operator_promotion(self):
        for path, prefix in (
            ("/page", ""), ("/news/page", ""), ("/page", "Berita. "),
            ("/page", "Nasional daerah cegah judi online. "), ("/page", "Ekonomi opini hukum. "),
        ):
            with self.subTest(path=path, prefix=prefix):
                result = self.classify(prefix + self.OPERATOR_TEXT, self.OPERATOR_TITLE, path)
                self.assertEqual((result["status"], result["reason"], result["bert_used"]),
                                 ("high_risk", "strong_operator_gambling_promotion", True))

    def test_store_language_does_not_mask_editorial_conflicts(self):
        for path, text, title, html, expected_reason in (
            ("/news/page", self.OPERATOR_TEXT, self.OPERATOR_TITLE, None,
             "strong_operator_gambling_promotion"),
            ("/page", "Nasional daerah cegah judi online. " + self.OPERATOR_TEXT,
             self.OPERATOR_TITLE, None, "strong_operator_gambling_promotion"),
            ("/page", "Ekonomi opini hukum. " + self.OPERATOR_TEXT,
             self.OPERATOR_TITLE, None, "strong_operator_gambling_promotion"),
            ("/news/page", self.REGISTRATION_TEXT, self.REGISTRATION_TITLE,
             self.REGISTRATION_HTML, "ml_strong_slot_thailand_registration"),
        ):
            with self.subTest(path=path, text=text):
                result = self.classify(text + self.STORE_TEXT, title, path, html)
                self.assertEqual((result["status"], result["reason"], result["bert_used"]),
                                 ("high_risk", expected_reason, True))

    def test_store_retains_precedence_without_editorial_conflict(self):
        for text, title in (
            (self.OPERATOR_TEXT, self.OPERATOR_TITLE),
            ("Ekonomi opini hukum. Keterangan pesanan pelanggan.", "Toko"),
        ):
            with self.subTest(title=title):
                result = self.classify(text + self.STORE_TEXT, title)
                self.assertEqual((result["status"], result["reason"], result["bert_used"]),
                                 ("low_risk", "safe_store_topup_context", False))

    def test_visible_registration_link_on_editorial_page_reaches_model(self):
        result = self.classify(self.REGISTRATION_TEXT, self.REGISTRATION_TITLE,
                               html=lambda: self.REGISTRATION_HTML)
        self.assertEqual((result["status"], result["reason"], result["bert_used"]),
                         ("high_risk", "ml_strong_slot_thailand_registration", True))

    def test_registration_still_requires_strong_model_support(self):
        original = self.engine.is_gambling_content_indobert
        self.addCleanup(setattr, self.engine, "is_gambling_content_indobert", original)
        self.engine.is_gambling_content_indobert = lambda text: (True, 0.88, "gambling")
        result = self.classify(self.REGISTRATION_TEXT, self.REGISTRATION_TITLE,
                               html=self.REGISTRATION_HTML)
        self.assertEqual((result["status"], result["bert_used"]), ("low_risk", True))

    def test_news_and_fact_checks_without_operator_evidence_keep_exemption(self):
        for text, title, html in (
            ("Berita mengenai penertiban perjudian dan perlindungan masyarakat.",
             "Liputan penertiban", None),
            ("Cek fakta tentang sebuah rumor perjudian yang beredar di masyarakat.",
             "Periksa fakta", None),
            (self.REGISTRATION_TEXT, self.REGISTRATION_TITLE,
             '<footer><a href="/join">Daftar</a></footer>'),
            ("Nasional daerah cegah judi online demi perlindungan seluruh masyarakat.",
             "Kegiatan warga", None),
            ("Ekonomi opini hukum dalam pembahasan kebijakan oleh warga setempat.",
             "Kegiatan warga", None),
        ):
            with self.subTest(title=title, text=text):
                result = self.classify(text, title, html=html)
                self.assertEqual((result["status"], result["bert_used"]), ("low_risk", False))


if __name__ == "__main__":
    unittest.main()
