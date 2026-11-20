"""Offline regression checks for the original crawler_url.py classifier.

Runs selected AST functions only, without importing live configs, ML weights,
Chrome, network clients, or BIMA credentials.
Usage: python3 test_classifier_rule.py /home/fauzi/crawler/crawler_url.py
"""
import ast
import re
import sys
import unittest
from pathlib import Path

source_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'crawler_url.py')
tree = ast.parse(source_path.read_text(encoding='utf-8'), filename=str(source_path))
required = {
    'normalize_text_for_match', 'has_context_terms',
    'has_real_gambling_context', 'has_safe_product_or_industrial_context',
    'has_safe_news_or_factcheck_context', 'has_contextual_gambling_phrase',
    'find_keyword_hits', 'has_strong_operator_gambling_promo',
    'classify_risk',
}
functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in required]
assert set(n.name for n in functions) == required, required - set(n.name for n in functions)
globals_ = dict(re=re, INDO_BERT_THRESHOLD=0.80, INDO_BERT_STRONG_THRESHOLD=0.92,
                ML_THRESHOLD=0.55, ML_STRONG_THRESHOLD=0.90,
                REQUIRE_GAMBLING_CONTEXT_FOR_ML=True)
for n in tree.body:
    if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STRONG_TEXT_KEYWORDS' for t in n.targets):
        globals_['STRONG_TEXT_KEYWORDS'] = ast.literal_eval(n.value)
assert 'STRONG_TEXT_KEYWORDS' in globals_
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source_path), 'exec'), globals_)

TITLE = 'TURBO128 : Tambah Kecepatan Spin - Main Sat Set Menang Cepet'
TEXT = ('TURBO128 Login Daftar TURBO128 Official Resmi Terpercaya Terbaru '
        'LINK ALTERNATIF sepeda semua sepeda gunung software download '
        'deposit jackpot rtp')

class Regression(unittest.TestCase):
    def classify(self, title=TITLE, text=TEXT, confidence=0.984890401363373,
                 prediction='gambling', source='indobert'):
        return globals_['classify_risk'](page_text=text, page_title=title,
                ml_hit=True, ml_conf=confidence, ml_pred=prediction,
                captcha_hit=False, ml_source=source)

    def test_zinergi_sample_strong_promo_is_high_risk(self):
        risk, reason, hits = self.classify()
        self.assertEqual(risk,'high_risk')
        self.assertEqual(reason,'strong_operator_gambling_promotion')
        self.assertTrue({'deposit','jackpot','rtp','link alternatif'}.issubset(set(hits)))

    def test_no_operator_title_keeps_safe_product_guard(self):
        self.assertEqual(self.classify(title='Industrial Software: Spin Motor Product')[0], 'low_risk')

    def test_moderate_confidence_keeps_safe_product_guard(self):
        self.assertEqual(self.classify(confidence=0.88)[0], 'low_risk')

    def test_no_gambling_prediction_keeps_safe_product_guard(self):
        self.assertEqual(self.classify(prediction='non-gambling')[0], 'low_risk')

    def test_missing_promotional_keywords_keeps_safe_guard(self):
        self.assertEqual(self.classify(text=TEXT.replace('deposit jackpot rtp','information'))[0], 'low_risk')

    def test_captcha_remains_blocked(self):
        result = globals_['classify_risk'](TEXT, True, 0.99,
                    ml_pred='gambling', captcha_hit=True,
                    ml_source='indobert',page_title=TITLE)
        self.assertEqual(result[0],'blocked_or_captcha')

    def test_engine_passes_page_title_to_risk_classifier(self):
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'classify_text_content')
        calls = [n for n in ast.walk(function) if isinstance(n,ast.Call)
                 and isinstance(n.func,ast.Name) and n.func.id=='classify_risk']
        self.assertEqual(len(calls),1)
        kwargs={k.arg: k.value for k in calls[0].keywords}
        self.assertIsInstance(kwargs.get('page_title'),ast.Name)
        self.assertEqual(kwargs['page_title'].id,'page_title')

if __name__ == '__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Regression)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
