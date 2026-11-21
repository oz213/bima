"""Exercise the BIMA sender with real Requests retries and no socket traffic.

Only transport responses are simulated. The legacy module is not imported:
its selected functions are extracted without loading configs, models or Chrome.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import NewConnectionError, ReadTimeoutError
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

import crawler_multi as crawler


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 21, tzinfo=timezone.utc)


class UrlProcessingTimeout(Exception):
    pass


def load_sender():
    source = Path(__file__).resolve().parents[2] / 'crawler_url.py'
    names = {'requests_session', 'alert_requests_session', 'send_alert',
             'ensure_url_scheme', 'is_id_tld_host', 'is_id_tld_url'}
    tree = ast.parse(source.read_text(encoding='utf-8'))
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        'requests': requests, 'HTTPAdapter': HTTPAdapter, 'Retry': Retry,
        'datetime': FixedDatetime, 'timezone': timezone, 'urlparse': urlparse,
        'MIN_ALERT_CONFIDENCE': 0.60, 'X_API_KEY': 'synthetic-api-key',
        'ENDPOINT_URL': 'https://bima.example/alert',
        'ALERT_CONNECT_TIMEOUT': 1, 'ALERT_READ_TIMEOUT': 1,
        'ALERT_RAISE_ON_FAILURE': False, 'UrlProcessingTimeout': UrlProcessingTimeout,
        'log': lambda message: None,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    namespace['session_http'] = namespace['requests_session']()
    namespace['session_http'].trust_env = False
    return namespace


class AlertTransportTests(unittest.TestCase):
    def setUp(self):
        self.engine = load_sender()
        self.addCleanup(self.engine['session_http'].close)

    @contextmanager
    def transport(self, outcomes, close_error=False):
        """Return controlled responses below the retry/redirect implementation."""
        result = SimpleNamespace(attempts=[], sessions=[], closed=[], responses=[])
        outcomes = iter(outcomes)
        real_session = requests.Session
        real_close = requests.Session.close

        def make_session():
            session = real_session()
            # The production client retains environment proxy/CA compatibility.
            # This fixture prevents any environment proxy from opening a socket.
            session.trust_env = False
            result.sessions.append(session)
            return session

        def close_session(session):
            result.closed.append(session)
            real_close(session)
            if close_error:
                raise OSError('synthetic cleanup failure')

        def respond(pool, connection, method, url, **kwargs):
            result.attempts.append((method, pool.host, url, dict(kwargs.get('headers', {})),
                                    kwargs.get('body')))
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            status, headers = outcome if isinstance(outcome, tuple) else (outcome, {})
            response = HTTPResponse(status=status, headers=headers,
                                    body=BytesIO(b'accepted'), preload_content=False)
            result.responses.append(response)
            return response

        with (patch.object(requests, 'Session', side_effect=make_session),
              patch.object(real_session, 'close', close_session),
              patch.object(HTTPConnectionPool, '_make_request', autospec=True, side_effect=respond),
              patch('socket.socket.connect', side_effect=AssertionError('Socket I/O forbidden')),
              patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden')),
              patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'})):
            yield result

    def send(self):
        return self.engine['send_alert']('https://example.id/path', 'Zml4dHVyZQ==', 0.1)

    def test_retryable_status_does_not_replay_post(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status), self.transport([status, 200]) as result:
                sent = self.send()
                self.assertEqual(len(result.attempts), 1)
                self.assertFalse(sent)

    def test_retry_after_does_not_delay_or_replay_alert(self):
        with self.transport([(429, {'Retry-After': '3600'}), 200]) as result:
            with patch('urllib3.util.retry.time.sleep') as sleep:
                sent = self.send()
            self.assertEqual(len(result.attempts), 1)
            self.assertFalse(sent)
            sleep.assert_not_called()

    def test_read_timeout_does_not_replay_post(self):
        with self.transport([ReadTimeoutError(None, '/alert', 'synthetic read timeout'), 200]) as result:
            sent = self.send()
            self.assertEqual(len(result.attempts), 1)
            self.assertFalse(sent)

    def test_connect_failure_does_not_replay_post(self):
        with self.transport([NewConnectionError(None, 'synthetic connect failure'), 200]) as result:
            sent = self.send()
            self.assertEqual(len(result.attempts), 1)
            self.assertFalse(sent)

    def test_redirect_does_not_forward_credentials_or_screenshot(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.transport([
                    (status, {'Location': 'https://other.example/collect'}), 200]) as result:
                sent = self.send()
                self.assertEqual([(a[0], a[1]) for a in result.attempts], [('POST', 'bima.example')])
                self.assertEqual(result.attempts[0][3]['X-API-key'], 'synthetic-api-key')
                self.assertIn('data:image/png;base64,Zml4dHVyZQ==', result.attempts[0][4].decode())
                self.assertFalse(sent)

    def test_success_preserves_payload_and_http_200_contract(self):
        with self.transport([200]) as result:
            self.assertTrue(self.send())
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(json.loads(result.attempts[0][4]), {
                'url': 'https://example.id/path', 'brand': '-',
                'discovered': '2026-11-21T00:00:00Z', 'confidence_level': 60,
                'status': '0', 'source': 'crawling', 'resolved': '0',
                'category': ['online gambling'],
                'screenshots': ['data:image/png;base64,Zml4dHVyZQ=='],
            })
        for status in (201, 204):
            with self.subTest(status=status), self.transport([status]):
                self.assertFalse(self.send())

    def test_alert_session_does_not_share_crawl_cookies_and_is_closed(self):
        self.engine['session_http'].cookies.set('site_state', 'crawler-cookie', domain='bima.example')
        with self.transport([200]) as result:
            self.assertTrue(self.send())
            self.assertNotIn('Cookie', result.attempts[0][3])
            self.assertEqual(len(result.sessions), 1)
            self.assertIn(result.sessions[0], result.closed)
            self.assertNotIn(self.engine['session_http'], result.closed)
        self.assertEqual(self.engine['session_http'].cookies.get('site_state'), 'crawler-cookie')

    def test_failed_request_closes_alert_session(self):
        error = ReadTimeoutError(None, '/alert', 'synthetic timeout')
        with self.transport([error, error]) as result:
            self.assertFalse(self.send())
            self.assertEqual(len(result.sessions), 1)
            self.assertIn(result.sessions[0], result.closed)

    def test_opt_in_exception_behavior_is_preserved(self):
        self.engine['ALERT_RAISE_ON_FAILURE'] = True
        error = ReadTimeoutError(None, '/alert', 'synthetic timeout')
        with self.transport([error, error]), self.assertRaises(requests.RequestException):
            self.send()

    def test_cleanup_error_does_not_erase_confirmed_success(self):
        with self.transport([200], close_error=True) as result:
            self.assertTrue(self.send())
            self.assertEqual(len(result.sessions), 1)
            self.assertIn(result.sessions[0], result.closed)

    def test_existing_crawl_get_retry_is_preserved(self):
        with self.transport([503, 200]) as result:
            response = self.engine['session_http'].get('https://example.id/')
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual([attempt[0] for attempt in result.attempts], ['GET', 'GET'])
            finally:
                response.close()

    def test_invalid_report_or_missing_endpoint_makes_no_request(self):
        with self.transport([]) as result:
            self.assertFalse(self.engine['send_alert']('https://example.com', 'fixture', 0.8))
            self.engine['ENDPOINT_URL'] = ''
            self.assertFalse(self.send())
            self.assertEqual(result.attempts, [])

    def test_ambiguous_send_stays_pending_without_automatic_requeue(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        conn = crawler.connect_db(str(Path(temp.name) / 'jobs.sqlite3'))
        self.addCleanup(conn.close)
        with conn:
            conn.execute("INSERT INTO jobs(url,status,risk) VALUES (?, 'pending_alert','high_risk')",
                         ('https://example.id/path',))
        job_id = conn.execute('SELECT id FROM jobs').fetchone()[0]
        engine = SimpleNamespace(send_alert=self.engine['send_alert'])
        with self.transport([503, 200]) as result:
            sent = crawler.send_pending_alert(engine, conn, job_id, 'https://example.id/path',
                                              0.8, b'\x89PNG\r\n\x1a\nfixture', False)
            self.assertFalse(sent)
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(conn.execute('SELECT status FROM jobs').fetchone()[0], 'pending_alert')
            crawler.requeue_pending_alerts(conn, enabled=False)
            self.assertEqual(conn.execute('SELECT status FROM jobs').fetchone()[0], 'pending_alert')
            self.assertEqual(len(result.attempts), 1)


if __name__ == '__main__':
    unittest.main()
