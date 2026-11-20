"""Offline regression tests: classification + BIMA decisions come from crawler_url.py."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import crawler_multi as cm


class LegacyEngine:
    MIN_ALERT_CONFIDENCE = 0.60
    ANTIBOT_MAX_WAIT_SECONDS = 10
    def __init__(self, result_status='low_risk'):
        self.result_status = result_status
        self.legacy_calls = []
        self.bert_calls = 0
        self.browser_text_calls = 0
    def is_captcha_or_antibot_page(self, text, title): return False
    def is_gambling_content_indobert(self, text):
        self.bert_calls += 1
        return True, 0.98489, 'gambling'
    def classify_text_content(self, source, url, page_text, page_title, current_url):
        self.legacy_calls.append((source, url, current_url))
        self.is_gambling_content_indobert(page_text)
        return {'status':self.result_status, 'reason':'legacy_decision',
                'ml_conf':0.98489, 'ml_pred':'gambling'}
    def selenium_get_with_scheme_fallback(self, driver, url): return url
    def wait_page_loaded(self, driver, timeout=15): return None
    def get_page_text(self, driver):
        self.browser_text_calls += 1
        return 'rendered javascript gambling content' * 10
    def choose_alert_url_for_id_endpoint(self, original_url, current_url=None):
        return original_url


class Browser:
    page_source = '<html><title>example</title></html>'
    current_url = 'https://zinergi.id/'
    title = 'Site'
    def quit(self): pass


class LegacyParity(unittest.TestCase):
    def _run_browser(self, http_cls, engine, hidden=False):
        with tempfile.TemporaryDirectory() as temp:
            tasks, results = cm.queue.Queue(), cm.queue.Queue()
            tasks.put({'id':1,'url':'http://zinergi.id',
                       'http_classification':http_cls,
                       'http_result':{'ok':True,
                                     'hidden_gambling_hit':hidden,
                                     'hidden_gambling_evidence':[{'href':'https://bad.invalid'}] if hidden else [],
                                     'final_url':'https://zinergi.id/'}})
            tasks.put(None)
            args=SimpleNamespace(browser_restart_every=100,browser_max_age=900,evidence_dir=temp)
            with mock.patch.object(cm,'create_browser_driver',return_value=(Browser(),None)), \
                 mock.patch.object(cm,'validate_public_target',return_value=True), \
                 mock.patch.object(cm,'evidence_screenshot',return_value=b'\x89PNG\r\n\x1a\nTEST') as cap:
                cm.browser_loop(engine,tasks,results,args)
            return results.get_nowait(), cap.call_args

    def test_legacy_result_is_authoritative_even_with_high_bert(self):
        eng=LegacyEngine(result_status='low_risk')
        cls=cm.classify_shared(eng,'http://zinergi.id','http://zinergi.id',
                               'deposit jackpot rtp link alternatif ' * 12, 'Title')
        self.assertEqual(cls['status'],'low_risk')
        self.assertEqual(cls['reason'],'legacy_decision')
        self.assertEqual(eng.legacy_calls[0][0],'requests')
        self.assertTrue(cls['bert_used'])
        self.assertEqual(eng.bert_calls,1)

    def test_every_http_low_risk_goes_to_browser_legacy(self):
        route=cm.route_to_browser(None,'http://zinergi.id',
                 {'ok':True,'text':'safe content'*100,'hidden_gambling_hit':False},
                 {'status':'low_risk'},0)
        self.assertEqual(route,'legacy_selenium_followup')

    def test_legacy_http_positive_sends_even_without_browser_reclassifying(self):
        eng=LegacyEngine('low_risk')
        result, shot=self._run_browser({'status':'high_risk','ml_conf':0.98489},eng)
        self.assertEqual(result['status'],'pending_alert')
        self.assertEqual(result['confidence'],0.98489)
        self.assertTrue(result['screenshot_png'].startswith(b'\x89PNG'))
        self.assertEqual(eng.browser_text_calls,0)
        self.assertEqual(eng.legacy_calls,[])
        self.assertEqual(shot.args[3],[])

    def test_hidden_evidence_has_original_priority_and_confidence(self):
        eng=LegacyEngine('low_risk')
        result, shot=self._run_browser({'status':'low_risk','ml_conf':0},eng,hidden=True)
        self.assertEqual(result['status'],'pending_alert')
        self.assertEqual(result['confidence'],0.60)
        self.assertTrue(shot.args[2])
        self.assertEqual(eng.legacy_calls,[])

    def test_browser_positive_only_from_original_legacy_classifier(self):
        eng=LegacyEngine('high_risk')
        result, shot=self._run_browser({'status':'low_risk','ml_conf':0},eng)
        self.assertEqual(result['status'],'pending_alert')
        self.assertEqual(result['reason'],'legacy_browser_classifier_alert')
        self.assertEqual(eng.legacy_calls[0][0],'selenium')
        self.assertEqual(shot.args[3],[])

    def test_browser_low_risk_stays_complete_without_alert(self):
        eng=LegacyEngine('low_risk')
        result, shot=self._run_browser({'status':'low_risk','ml_conf':0},eng)
        self.assertEqual(result['status'],'complete')
        self.assertIsNone(shot)
