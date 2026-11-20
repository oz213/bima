"""Offline tests: no external crawling and no BIMA API calls."""
import json
import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import types

import crawler_multi as cm


class FakeEngine:
    MIN_ALERT_CONFIDENCE = 0.60
    ML_THRESHOLD = 0.75
    ENABLE_INDO_BERT = True
    ANTIBOT_MAX_WAIT_SECONDS = 10

    def __init__(self):
        self.bert_calls = 0

    def is_captcha_or_antibot_page(self, text, title):
        return False

    def is_gambling_content_indobert(self, text):
        self.bert_calls += 1
        return False, 0.01, 'non-gambling'

    def has_safe_news_or_factcheck_context(self, text):
        return 'news' in text

    def is_gambling_content_ml(self, text, threshold=0.75):
        return False, 0.01, 'non-gambling'

    def classify_risk(self, **kwargs):
        return 'low_risk', 'low_signal', []

    def classify_text_content(self, source, url, page_text, page_title, current_url):
        # Mimic legacy: safe-news returns BEFORE either model is called.
        if self.has_safe_news_or_factcheck_context(
                f'{url} {current_url} {page_title} {page_text}'):
            return {'status': 'low_risk', 'reason': 'safe_news_or_factcheck_context',
                    'ml_conf': 0.0, 'ml_pred': 'non-gambling'}
        _hit, conf, pred = self.is_gambling_content_indobert(page_text)
        return {'status': 'low_risk', 'reason': 'low_signal',
                'ml_conf': conf, 'ml_pred': pred}

    def choose_alert_url_for_id_endpoint(self, original_url, current_url=None):
        return original_url

    def __getattr__(self, name):
        if name.startswith('is_safe_'):
            return lambda text, title: False
        raise AttributeError(name)


