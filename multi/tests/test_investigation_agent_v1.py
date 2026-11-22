"""Offline regression and safety tests for BIMA Investigation Engine v1."""
from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'investigation_agent_v1.py'
spec = importlib.util.spec_from_file_location('investigation_agent_v1', MODULE)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class InvestigationTests(unittest.TestCase):
    def test_hidden_seo_links_news_page(self):
        html = '''<!DOCTYPE html><html><head><title>Home - Sorot Hari Ini</title></head>
        <body><div style="display:none;">
        <a href="https://a.example/one" rel="dofollow">slot gacor</a>
        <a href="https://b.example/two" rel="dofollow">slot gacor</a>
        <a href="https://c.example/three" rel="dofollow">slot gacor</a>
        </div><article><h1>Berita Kota Sukabumi</h1>Berita biasa.</article></body></html>'''
        result = agent.analyse_html(html.encode(), 'https://sorothariini.id/')
        self.assertEqual(result['category'], 'hidden_gambling_seo_spam')
        self.assertEqual(result['hidden_gambling_links_count'], 3)
        self.assertEqual(result['page_title'], 'Home - Sorot Hari Ini')
        self.assertEqual(result['hidden_gambling_links_sample'][0]['hidden_method'],
                         'style:display:none')
        self.assertNotIn('Berita biasa.', json.dumps(result))

    def test_safe_news_about_gambling_is_not_an_auto_finding(self):
        html = b'<title>Polisi Tangkap Pelaku Judi Online</title><body><article>Polisi mengungkap kasus judi online. <a href="/article">Baca berita</a></article></body>'
        result = agent.analyse_html(html, 'https://berita.id/')
        self.assertEqual(result['category'], 'not_determined')
        self.assertEqual(result['assessment'], 'insufficient_evidence')

    def test_internal_hidden_related_link_is_not_external_spam(self):
        html = b'<div style="display:none"><a href="/blog/slot-gacor">slot gacor</a></div>'
        result = agent.analyse_html(html, 'https://berita.id/')
        self.assertEqual(result['hidden_gambling_links_count'], 0)

    def test_visible_external_registration_and_bonus(self):
        html = '''<html><head><title>JebolTogel - Login dan Daftar</title></head><body>
        <h1>JebolTogel</h1><a href="https://example.org/login">LINK LOGIN</a>
        <a href="https://example.org/daftar">LINK DAFTAR</a>
        <a href="https://example.org/bonus">INFO BONUS</a></body></html>'''
        result = agent.analyse_html(html.encode(), 'https://linkgue.site/jeboltogel')
        self.assertEqual(result['category'], 'suspected_gambling_promotion')
        self.assertEqual(result['outbound_promotion_links_count'], 3)

    def test_https_to_external_public_redirect_without_following_links(self):
        calls = []

        def fetch_one(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                return {'status': 302, 'location': 'https://linkgue.site/jeboltogel?key=secret', 'body': b''}
            return {'status': 200, 'location': None, 'body': b'<title>JebolTogel</title>'}

        r = agent.investigate('http://jebol.id', fetch_one=fetch_one)
        self.assertEqual(len(calls), 2)
        self.assertEqual(r['observation']['redirect_chain'], ['http://jebol.id/',
                          'https://linkgue.site/jeboltogel'])
        self.assertNotIn('secret', json.dumps(r))
        self.assertEqual(r['observation']['assessment'], 'insufficient_evidence')

    def test_prevent_url_credentials_private_port_and_non_id_initial(self):
        forbidden = ['http://127.0.0.1/', 'http://10.0.0.2/',
                     'http://example.com/', 'http://user:pwd@foo.id/',
                     'https://foo.id:8443/', 'file:///etc/passwd',
                     'http://foo.id\\@127.0.0.1/', 'http://foo.id/a b']
        for target in forbidden:
            with self.subTest(target=target), self.assertRaises(agent.InvestigationError):
                agent._validate_url(target, initial=True)

    def test_block_any_nonpublic_dns_answer(self):
        with patch.object(agent.socket, 'getaddrinfo', return_value=[
            (2, 1, 6, '', ('8.8.8.8', 443)),
            (2, 1, 6, '', ('127.0.0.1', 443))
        ]):
            with self.assertRaisesRegex(agent.InvestigationError,
                                         'blocked_nonpublic_dns_address'):
                agent._public_ip_for('foo.id', 443)

    def test_public_dns_and_pinned_choice(self):
        with patch.object(agent.socket, 'getaddrinfo', return_value=[
            (10, 1, 6, '', ('2606:4700:4700::1111', 443, 0, 0)),
            (2, 1, 6, '', ('1.1.1.1', 443))
        ]):
            self.assertEqual(agent._public_ip_for('foo.id', 443), '1.1.1.1')

    def test_redirect_loop_and_hop_limit(self):
        def forever(url, timeout):
            return {'status': 302, 'location': '/step', 'body': b''}
        with self.assertRaisesRegex(agent.InvestigationError, 'redirect_loop'):
            agent.fetch_chain('https://foo.id/', fetch_one=forever)

        def new_path(url, timeout):
            count = int(url.rsplit('/', 1)[1] or '0')
            return {'status': 302, 'location': '/' + str(count + 1), 'body': b''}
        with self.assertRaisesRegex(agent.InvestigationError, 'redirect_hop_limit_exceeded'):
            agent.fetch_chain('https://foo.id/0', max_hops=2, fetch_one=new_path)

    def test_readonly_job_correlation_and_model_disagreement(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'jobs.sqlite3'
            con = sqlite3.connect(db)
            con.execute('''CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT,final_url TEXT,
                            status TEXT,risk TEXT,reason TEXT,route_reason TEXT,
                            classifier_json TEXT, updated_at TEXT)''')
            con.execute('INSERT INTO jobs VALUES (47483,?,?,?,?,?,?,?,?)', (
                'https://jebol.id/', 'https://linkgue.site/jeboltogel', 'alerted',
                'high_risk', 'bima_alert_sent', 'risky_http',
                json.dumps({'ml_pred': 'gambling', 'ml_source': 'classic_ml',
                            'ml_conf': 0.79, 'bert_used': True,
                            'indobert_pred': 'non-gambling', 'indobert_conf': .0001,
                            'strong_hits': ['togel', 'bonus']}),
                '2026-11-21T15:24:06Z'))
            con.commit()
            con.close()
            r = agent.read_job(str(db), 47483)
            self.assertTrue(r['model_disagreement'])
            self.assertEqual(r['classification_reason'], None)
            self.assertEqual(r['strong_hits'], ['togel', 'bonus'])
            self.assertEqual(os.stat(db).st_size > 0, True)

    def test_exclusive_private_output_and_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'manifest.json'
            lock = Path(d) / 'agent.lock'
            with agent.single_investigation(str(lock)):
                with self.assertRaisesRegex(agent.InvestigationError,
                                             'investigation_already_running'):
                    with agent.single_investigation(str(lock)):
                        pass
            agent.save_private(path, {'job': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(agent.InvestigationError,
                                         'output_open_failed'):
                agent.save_private(path, {'job': 2})

    def test_network_failure_is_inconclusive(self):
        def fail(url, timeout):
            raise agent.InvestigationError('network_or_tls_failure')
        r = agent.investigate('https://foo.id/', fetch_one=fail)
        self.assertEqual(r['observation']['assessment'], 'inconclusive')
        self.assertEqual(r['observation']['category'], 'not_determined')


if __name__ == '__main__':
    unittest.main()
