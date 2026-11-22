"""BIMA Stage-1 delivery audit/review safety: offline, no API calls or crawler.

Uses a disposable synthetic SQLite DB; never touches production state or images.
"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from telegram_checks import CHECK_SCHEMA
import selective_prealert as pa

PNG = pa.PNG_HEADER + b'SYNTHETIC_TEST_ONLY'


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'synthetic.sqlite3')
        self.config = Path(self.tmp.name) / 'private.json'
        self.config.write_text(json.dumps({
            'ENDPOINT_URL': 'https://bima.example/alert',
            'X_API_KEY': 'FAKE_TEST_KEY_PRIVATE',
            'MIN_ALERT_CONFIDENCE': 0.6,
        }))
        self.env = mock.patch.dict(os.environ, {
            'BIMA_PREALERT_ENABLED': '1',
            'BIMA_PREALERT_REVIEWER_CHAT_ID': '42',
            'TELEGRAM_REVIEW_USER_IDS': '42',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL,
              status TEXT, classifier_json TEXT, risk TEXT, reason TEXT,
              final_url TEXT, updated_at TEXT);
        ''')
        self.conn.executescript(CHECK_SCHEMA)
        self.conn.executescript(pa.PREALERT_SCHEMA)
        self.conn.execute('''INSERT INTO jobs
            VALUES(1,'https://synthetic.id/','pending_alert','{}',
            'hidden_link_evidence','legacy_http_hidden_alert',
            'https://synthetic.id/',?)''', (pa.utc_now(),))
        self.conn.commit()

    def create_case(self):
        self.assertTrue(pa.enqueue_candidate(
            self.conn, 1, 'https://synthetic.id/', 0.85, PNG))
        self.assertTrue(pa.deliver_one(
            'test-token', self.db, {'42'}, uploader=lambda *args: {
                'message_id': 1, 'document': {'file_id': 'fake-file-id'}}))
        return self.conn.execute('SELECT id FROM bima_prealerts').fetchone()[0]

    def decision(self, cid, action):
        return pa.decide(self.db, f'pa:{cid}:{action}', '42', '42', {'42'}, 'private')

    def audit(self, cid):
        return self.conn.execute('''
            SELECT result,http_status,elapsed_ms,error_kind
            FROM bima_prealert_dispatch_audit WHERE prealert_id=?''', (cid,)).fetchone()

    def test_audit_non_200_does_not_retry(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        response = mock.Mock(status_code=409)
        session = mock.MagicMock()
        session.post.return_value = response
        session.__enter__.return_value = session
        with mock.patch('requests.Session', return_value=session):
            self.assertEqual(pa.dispatch_one(self.db, config_path=str(self.config)),
                             {'id': cid, 'ok': False})
            self.assertIsNone(pa.dispatch_one(self.db, config_path=str(self.config)))
        self.assertEqual(self.audit(cid)[0], 'send_unknown')
        self.assertEqual(self.audit(cid)[1], 409)
        self.assertGreaterEqual(self.audit(cid)[2], 0)
        self.assertEqual(self.audit(cid)[3], 'http_non_200')
        session.post.assert_called_once()
        self.assertTrue(response.close.called)
        # No secrets/response bodies/headers ever stored in audit table.
        self.assertNotIn('FAKE_TEST_KEY_PRIVATE', str(self.audit(cid)))

    def test_200_response_preserves_original_contract(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        response = mock.Mock(status_code=200)
        session = mock.MagicMock()
        session.post.return_value = response
        session.__enter__.return_value = session
        with mock.patch('requests.Session', return_value=session):
            self.assertEqual(pa.dispatch_one(self.db, config_path=str(self.config)),
                             {'id': cid, 'ok': True})
        self.assertEqual(self.audit(cid)[0:2], ('sent', 200))
        self.assertEqual(self.audit(cid)[3], None)
        self.assertFalse(list((Path(self.tmp.name) / 'prealert_evidence').glob('*.png')))
        self.assertTrue(session.post.call_args.kwargs['allow_redirects'] is False)

    def test_timeout_audit_and_no_replay(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        sender = mock.Mock(side_effect=TimeoutError('Fake token inside exception message'))
        with self.assertLogs('pandi.prealert', level='INFO') as logs:
            self.assertEqual(pa.dispatch_one(self.db, config_path=str(self.config),
                                             sender=sender), {'id':cid, 'ok':False})
        self.assertNotIn('Fake token inside exception message', '\n'.join(logs.output))
        self.assertEqual(self.audit(cid)[3], 'TimeoutError')
        self.assertIsNone(self.audit(cid)[1])
        self.assertIsNone(pa.dispatch_one(self.db, sender=sender,
                                          config_path=str(self.config)))
        sender.assert_called_once()

    def test_historic_unknown_status_reset_cannot_send_again(self):
        # v1 attempts have no new audit rows. Their attempted_at still acts as
        # a durable, conservative no-replay guard if a status is reset by mistake.
        cid = self.create_case()
        self.decision(cid, 'yes')
        self.conn.execute("UPDATE bima_prealerts SET bima_attempted_at=?,status='approved' "
                          "WHERE id=?", (pa.utc_now(), cid))
        self.conn.commit()
        sender = mock.Mock(side_effect=AssertionError('network must not be called'))
        self.assertIsNone(pa.dispatch_one(self.db, config_path=str(self.config),
                                          sender=sender))
        sender.assert_not_called()

    def test_incomplete_legacy_case_is_not_reattempted(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        self.conn.execute("UPDATE bima_prealerts SET status='sending' WHERE id=?", (cid,))
        self.conn.commit()
        sender = mock.Mock(side_effect=AssertionError('network must not be called'))
        self.assertIsNone(pa.dispatch_one(self.db, sender=sender,
                                          config_path=str(self.config)))
        sender.assert_not_called()

    def test_reopen_needs_review_and_stale_buttons_blocked(self):
        cid = self.create_case()
        self.assertIn('ditunda', self.decision(cid, 'defer'))
        self.assertIn('dimasukkan kembali', pa.reopen_review(
            self.db, f'#{cid}', '42', '42', {'42'}, 'private'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'pending_telegram')
        self.assertIn('tidak sesuai', self.decision(cid, 'yes'))
        new_reply = {}
        def upload(_token,_chat,_image,_caption,buttons):
            new_reply.update(buttons)
            self.assertIn('TINJAU ULANG', _caption)
            return {'message_id': 12, 'document': {'file_id': 'fake-review-2'}}
        self.assertTrue(pa.deliver_one('token', self.db, {'42'}, uploader=upload))
        callback = new_reply['inline_keyboard'][0][0]['callback_data']
        self.assertRegex(callback, r'^pa:\d+:[0-9a-f]{12}:yes$')
        self.assertIn('tidak sesuai', self.decision(cid, 'yes'))
        self.assertIn('disetujui', pa.decide(self.db,callback,'42','42',{'42'},'private'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'approved')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealert_events '
                                           'WHERE event_type=?',
                                           ('review_reopened_manual',)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bima_prealert_dispatch_audit').fetchone()[0], 0)

    def test_reopen_rejects_unauthorized_and_unknown(self):
        cid = self.create_case()
        self.decision(cid, 'defer')
        self.assertIn('Hanya reviewer', pa.reopen_review(
            self.db, f'#{cid}', '-42', '42', {'42'}, 'group'))
        self.assertIn('Kasus tidak ditemukan', pa.reopen_review(
            self.db, '#99999', '42', '42', {'42'}, 'private'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'needs_review')

    def test_reopen_cannot_change_unknown_delivery(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        pa.dispatch_one(self.db, config_path=str(self.config), sender=lambda *_: False)
        self.assertIn('hanya needs_review', pa.reopen_review(
            self.db, f'#{cid}', '42', '42', {'42'}, 'private'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'send_unknown')

    def test_reopen_rejects_modified_evidence(self):
        cid = self.create_case()
        self.decision(cid, 'defer')
        image = self.conn.execute('SELECT image_filename FROM bima_prealerts').fetchone()[0]
        (Path(self.tmp.name) / 'prealert_evidence' / image).write_bytes(PNG + b'ALTERED')
        self.assertIn('hilang/berubah', pa.reopen_review(
            self.db, f'#{cid}', '42', '42', {'42'}, 'private'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'needs_review')

    def test_existing_schema_upgrades_additively(self):
        self.conn.execute('DROP TABLE bima_prealert_dispatch_audit') if self._exists_audit() else None
        self.conn.execute('ALTER TABLE bima_prealerts RENAME TO prealert_v2_test')
        # Simulate an old v1 DB schema with no nonce and pre-existing cases.
        self.conn.execute('''CREATE TABLE bima_prealerts AS
            SELECT id,job_id,url,final_url,day_utc,bucket,confidence,
            evidence_sha256,image_filename,image_bytes,status,
            notify_attempt_at,notify_attempts,telegram_chat_id,
            telegram_message_id,telegram_file_id,reviewer_id,
            feedback_label,reviewed_at,bima_attempted_at,bima_result_at,
            created_at,updated_at FROM prealert_v2_test WHERE 0''')
        self.conn.execute('DROP TABLE prealert_v2_test')
        self.conn.commit()
        with pa.db_connect(self.db) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(bima_prealerts)')}
            self.assertIn('review_nonce', cols)
            self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='bima_prealert_dispatch_audit'").fetchone())

    def test_preflight_failure_returns_no_dispatch(self):
        cid = self.create_case()
        self.decision(cid, 'yes')
        self.assertIsNone(pa.dispatch_one(
            self.db, config_path=str(self.config) + '.missing'))
        self.assertEqual(self.conn.execute('SELECT status FROM bima_prealerts').fetchone()[0],
                         'approved')
        self.assertIsNone(self.audit(cid))

    def test_triage_reader_never_changes_database(self):
        import runpy
        tool = Path(__file__).resolve().parents[2] / 'tools' / 'bima_prealert_triage.py'
        triage = runpy.run_path(str(tool))
        cid = self.create_case()
        self.decision(cid, 'yes')
        pa.dispatch_one(self.db, config_path=str(self.config), sender=lambda *_: False)
        before = self.conn.total_changes
        rows = triage['inspect'](self.db, cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['case'], cid)
        self.assertEqual(rows[0]['status'], 'send_unknown')
        self.assertEqual(rows[0]['audit'], 'send_unknown')
        self.assertEqual(self.conn.total_changes, before)

    def _exists_audit(self):
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='bima_prealert_dispatch_audit'").fetchone())


if __name__ == '__main__':
    unittest.main()