class OfflineTests(unittest.TestCase):
    def test_id_url_sanitization(self):
        self.assertEqual(cm.normalize_input_url('example.co.id/a#frag'),
                         'https://example.co.id/a')
        self.assertIsNone(cm.normalize_input_url('example.com'))
        self.assertIsNone(cm.normalize_input_url('http://user:secret@evil.id/'))
        self.assertIsNone(cm.normalize_input_url('http://evil.id:3000/'))
        self.assertIsNone(cm.normalize_input_url('file:///etc/passwd'))

    def test_malformed_bracketed_urls_are_skipped_not_fatal(self):
        for url in ('https://domain[abc].id', 'https://[abc.id',
                    'https://domain.id:abc', 'https://domain.id:65536',
                    'https://user@domain.id', '\x00domain.id',
                    'https://domain.id\nother.id'):
            with self.subTest(url=url):
                self.assertIsNone(cm.normalize_input_url(url))

    def test_import_mixed_invalid_ipv6_urls_keeps_valid_entries(self):
        with tempfile.TemporaryDirectory() as d:
            urls = Path(d) / 'urls.json'
            urls.write_text(json.dumps({'urls': [
                'valid.id', 'https://domain[abc].id', 'two.co.id',
                'https://[abc.id', 'https://domain.id:abc',
                'three.my.id', 'not-a-dot-com.example']}), encoding='utf-8')
            conn = cm.connect_db(str(Path(d) / 'smoke.sqlite3'))
            try:
                cm.import_input_urls(conn, str(urls))
                result = [row[0] for row in conn.execute('SELECT url FROM jobs ORDER BY url')]
                self.assertEqual(result, ['https://three.my.id',
                                          'https://two.co.id', 'https://valid.id'])
            finally:
                conn.close()

    def test_block_loopback_ip(self):
        with self.assertRaisesRegex(ValueError, 'Non-public'):
            cm.validate_public_target('http://127.0.0.1')

    def test_legacy_safe_news_returns_before_bert(self):
        engine = FakeEngine()
        result = cm.classify_shared(engine, 'https://site.id', 'https://site.id',
                                    'news ' + ('word ' * 40), 'headline')
        self.assertEqual(engine.bert_calls, 0)
        self.assertEqual(result['reason'], 'safe_news_or_factcheck_context')
        self.assertFalse(result['bert_used'])

    def test_shared_bert_called_once_on_regular_page(self):
        engine = FakeEngine()
        result = cm.classify_shared(engine, 'https://site.id', 'https://site.id',
                                    'normal content ' * 20, 'home')
        self.assertEqual(engine.bert_calls, 1)
        self.assertEqual(result['status'], 'low_risk')

    def test_browser_routing(self):
        engine = FakeEngine()
        pre = {'ok': True, 'html': '<body>static</body>', 'text': 'static content '*50}
        cls = {'status': 'low_risk'}
        self.assertEqual(cm.route_to_browser(engine,'https://x.id',pre,cls,0),
                         'legacy_selenium_followup')
        self.assertEqual(cm.route_to_browser(engine,'https://x.id',pre,cls,1),
                         'legacy_selenium_followup')
        cls['status'] = 'high_risk'
        self.assertEqual(cm.route_to_browser(engine,'https://x.id',pre,cls,0), 'risky_http')

    def test_db_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            urls = Path(d)/'urls.json'
            urls.write_text(json.dumps({'urls': ['a.id','a.id','b.id','x.com']}))
            conn = cm.connect_db(str(Path(d)/'state.sqlite'))
            try:
                cm.import_input_urls(conn, str(urls))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
                id_ = conn.execute('SELECT id FROM jobs LIMIT 1').fetchone()[0]
                cm.set_status(conn,id_,'complete',reason='done')
                self.assertEqual(conn.execute('SELECT status FROM jobs WHERE id=?',(id_,)).fetchone()[0],
                                 'complete')
            finally:
                conn.close()

    def test_legacy_http_positive_alerts_without_browser_reclassification(self):
        with tempfile.TemporaryDirectory() as d:
            class Browser:
                page_source = '<html>this is a normal site</html>'
                current_url = 'https://a.id/'
                def quit(self):
                    pass
            class BrowserEngine(FakeEngine):
                def selenium_get_with_scheme_fallback(self, driver, url):
                    return url
                def wait_page_loaded(self, driver, timeout=12):
                    return None
                def extract_text_from_html(self, html, page_url=None):
                    return 'normal site content ' * 20, 'Normal', False, [], []
            args = types.SimpleNamespace(
                browser_restart_every=100, browser_max_age=900,
                evidence_dir=d)
            tasks = cm.queue.Queue()
            results = cm.queue.Queue()
            tasks.put({'id':1,'url':'https://a.id/',
                       'http_classification':{'status':'high_risk','ml_conf':0.97},
                       'http_result':{'hidden_gambling_hit':False}})
            tasks.put(None)
            with (mock.patch.object(cm,'create_browser_driver',return_value=(Browser(),'/tmp/no-file')),
                  mock.patch.object(cm,'validate_public_target',return_value=True),
                  mock.patch.object(cm,'evidence_screenshot',return_value=b"\x89PNG\r\n\x1a\nIMG")):
                cm.browser_loop(BrowserEngine(),tasks,results,args)
            result = results.get_nowait()
            self.assertEqual(result['status'],'pending_alert')
            self.assertEqual(result['reason'],'legacy_http_classifier_alert')
            self.assertEqual(result['confidence'],0.97)
            self.assertIsInstance(result['screenshot_png'], bytes)

    def test_failed_alert_retains_state_but_not_screenshot(self):
        with tempfile.TemporaryDirectory() as d:
            conn = cm.connect_db(str(Path(d)/'state.sqlite3'))
            with conn:
                conn.execute('INSERT INTO jobs(url,status,alert_confidence) VALUES (?,?,?)',
                             ('https://alert.id', 'pending_alert', 0.8))
            id_ = conn.execute('SELECT id FROM jobs').fetchone()[0]
            png = b'\x89PNG\r\n\x1a\nFAKE'
            class Sender:
                def __init__(self): self.calls = 0
                def send_alert(self, url, screenshot_png_b64, confidence):
                    self.calls += 1
                    return False
            sender = Sender()
            self.assertFalse(cm.send_pending_alert(sender, conn, id_,
                          'https://alert.id', 0.8, png, False))
            row = conn.execute('SELECT status,evidence_path FROM jobs WHERE id=?', (id_,)).fetchone()
            self.assertEqual(row, ('pending_alert', None))
            cm.requeue_pending_alerts(conn, enabled=False)
            self.assertEqual(sender.calls, 1)
            self.assertEqual(conn.execute('SELECT status FROM jobs WHERE id=?',
                                          (id_,)).fetchone()[0], 'pending_alert')
            cm.requeue_pending_alerts(conn, enabled=True)
            self.assertEqual(conn.execute('SELECT status FROM jobs WHERE id=?',
                                          (id_,)).fetchone()[0], 'pending')
            self.assertEqual(sender.calls, 1)  # Re-fetch required; not blind resend.
            self.assertFalse(list(Path(d).glob('*.png')))
            conn.close()

    def test_screenshot_uses_exact_legacy_two_line_watermark_in_memory(self):
        from PIL import Image
        page = Image.new('RGB', (420, 280), color='white')
        buf = io.BytesIO()
        page.save(buf, format='PNG')
        page.close()
        png = buf.getvalue()
        events = []
        class ScreenshotEngine:
            def take_screenshot_png(self, driver):
                events.append('capture')
                return png
            def add_watermark(self, image, text1, text2):
                events.append('watermark')
                self.texts = (text1, text2)
                return image.copy()
            def inject_hidden_evidence_panel(self, driver, url, evidence):
                events.append('hidden_panel')
        engine = ScreenshotEngine()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cm, 'utcnow',
                 return_value='2026-11-07T07:11:12Z'):
            result = cm.evidence_screenshot(engine, object(), 'https://sample.id/a', [], d)
            self.assertEqual(events, ['capture', 'watermark'])
            self.assertEqual(engine.texts,
                ('Screenshot taken at 2026-11-07T07:11:12Z',
                 'from BIMA: https://sample.id/a'))
            self.assertIsInstance(result, bytes)
            self.assertTrue(result.startswith(b'\x89PNG\r\n\x1a\n'))
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.size, (420, 280))
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_hidden_screenshot_injects_legacy_panel_before_capture(self):
        from PIL import Image
        img = Image.new('RGB', (100, 100), 'white')
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        img.close()
        events = []
        class ScreenshotEngine:
            def inject_hidden_evidence_panel(self, driver, url, evidence):
                events.append(('overlay', url, evidence))
            def take_screenshot_png(self, driver):
                events.append(('capture',))
                return buf.getvalue()
            def add_watermark(self, image, line1, line2):
                events.append(('watermark', line1, line2))
                return image.copy()
        evidence = [{'href':'https://bad.example', 'matched_terms':['judi']}]
        with tempfile.TemporaryDirectory() as d:
            result = cm.evidence_screenshot(ScreenshotEngine(), object(),
                                          'http://test.id', evidence, d)
            self.assertTrue(result.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertEqual(list(Path(d).iterdir()), [])
        self.assertEqual([entry[0] for entry in events], ['overlay', 'capture', 'watermark'])
        self.assertEqual(events[0][2], evidence)

    def test_hidden_screenshot_fails_closed_if_overlay_fails(self):
        class BrokenOverlay:
            def inject_hidden_evidence_panel(self, *args):
                raise RuntimeError('overlay could not be shown')
            def take_screenshot_png(self, driver):
                self.fail('should not capture if overlay failed')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, 'overlay could not be shown'):
                cm.evidence_screenshot(BrokenOverlay(), object(),
                                       'https://sample.id', [{'href':'x'}], d)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_bima_receives_exact_in_memory_png_as_base64(self):
        with tempfile.TemporaryDirectory() as d:
            conn = cm.connect_db(str(Path(d)/'state.sqlite3'))
            data = b'\x89PNG\r\n\x1a\nEXAMPLE'
            with conn:
                conn.execute('INSERT INTO jobs(url,status) VALUES (?,?)',
                             ('https://sample.id/path', 'pending_alert'))
            id_ = conn.execute('SELECT id FROM jobs').fetchone()[0]
            class SpySender:
                def send_alert(self, url, b64, confidence):
                    self.args = (url, b64, confidence)
                    return True
            sender = SpySender()
            self.assertTrue(cm.send_pending_alert(sender, conn, id_,
                                                 'https://sample.id/path',
                                                 0.80, data, False))
            self.assertEqual(sender.args[0], 'https://sample.id/path')
            self.assertEqual(base64.b64decode(sender.args[1]), data)
            self.assertEqual(sender.args[2], 0.80)
            self.assertEqual(conn.execute('SELECT status,evidence_path FROM jobs').fetchone(),
                             ('alerted', None))
            self.assertEqual(list(Path(d).glob('*.png')), [])
            conn.close()

    def test_dry_run_captures_but_never_sends_or_saves(self):
        with tempfile.TemporaryDirectory() as d:
            conn = cm.connect_db(str(Path(d)/'state.sqlite3'))
            with conn:
                conn.execute('INSERT INTO jobs(url,status) VALUES (?,?)',
                             ('https://sample.id/path', 'pending_alert'))
            id_ = conn.execute('SELECT id FROM jobs').fetchone()[0]
            class SpySender:
                def send_alert(self, *args):
                    raise AssertionError('dry run must not call BIMA')
            self.assertTrue(cm.send_pending_alert(SpySender(), conn, id_,
                             'https://sample.id/path', 0.99,
                             b'\x89PNG\r\n\x1a\nIMAGE', True))
            self.assertEqual(conn.execute('SELECT status,evidence_path FROM jobs').fetchone(),
                             ('dry_run_alert', None))
            self.assertEqual(list(Path(d).glob('*.png')), [])
            conn.close()

    def test_dns_error_classification_by_errno(self):
        cases = [
            (-3, 'Temporary failure in name resolution', 'temporary_failure'),
            (-5, 'No address associated with hostname', 'no_address'),
            (-2, 'Name or service not known', 'not_found'),
        ]
        for err, message, expected in cases:
            with self.subTest(errno=err):
                http = {'ok': False, 'reason': 'fetch_failed',
                        'error': f'DNS lookup failed: [Errno {err}] {message}'}
                self.assertEqual(cm.classify_http_error(http), 'dns_error')
                self.assertEqual(cm.diagnose_dns_error(http), (expected, err))

    def test_dns_retry_delays(self):
        args = types.SimpleNamespace(network_retry_minutes=30,
                                     dns_no_address_retry_minutes=360,
                                     dns_not_found_retry_minutes=1440)
        self.assertEqual(cm.network_retry_delay_minutes(args, 'temporary_failure'), 30)
        self.assertEqual(cm.network_retry_delay_minutes(args, 'no_address'), 360)
        self.assertEqual(cm.network_retry_delay_minutes(args, 'not_found'), 1440)
        self.assertEqual(cm.network_retry_delay_minutes(args, 'none'), 30)

    def test_failed_dns_precheck_skips_duplicate_http_scheme(self):
        with mock.patch.object(cm, 'validate_public_target',
                               side_effect=ValueError('DNS lookup failed: [Errno -3] Temporary failure')), \
             mock.patch.object(cm, 'http_session') as session:
            args = types.SimpleNamespace(min_host_interval=0.0,
                                         http_connect_timeout=3, http_read_timeout=8,
                                         http_max_bytes=2000000)
            result = cm.fetch_http(FakeEngine(), 'https://name.id/', args)
            self.assertEqual(result['reason'], 'fetch_failed')
            self.assertEqual(cm.diagnose_dns_error(result), ('temporary_failure', -3))
            session.assert_not_called()

    def test_bert_duration_recorded_for_classified_page(self):
        engine = FakeEngine()
        result = cm.classify_shared(engine, 'https://site.id', 'https://site.id',
                                    'useful page text ' * 30, 'title')
        self.assertTrue(result['bert_used'])
        self.assertIn('bert_ms', result)
        self.assertGreaterEqual(result['bert_ms'], 0)
        self.assertEqual(engine.bert_calls, 1)

    def test_db_additive_migration_v123(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'db.sqlite3'
            old = sqlite3.connect(db)
            old.executescript("""
                CREATE TABLE jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  url TEXT UNIQUE NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  http_ms INTEGER, browser_ms INTEGER, http_status INTEGER,
                  reason TEXT, risk TEXT, classifier_json TEXT, final_url TEXT,
                  evidence_path TEXT, alert_confidence REAL, error TEXT,
                  updated_at TEXT, route_reason TEXT, error_category TEXT,
                  classification_ms INTEGER, next_retry_at TEXT
                );
                INSERT INTO jobs (url, status) VALUES ('https://keep.id', 'complete');
            """)
            old.commit()
            old.close()
            conn = cm.connect_db(str(db))
            cols = {col[1] for col in conn.execute('PRAGMA table_info(jobs)')}
            self.assertTrue({'bert_ms', 'dns_result', 'dns_errno'} <= cols)
            self.assertEqual(conn.execute('SELECT status FROM jobs WHERE url=?',
                                          ('https://keep.id',)).fetchone()[0], 'complete')
            conn.close()

    def test_dns_failure_deferred_without_browser_and_never_marked_safe(self):
        engine = FakeEngine()
        http = {'ok': False, 'reason': 'fetch_failed',
                'error': 'DNS lookup failed: [Errno -2] Name or service not known'}
        cls = {'status': 'inconclusive', 'reason': 'fetch_failed', 'bert_used': False}
        first = cm.plan_http_outcome(engine, 'https://a.id', http, cls, 0.05, 1, 3)
        self.assertEqual(first, ('retry_http', 'deferred_dns_error', 'dns_error'))
        third = cm.plan_http_outcome(engine, 'https://a.id', http, cls, 0.05, 3, 3)
        self.assertEqual(third, ('inconclusive', 'retry_exhausted_dns_error', 'dns_error'))
        self.assertEqual(engine.bert_calls, 0)

    def test_timeout_retry_and_unknown_error_still_uses_browser(self):
        engine = FakeEngine()
        cls = {'status': 'inconclusive', 'reason': 'fetch_failed'}
        timeout = {'ok': False, 'reason': 'fetch_failed', 'error': 'Read timed out.'}
        self.assertEqual(cm.plan_http_outcome(engine, 'https://a.id', timeout, cls,
                                              0, 1, 3)[0], 'retry_http')
        unknown = {'ok': False, 'reason': 'fetch_failed',
                   'error': 'Connection aborted due to unexpected condition'}
        self.assertEqual(cm.plan_http_outcome(engine, 'https://a.id', unknown, cls,
                                              0, 1, 3)[0], 'browser_queued')

    def test_import_limit_preserves_source_order_and_db_migrates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'input.json'
            path.write_text(json.dumps({'urls': ['c.id', 'bad.com', 'a.id',
                                                  'c.id', 'b.id']}))
            db = Path(d) / 'db.sqlite'
            # Simulate an existing v1.2.1 database.
            import sqlite3
            old = sqlite3.connect(db)
            old.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                        "url TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', "
                        'attempts INTEGER NOT NULL DEFAULT 0, http_ms INTEGER, browser_ms INTEGER, '
                        'http_status INTEGER, reason TEXT, risk TEXT, classifier_json TEXT, '
                        'final_url TEXT, evidence_path TEXT, alert_confidence REAL, '
                        'error TEXT, updated_at TEXT)')
            old.commit()
            old.close()
            conn = cm.connect_db(str(db))
            try:
                cm.import_input_urls(conn, str(path), import_limit=2)
                names = [x[0] for x in conn.execute('SELECT url FROM jobs ORDER BY id')]
                self.assertEqual(names, ['https://c.id', 'https://a.id'])
                columns = {x[1] for x in conn.execute('PRAGMA table_info(jobs)')}
                self.assertTrue({'route_reason', 'error_category',
                                 'classification_ms', 'next_retry_at'} <= columns)
            finally:
                conn.close()

    def test_retry_network_only_requeues_due_rows(self):
        with tempfile.TemporaryDirectory() as d:
            conn = cm.connect_db(str(Path(d) / 'db.sqlite'))
            with conn:
                conn.execute('INSERT INTO jobs(url,status,next_retry_at) VALUES (?,?,?)',
                             ('https://due.id', 'retry_http', '2020-01-01T00:00:00Z'))
                conn.execute('INSERT INTO jobs(url,status,next_retry_at) VALUES (?,?,?)',
                             ('https://future.id', 'retry_http', '2099-01-01T00:00:00Z'))
            conn.execute("UPDATE jobs SET status='pending', next_retry_at=NULL "
                         "WHERE status='retry_http' AND next_retry_at<=?", (cm.utcnow(),))
            due = conn.execute("SELECT status FROM jobs WHERE url='https://due.id'").fetchone()[0]
            future = conn.execute("SELECT status FROM jobs WHERE url='https://future.id'").fetchone()[0]
            self.assertEqual((due, future), ('pending', 'retry_http'))
            conn.close()

    def test_http_run_network_failures_are_deferred_without_alert_or_browser(self):
        with tempfile.TemporaryDirectory() as d:
            urls = Path(d) / 'urls.json'
            urls.write_text(json.dumps({'urls': ['dns.id', 'slow.id', 'ok.id']}))
            args = types.SimpleNamespace(
                engine_file='unused.py', input=str(urls), db=str(Path(d) / 'state.sqlite'),
                evidence_dir=str(Path(d) / 'evidence'), http_workers=3,
                browser_sample_rate=0, browser_queue_size=2, browser_restart_every=100,
                browser_max_age=900, limit=3, import_limit=3, dry_run=True,
                http_only=True, retry_failed=False, retry_network=False,
                network_retry_minutes=30, max_network_attempts=3, bert_batch_size=2,
                http_max_bytes=2_000_000, min_host_interval=0.3, retry_dry_run=False)
            engine = FakeEngine()
            def local_fetch(engine, url, args):
                if 'dns.id' in url:
                    return {'ok':False, 'reason':'fetch_failed', 'error':
                            'DNS lookup failed: Name or service not known',
                            'duration_ms':100, 'status_code':None}
                if 'slow.id' in url:
                    return {'ok':False, 'reason':'fetch_failed', 'error':'Read timed out.',
                            'duration_ms':500, 'status_code':None}
                return {'ok':True, 'reason':'http_ok', 'duration_ms':50,
                        'html':'<html><body>normal content here</body></html>',
                        'text':'normal content ' * 30, 'title':'normal', 'final_url':url,
                        'status_code':200, 'hidden_gambling_hit':False}
            with mock.patch.object(cm, 'load_engine', return_value=engine), \
                 mock.patch.object(cm, 'fetch_http', side_effect=local_fetch):
                cm.run(args)
            db = cm.connect_db(args.db)
            try:
                actual = dict(db.execute('SELECT url,status FROM jobs').fetchall())
                self.assertEqual(actual['https://dns.id'], 'retry_http')
                self.assertEqual(actual['https://slow.id'], 'retry_http')
                self.assertEqual(actual['https://ok.id'], 'browser_queued')
                self.assertEqual(engine.bert_calls, 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='browser_queued'").fetchone()[0], 1)
                self.assertTrue(db.execute("SELECT next_retry_at FROM jobs WHERE url='https://dns.id'").fetchone()[0])
            finally:
                db.close()

    def test_http_first_run_offline(self):
        with tempfile.TemporaryDirectory() as d:
            urls = Path(d)/'urls.json'
            urls.write_text(json.dumps({'urls': ['a.id','b.id','c.id','bad.com']}))
            args = types.SimpleNamespace(
                engine_file='unused.py',input=str(urls),db=str(Path(d)/'state.sqlite'),
                evidence_dir=str(Path(d)/'evidence'),http_workers=3,
                browser_sample_rate=0,browser_queue_size=2, browser_restart_every=100,
                browser_max_age=900,limit=0,dry_run=True,http_only=True,retry_failed=False,
                bert_batch_size=2, http_max_bytes=2_000_000, min_host_interval=0.3,
                retry_dry_run=False,import_limit=0,retry_network=False,
                network_retry_minutes=30,max_network_attempts=3)
            def local_fetch(engine, url, args):
                return {'ok':True,'reason':'http_ok','duration_ms':5,
                        'html':'<html><body>normal content here</body></html>',
                        'text':'normal content ' * 30,'title':'normal',
                        'final_url':url,'status_code':200,'hidden_gambling_hit':False}
            with mock.patch.object(cm, 'load_engine', return_value=FakeEngine()),\
                 mock.patch.object(cm, 'fetch_http', side_effect=local_fetch):
                cm.run(args)
            conn = cm.connect_db(args.db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='browser_queued'").fetchone()[0], 3)
            conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
