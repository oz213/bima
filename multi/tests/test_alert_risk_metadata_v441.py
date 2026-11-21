"""v4.4.1 regression: alert evidence capture must not erase HTTP risk.

Local temporary SQLite only. No URLs are fetched and no BIMA alert is sent.
"""
import json
from pathlib import Path
import tempfile
import unittest

import crawler_multi as cm


class AlertRiskMetadataTests(unittest.TestCase):
    def test_http_medium_alert_preserves_http_classification(self):
        original = {'status': 'medium_risk',
                    'reason': 'contextual_gambling_phrase_detected',
                    'ml_pred': 'gambling'}
        result = {'status': 'pending_alert',
                  'reason': 'legacy_http_classifier_alert',
                  'browser_ms': 11, 'confidence': 0.73}
        updates = cm.build_browser_result_updates(result)
        self.assertNotIn('risk', updates)
        self.assertNotIn('classifier_json', updates)
        self._assert_db_preserved(original, result, updates)

    def test_http_high_alert_preserves_http_classification(self):
        original = {'status': 'high_risk', 'reason': 'ml_strong_with_real_gambling_context'}
        result = {'status': 'pending_alert',
                  'reason': 'legacy_http_classifier_alert',
                  'browser_ms': 10, 'confidence': 0.90}
        updates = cm.build_browser_result_updates(result)
        self._assert_db_preserved(original, result, updates)

    def test_hidden_link_evidence_not_overwritten_by_browser_screenshot(self):
        original = {'status': 'hidden_link_evidence',
                    'reason': 'hidden_link_http', 'bert_used': False}
        result = {'status': 'pending_alert',
                  'reason': 'legacy_http_hidden_alert',
                  'browser_ms': 12, 'confidence': 0.60}
        updates = cm.build_browser_result_updates(result)
        self._assert_db_preserved(original, result, updates)

    def test_selenium_classifier_replaces_http_result(self):
        browser = {'status': 'high_risk',
                   'reason': 'ml_and_real_gambling_context',
                   'http_classification': {'status': 'low_risk',
                                           'reason': 'low_signal'}}
        result = {'status': 'pending_alert',
                  'reason': 'legacy_browser_classifier_alert',
                  'browser_classification': browser, 'confidence': 0.85}
        updates = cm.build_browser_result_updates(result)
        self.assertEqual(updates['risk'], 'high_risk')
        self.assertEqual(json.loads(updates['classifier_json']), browser)

    def test_inconclusive_browser_error_still_clears_stale_risk(self):
        result = {'status': 'inconclusive', 'reason': 'browser_error',
                  'error': 'simulated browser failure'}
        updates = cm.build_browser_result_updates(result)
        self.assertIn('risk', updates)
        self.assertIsNone(updates['risk'])

    def _assert_db_preserved(self, original, result, updates):
        with tempfile.TemporaryDirectory() as tmp:
            db = cm.connect_db(str(Path(tmp) / 'metadata_test.sqlite3'))
            try:
                with db:
                    db.execute("""INSERT INTO jobs(url, status, risk, classifier_json)
                                  VALUES (?, ?, ?, ?)""",
                               ('https://example.id', 'browser_queued',
                                original['status'], json.dumps(original)))
                job_id = db.execute('SELECT id FROM jobs').fetchone()[0]
                cm.set_status(db, job_id, result['status'], **updates)
                saved_risk, raw = db.execute(
                    'SELECT risk, classifier_json FROM jobs WHERE id=?',
                    (job_id,)).fetchone()
                self.assertEqual(saved_risk, original['status'])
                self.assertEqual(json.loads(raw), original)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
