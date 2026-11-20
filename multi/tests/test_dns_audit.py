"""Offline DNS delegation audit tests; no network access or BIMA calls."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import dns_audit as da
import crawler_multi as cm


HOST = 'anggrekbondowoso.web.id'
ZONE = HOST + '.'
PARENT_IP = '1.1.1.1'
LEAF_IPS = ('203.161.184.96', '103.30.147.9')


def packet(server, record_type, rcode, aa=False, soa=False, referrals=None):
    return {'server': server, 'type': record_type, 'rcode': rcode, 'aa': aa,
            'has_soa': soa, 'has_answer': soa, 'has_address': False,
            'referrals': referrals or []}


def simulated_audit(soa_rcodes=('REFUSED','REFUSED'), complete=True,
                    parent_response=None):
    child_referral = {'zone': ZONE, 'nameservers': ['ns1.idwebhost.id.',
                                                   'ns2.idwebhost.id.']}
    if parent_response is None:
        parent_response = packet(PARENT_IP, 'A', 'NOERROR', referrals=[child_referral])
    endpoints = [{'nameserver': 'ns1.idwebhost.id.', 'address': LEAF_IPS[0]},
                 {'nameserver': 'ns2.idwebhost.id.', 'address': LEAF_IPS[1]}]

    def query(ip, domain, typ, timeout):
        if ip == PARENT_IP:
            return parent_response
        if typ == 'A':
            return packet(ip, 'A', 'REFUSED')
        if typ == 'SOA':
            idx = LEAF_IPS.index(ip)
            rc = soa_rcodes[idx]
            return packet(ip, 'SOA', rc, aa=(rc == 'NOERROR'), soa=(rc == 'NOERROR'))
        return packet(ip, typ, 'NOERROR', aa=True)

    with mock.patch.object(da, 'bootstrap_id_servers', return_value=(
                ['f.dns.id.'], [{'nameserver':'f.dns.id.','address':PARENT_IP}], [],[], True)), \
         mock.patch.object(da, 'resolve_ns_endpoints', return_value=(endpoints, [], [], complete)), \
         mock.patch.object(da, 'query_server', side_effect=query):
        return da.audit_domain(HOST, resolver=object(), timeout=0.25)


class DnsAuditTests(unittest.TestCase):
    def test_id_host_normalization(self):
        self.assertEqual(da.canonical_host('https://WWW.EXAMPLE.co.id/path'),
                         'www.example.co.id')
        self.assertEqual(da.canonical_host('xn--bali-test.id.'), 'xn--bali-test.id')
        for x in ('evil.com', '127.0.0.1', 'http://user:pass@pandi.id',
                  'bad host.id', '-bad.id', 'foo..id'):
            with self.subTest(host=x), self.assertRaises(ValueError):
                da.canonical_host(x)

    def test_reject_nonpublic_dns_servers(self):
        for addr in ('127.0.0.1', '10.0.0.5', '192.168.1.2',
                     '169.254.169.254', '::1', 'fc00::1', '203.0.113.1'):
            self.assertFalse(da.public_address(addr), addr)
        self.assertTrue(da.public_address('1.1.1.1'))
        with self.assertRaises(ValueError):
            da.new_resolver(('127.0.0.1',))

    def test_refused_all_authoritative_ns_is_diagnosed_not_nxdomain(self):
        result = simulated_audit()
        self.assertEqual(result['diagnosis'], 'authoritative_refused')
        self.assertEqual(result['delegated_zone'], ZONE)
        self.assertEqual(result['authority_rcodes'], ['REFUSED'])
        self.assertTrue(result['coverage_complete'])
        self.assertEqual(len(result['authority_checks']), 2)

    def test_healthy_authority_takes_precedence_over_refusal(self):
        result = simulated_audit(('NOERROR', 'REFUSED'))
        self.assertEqual(result['diagnosis'], 'authoritative_responding')
        self.assertEqual(result['address_observation'], 'inconclusive')

    def test_partial_coverage_cannot_be_confirmed_refused(self):
        result = simulated_audit(complete=False)
        self.assertEqual(result['diagnosis'], 'authority_inconsistent_or_unavailable')
        self.assertFalse(result['coverage_complete'])

    def test_timeout_and_refused_cannot_be_confirmed_refused(self):
        result = simulated_audit(('REFUSED', 'NO_RESPONSE'))
        self.assertEqual(result['diagnosis'], 'authority_inconsistent_or_unavailable')
        result2 = simulated_audit(('NO_RESPONSE', 'NO_RESPONSE'))
        self.assertEqual(result2['diagnosis'], 'authority_unreachable')

    def test_parent_refused_not_misreported_as_lame_delegation(self):
        result = simulated_audit(parent_response=packet(PARENT_IP,'A','REFUSED'))
        self.assertEqual(result['diagnosis'], 'parent_authority_unavailable')
        self.assertIsNone(result['delegated_zone'])

    def test_migration_v123_preserves_crawler_jobs(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tempdir:
            path=Path(tempdir)/'old.sqlite3'
            old=sqlite3.connect(path)
            old.executescript('''CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                http_ms INTEGER, browser_ms INTEGER, http_status INTEGER,
                reason TEXT, risk TEXT, classifier_json TEXT, final_url TEXT,
                evidence_path TEXT, alert_confidence REAL, error TEXT,
                updated_at TEXT, route_reason TEXT, error_category TEXT,
                classification_ms INTEGER, next_retry_at TEXT,
                bert_ms INTEGER, dns_result TEXT, dns_errno INTEGER
            );
            INSERT INTO jobs(url,status,attempts,error_category)
              VALUES ('https://name.id', 'retry_http',1,'dns_error');''')
            old.commit()
            old.close()
            conn=cm.connect_db(str(path))
            cols={x[1] for x in conn.execute('PRAGMA table_info(jobs)')}
            self.assertIn('dns_host',cols)
            self.assertIn('dns_host_audits', {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
            da.store_audit(conn, {'hostname':'name.id','diagnosis':'authoritative_refused',
                                  'checked_at': cm.utcnow(), 'authority_rcodes':['REFUSED']})
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE url='https://name.id'").fetchone()[0],
                             'retry_http')
            self.assertEqual(conn.execute('SELECT diagnosis FROM dns_host_audits').fetchone()[0],
                             'authoritative_refused')
            conn.close()

    def test_backlog_dedup_cache_no_change_to_classification(self):
        with tempfile.TemporaryDirectory() as tempdir:
            conn=cm.connect_db(str(Path(tempdir)/'db.sqlite3'))
            with conn:
                for url in ('https://a.example.id/p1', 'https://a.example.id/p2',
                            'https://other.example.id/p1'):
                    conn.execute('''INSERT INTO jobs(url,status,attempts,error_category,reason,risk)
                                    VALUES (?, 'retry_http', 1, 'dns_error','fetch_failed','inconclusive')''',
                                 (url,))
            args=types.SimpleNamespace(max_domains=5, max_scan_multiplier=10,
                                       cache_hours=6, force=False,
                                       upstreams=('1.1.1.1',),timeout=0.25,
                                       max_authority_ips=8)
            call_count=[]
            def mock_audit(host, **kwargs):
                call_count.append(host)
                return {'hostname':host,'diagnosis':'authoritative_refused',
                        'checked_at':cm.utcnow(), 'authority_rcodes':['REFUSED']}
            with mock.patch.object(da,'new_resolver', return_value=object()), \
                 mock.patch.object(da,'audit_domain', side_effect=mock_audit):
                result=da.audit_backlog(conn,args)
                self.assertEqual(len(result),2)
                self.assertEqual(len(call_count),2)
                da.audit_backlog(conn,args)
                self.assertEqual(len(call_count),2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs WHERE status="retry_http"').fetchone()[0],3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs WHERE risk="inconclusive"').fetchone()[0],3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dns_host_audits').fetchone()[0],2)
            joined=conn.execute('''SELECT j.dns_host,a.diagnosis FROM jobs j
                LEFT JOIN dns_host_audits a ON a.hostname=j.dns_host
                ORDER BY j.id''').fetchall()
            self.assertEqual(joined[0], ('a.example.id', 'authoritative_refused'))
            conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
