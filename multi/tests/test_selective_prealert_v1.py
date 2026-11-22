"""Offline regression tests for selective screenshot review and BIMA dispatch.

No live Telegram calls, no crawling, no BIMA API, no IndoBERT initialization.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from telegram_checks import CHECK_SCHEMA
import selective_prealert as pa

PNG = pa.PNG_HEADER + b'SYNTHETIC_PNG_EVIDENCE'


class SelectivePrealertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'crawler.sqlite3')
        self.cfg = Path(self.tmp.name) / 'config.json'
        self.cfg.write_text(json.dumps({
            'ENDPOINT_URL': 'https://bima.example/alert',
            'X_API_KEY': 'test-key-not-a-secret',
            'MIN_ALERT_CONFIDENCE': 0.60,
            'ALERT_CONNECT_TIMEOUT': 1,
            'ALERT_READ_TIMEOUT': 1,
        }))
        self.env = mock.patch.dict(os.environ, {
            'BIMA_PREALERT_ENABLED': '1',
            'BIMA_PREALERT_REVIEWER_CHAT_ID': '42',
            'TELEGRAM_REVIEW_USER_IDS': '42',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript('''
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL,
              status TEXT, classifier_json TEXT, risk TEXT, reason TEXT,
              final_url TEXT, updated_at TEXT);
            ''')
        # Mirror crawler_multi.connect_db(): production always creates this table.
        # Without it, selective_prealert.save_results fails in the test fixture.
        self.conn.executescript(CHECK_SCHEMA)
        self.conn.executescript(pa.PREALERT_SCHEMA)
        self.addCleanup(self.conn.close)
        self.next_job = 1

    def job(self, *, reason='legacy_http_hidden_alert', risk='hidden_link_evidence',
            confidence=0.60, model=None):
        jid = self.next_job
        self.next_job += 1
        url = f'https://site{jid}.id/'
        self.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                          (jid,url,'pending_alert',json.dumps(model or {}),risk,reason,
                           'https://landing.example/path',pa.utc_now()))
        self.conn.commit()
        return jid,url,confidence

    def add(self, **kwargs):
        jid,url,confidence = self.job(**kwargs)
        selected = pa.enqueue_candidate(self.conn,jid,url,confidence,PNG)
        return jid,selected

    def state(self, table, jid):
        return self.conn.execute(f'SELECT status FROM {table} WHERE job_id=?' if table=='bima_prealerts'
                                 else f'SELECT status FROM {table} WHERE id=?', (jid,)).fetchone()[0]

    def upload(self, token, chat, image, caption, reply):
        self.assertEqual(token,'test-token')
        self.assertEqual(chat,'42')
        self.assertEqual(image,PNG)
        self.assertIn('VERIFIKASI PRE-ALERT',caption)
        self.assertIn('Judol - kirim BIMA',str(reply))
        return {'message_id': 777, 'document': {'file_id':'telegram-file-id-777'}}

    def deliver(self):
        return pa.deliver_one('test-token',self.db,{'42'},uploader=self.upload)

    def decide(self, cid, action, chat='42', sender='42', allowed=None, chat_type='private'):
        return pa.decide(self.db,f'pa:{cid}:{action}',chat,sender,
                         {'42'} if allowed is None else allowed,chat_type)

    def test_feature_off_keeps_legacy_path(self):
        jid,url,conf = self.job()
        with mock.patch.dict(os.environ, {'BIMA_PREALERT_ENABLED':'0'}):
            self.assertFalse(pa.enqueue_candidate(self.conn,jid,url,conf,PNG))
        self.assertEqual(self.state('jobs',jid),'pending_alert')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0],0)

    def test_selected_case_completes_waiting_telegram_check(self):
        # A /cek request waiting for this job must be marked as awaiting review,
        # rather than remaining indefinitely pending after pre-alert selection.
        jid, url, confidence = self.job()
        self.conn.execute(
            'INSERT INTO telegram_checks(update_id,job_id,chat_id) VALUES(?,?,?)',
            (77, jid, '42'))
        self.conn.commit()
        self.assertTrue(pa.enqueue_candidate(self.conn, jid, url, confidence, PNG))
        row = self.conn.execute(
            'SELECT result,risk,reason FROM telegram_checks WHERE job_id=?',
            (jid,)).fetchone()
        self.assertEqual(row, ('awaiting_review', 'hidden_link_evidence',
                               'telegram_prealert_pending'))

    def test_eligible_candidate_is_durable_and_screenshot_owner_only(self):
        jid,selected = self.add()
        self.assertTrue(selected)
        self.assertEqual(self.state('jobs',jid),'awaiting_review')
        self.assertEqual(self.state('bima_prealerts',jid),'pending_telegram')
        name,digest = self.conn.execute('SELECT image_filename,evidence_sha256 FROM bima_prealerts').fetchone()
        self.assertEqual(hashlib.sha256(PNG).hexdigest(),digest)
        image = Path(self.tmp.name)/'prealert_evidence'/name
        self.assertEqual(image.read_bytes(),PNG)
        if os.name == 'posix':
            self.assertEqual(image.stat().st_mode & 0o777,0o600)
            self.assertEqual(image.parent.stat().st_mode & 0o777,0o700)

    def test_sample_only_reviews_small_fraction_of_medium_candidates(self):
        results = []
        for i in range(60):
            jid,selected = self.add(reason='legacy_browser_classifier_alert',
                                    risk='medium_risk',confidence=0.67)
            results.append(selected)
        self.assertLessEqual(sum(results),1)
        self.assertGreater(sum(1 for x in results if x is False),0)

    def test_disagreement_has_separate_quota_from_strong(self):
        mismatch={'indobert_pred':'non-gambling','ml_pred':'gambling'}
        selected = []
        for _ in range(4):
            selected.append(self.add(reason='legacy_http_classifier_alert',risk='high_risk',
                                     confidence=0.97,model=mismatch)[1])
        self.assertEqual(selected,[True,True,True,False])
        for _ in range(6):
            self.assertTrue(self.add()[1])
        self.assertFalse(self.add()[1])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0],9)

    def test_selected_reentry_is_idempotent(self):
        jid,url,conf = self.job()
        self.assertTrue(pa.enqueue_candidate(self.conn,jid,url,conf,PNG))
        self.assertTrue(pa.enqueue_candidate(self.conn,jid,url,conf,PNG))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealerts').fetchone()[0],1)

    def test_telegram_screenshot_and_approval_dispatch_original_wire_contract(self):
        jid,_ = self.add()
        self.assertTrue(self.deliver())
        cid,chat,file_id = self.conn.execute('SELECT id,telegram_chat_id,telegram_file_id FROM bima_prealerts').fetchone()
        self.assertEqual((chat,file_id),('42','telegram-file-id-777'))
        self.assertEqual(self.state('bima_prealerts',jid),'awaiting_review')
        self.assertIn('disetujui',self.decide(cid,'yes'))
        self.assertEqual(self.state('jobs',jid),'review_approved')
        payloads=[]
        def sender(payload,cfg):
            payloads.append(payload)
            self.assertEqual(cfg['X_API_KEY'],'test-key-not-a-secret')
            return True
        result = pa.dispatch_one(self.db,sender=sender,config_path=str(self.cfg))
        self.assertEqual(result,{'id':cid,'ok':True})
        self.assertEqual(self.state('jobs',jid),'alerted')
        self.assertEqual(self.state('bima_prealerts',jid),'sent')
        self.assertEqual(len(payloads),1)
        self.assertEqual(payloads[0]['url'],f'https://site{jid}.id/')
        self.assertEqual(payloads[0]['category'],['online gambling'])
        self.assertEqual(payloads[0]['confidence_level'],60)
        self.assertTrue(payloads[0]['screenshots'][0].startswith('data:image/png;base64,'))
        self.assertIsNone(pa.dispatch_one(self.db,sender=sender,config_path=str(self.cfg)))
        self.assertEqual(len(payloads),1)
        self.assertEqual(list((Path(self.tmp.name)/'prealert_evidence').glob('*.png')),[])

    def test_rejection_keeps_learning_label_and_never_dispatches(self):
        jid,_ = self.add()
        self.deliver()
        cid=self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]
        self.assertIn('ditolak',self.decide(cid,'no'))
        self.assertEqual(self.state('jobs',jid),'review_rejected')
        self.assertEqual(self.conn.execute('SELECT feedback_label FROM bima_prealerts').fetchone()[0],
                         'non_gambling')
        send=mock.Mock(side_effect=AssertionError('BIMA must not be called'))
        self.assertIsNone(pa.dispatch_one(self.db,sender=send,config_path=str(self.cfg)))
        send.assert_not_called()
        self.assertEqual(list((Path(self.tmp.name)/'prealert_evidence').glob('*.png')),[])

    def test_defer_keeps_screenshot_without_bima_alert(self):
        jid,_ = self.add()
        self.deliver()
        cid=self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]
        self.assertIn('ditunda',self.decide(cid,'defer'))
        self.assertEqual(self.state('jobs',jid),'review_needs_evidence')
        self.assertEqual(self.state('bima_prealerts',jid),'needs_review')
        self.assertIsNone(pa.dispatch_one(self.db,sender=mock.Mock(),config_path=str(self.cfg)))
        self.assertEqual(len(list((Path(self.tmp.name)/'prealert_evidence').glob('*.png'))),1)

    def test_only_authorized_private_reviewer_can_decide(self):
        jid,_ = self.add()
        self.deliver()
        cid=self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]
        for chat,sender,chat_type in [('42','99','private'),('-42','42','group'),
                                      ('42','42','supergroup')]:
            self.assertIn('Hanya reviewer',self.decide(cid,'yes',chat=chat,sender=sender,
                                                       chat_type=chat_type))
        self.assertEqual(self.state('bima_prealerts',jid),'awaiting_review')

    def test_duplicate_button_click_has_no_additional_bima_send(self):
        jid,_ = self.add()
        self.deliver()
        cid=self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]
        self.decide(cid,'yes')
        self.assertIn('sudah berstatus approved',self.decide(cid,'no'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealert_events '
                         'WHERE event_type LIKE ?', ('human_%',)).fetchone()[0],1)
        self.assertEqual(self.state('bima_prealerts',jid),'approved')

    def test_unknown_post_is_never_automatically_retried(self):
        jid,_ = self.add()
        self.deliver()
        cid=self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]
        self.decide(cid,'yes')
        sender=mock.Mock(side_effect=TimeoutError('synthetic endpoint timeout'))
        self.assertEqual(pa.dispatch_one(self.db,sender=sender,config_path=str(self.cfg)),
                         {'id':cid,'ok':False})
        self.assertEqual(self.state('bima_prealerts',jid),'send_unknown')
        self.assertEqual(self.state('jobs',jid),'prealert_send_unknown')
        self.assertIsNone(pa.dispatch_one(self.db,sender=sender,config_path=str(self.cfg)))
        sender.assert_called_once()

    def test_telegram_failure_keeps_queue_and_never_sends_bima(self):
        jid,_=self.add()
        def broken(*args):
            raise TimeoutError('synthetic telegram timeout')
        self.assertFalse(pa.deliver_one('token',self.db,{'42'},uploader=broken))
        self.assertEqual(self.state('bima_prealerts',jid),'pending_telegram')
        self.assertEqual(self.state('jobs',jid),'awaiting_review')
        self.assertFalse(pa.deliver_one('token',self.db,{'42'},uploader=broken))

    def test_screenshot_tamper_blocks_telegram_and_bima(self):
        jid,_=self.add()
        name=self.conn.execute('SELECT image_filename FROM bima_prealerts').fetchone()[0]
        (Path(self.tmp.name)/'prealert_evidence'/name).write_bytes(PNG+b'TAMPER')
        self.assertFalse(self.deliver())
        self.assertEqual(self.state('jobs',jid),'awaiting_review')


if __name__ == '__main__':
    unittest.main()
