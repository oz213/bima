#!/usr/bin/env python3
"""Optional, bounded DNS delegation audits for failed .id crawler hostnames.

Runs OUTSIDE the HTTP/IndoBERT/Selenium fast path. Never changes a URL's
classification, crawler status, retry schedule, or sends a BIMA alert.
Requires dnspython. This is operational evidence, not DNSSEC validation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import re
import sys
from urllib.parse import urlparse

try:
    import dns.exception
    import dns.flags
    import dns.message
    import dns.query
    import dns.rcode
    import dns.rdatatype
    import dns.resolver
except ImportError as exc:
    raise SystemExit('dnspython is required for DNS audits: pip install "dnspython>=2.6,<3"') from exc

from crawler_multi import connect_db, utcnow

DEFAULT_UPSTREAMS = ('1.1.1.1', '8.8.8.8')


def canonical_host(value):
    """Accept one .id hostname or URL; never accept an IP or an outside TLD."""
    if not isinstance(value, str):
        raise ValueError('hostname must be text')
    value = value.strip()
    if '://' in value:
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('only HTTP(S) URLs without credentials are supported')
        value = parsed.hostname or ''
    value = value.lower().rstrip('.')
    if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        # IDN is accepted through its A-label (ASCII/Punycode) form only.
        raise ValueError('use a valid ASCII hostname / IDN A-label')
    if len(value) > 253 or not value.endswith('.id') or value == 'id':
        raise ValueError('only registered .id hostnames are supported')
    if not all(1 <= len(label) <= 63 and
               re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label)
               for label in value.split('.')):
        raise ValueError('invalid DNS hostname labels')
    return value


def public_address(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def new_resolver(upstreams=DEFAULT_UPSTREAMS, timeout=2.0):
    servers = tuple(str(x) for x in upstreams)
    if not servers or not all(public_address(s) for s in servers):
        raise ValueError('upstream DNS resolvers must be public IP addresses')
    result = dns.resolver.Resolver(configure=False)
    result.nameservers = list(servers)
    result.timeout = timeout
    result.lifetime = timeout * max(2, len(servers))
    return result


def resolve_ns_endpoints(resolver, names, max_ips=8):
    """Resolve delegated NS names through external recursors, not system stub.

    A failure to resolve one of the NS hosts is retained as a coverage gap.
    Private/link-local answers are excluded from audit probes.
    """
    ips, unresolved, rejected = [], [], []
    seen = set()
    for ns in names:
        usable = False
        for rtype in ('A', 'AAAA'):
            try:
                records = resolver.resolve(ns, rtype, raise_on_no_answer=False)
                for record in records or []:
                    address = getattr(record, 'address', None)
                    if address is None:
                        continue
                    if not public_address(address):
                        rejected.append({'nameserver': ns, 'address': address})
                        continue
                    usable = True
                    if address not in seen:
                        seen.add(address)
                        ips.append({'nameserver': ns, 'address': address})
            except (dns.exception.DNSException, OSError):
                continue
        if not usable:
            unresolved.append(ns)
    is_complete = not unresolved and len(ips) <= max_ips and not rejected
    return ips[:max_ips], unresolved, rejected, is_complete


def bootstrap_id_servers(resolver, max_ips=8):
    names = sorted({str(n.target).lower() for n in resolver.resolve('id.', 'NS')})
    if not names:
        raise ValueError('no .id authoritative nameservers found')
    ips, missing, rejected, complete = resolve_ns_endpoints(resolver, names, max_ips)
    if not ips:
        raise ValueError('no reachable public .id authoritative server address could be discovered')
    return names, ips, missing, rejected, complete


def query_server(ip, domain, record_type, timeout):
    """Direct non-recursive DNS query. UDP with TCP fallback on truncation."""
    try:
        question = dns.message.make_query(domain, record_type, flags=0)
        try:
            response = dns.query.udp(question, ip, timeout=timeout, raise_on_truncation=True)
        except dns.message.Truncated:
            response = dns.query.tcp(question, ip, timeout=timeout)
        rcode = dns.rcode.to_text(response.rcode())
        aa = bool(response.flags & dns.flags.AA)
        referrals = []
        for rrset in response.authority:
            if rrset.rdtype == dns.rdatatype.NS:
                referrals.append({'zone': str(rrset.name).lower(),
                                  'nameservers': sorted(str(x.target).lower() for x in rrset)})
        has_answer = bool(response.answer)
        has_address = any(rrset.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA)
                          for rrset in response.answer)
        has_soa = any(rrset.rdtype == dns.rdatatype.SOA for rrset in response.answer)
        return {'server': ip, 'type': record_type, 'rcode': rcode, 'aa': aa,
                'has_answer': has_answer, 'has_address': has_address,
                'has_soa': has_soa, 'referrals': referrals}
    except (dns.exception.DNSException, OSError) as exc:
        return {'server': ip, 'type': record_type, 'rcode': 'NO_RESPONSE',
                'aa': False, 'error': f'{type(exc).__name__}: {exc}'[:250],
                'referrals': []}


def most_specific_referral(checks, hostname, current_zone):
    """Choose only a referral strictly below the current delegated zone."""
    candidates = []
    host = hostname.rstrip('.') + '.'
    for check in checks:
        for ref in check.get('referrals', []):
            zone = ref['zone'].lower()
            if (zone != current_zone and zone.endswith(current_zone) and
                    (host == zone or host.endswith('.' + zone))):
                candidates.append(ref)
    return max(candidates, key=lambda r: len(r['zone']), default=None)


def assess_soa(checks, complete_coverage):
    """Only call it authoritative REFUSED when *all* delegated endpoints refused."""
    if any(x['rcode'] == 'NOERROR' and x.get('aa') and x.get('has_soa') for x in checks):
        return 'authoritative_responding'
    if (checks and complete_coverage and
            all(x['rcode'] == 'REFUSED' for x in checks)):
        return 'authoritative_refused'
    if checks and all(x['rcode'] == 'NO_RESPONSE' for x in checks):
        return 'authority_unreachable'
    if checks and complete_coverage and all(x['rcode'] == 'NXDOMAIN' and x.get('aa')
                                              for x in checks):
        return 'delegated_zone_missing'
    return 'authority_inconsistent_or_unavailable'


def audit_domain(domain, resolver=None, timeout=2.0, max_authority_ips=8,
                 max_parent_probes=3, max_hops=6):
    """Walk NS referrals from .id; prove leaf failure using direct SOA checks.

    DNS diagnostics are non-authoritative *observations*: do not equate with
    domain registry status, safety, or DNSSEC validity.
    """
    host = canonical_host(domain)
    out = {'hostname': host, 'checked_at': utcnow(), 'diagnosis': 'unclassified',
           'delegated_zone': None, 'delegation_chain': [],
           'authority_checks': [], 'authority_rcodes': [], 'warnings': []}
    resolver = resolver or new_resolver(timeout=timeout)
    try:
        names, endpoints, missing, rejected, complete = bootstrap_id_servers(
            resolver, max_authority_ips)
    except (ValueError, dns.exception.DNSException, OSError) as exc:
        out.update(diagnosis='bootstrap_failed', error=str(exc)[:300])
        return out
    current_zone = 'id.'
    seen_zones = {current_zone}
    out['delegation_chain'].append({'zone': current_zone, 'nameservers': names})
    out['warnings'].extend([f'bootstrap NS resolution failed: {ns}' for ns in missing])
    out['warnings'].extend([f'ignored non-public bootstrap address for {item["nameserver"]}'
                            for item in rejected])
    for hop in range(max_hops):
        # First attempt referral discovery from a subset of currently delegated
        # servers. At the leaf, probe *all* available IPs for SOA below.
        checks = [query_server(item['address'], host + '.', 'A', timeout)
                  for item in endpoints[:max_parent_probes]]
        referral = most_specific_referral(checks, host, current_zone)
        if referral:
            current_zone = referral['zone']
            if current_zone in seen_zones:
                out.update(diagnosis='delegation_loop', error='repeated referral')
                return out
            seen_zones.add(current_zone)
            out['delegated_zone'] = current_zone
            out['delegation_chain'].append(referral)
            endpoints, missing, rejected, complete = resolve_ns_endpoints(
                resolver, referral['nameservers'], max_authority_ips)
            out['warnings'].extend([f'NS resolution failed: {ns}' for ns in missing])
            out['warnings'].extend([f'ignored non-public NS address for {item["nameserver"]}'
                                    for item in rejected])
            if not endpoints:
                out['diagnosis'] = 'delegated_ns_unresolvable'
                return out
            continue
        if current_zone == 'id.':
            # We have not confirmed a child zone delegation. Parent NS refusal
            # must NOT be mislabeled as lame delegation at the target.
            aa_checks = [x for x in checks if x.get('aa')]
            if aa_checks:
                out['diagnosis'] = ('nxdomain_authoritative' if all(
                    x['rcode'] == 'NXDOMAIN' for x in aa_checks)
                    else 'authoritative_parent_responding')
            else:
                out['diagnosis'] = 'parent_authority_unavailable'
            out['authority_checks'] = checks
            out['authority_rcodes'] = sorted({x['rcode'] for x in checks})
            return out
        soa_checks = [query_server(item['address'], current_zone, 'SOA', timeout)
                      for item in endpoints]
        out['authority_checks'] = soa_checks
        out['authority_rcodes'] = sorted({x['rcode'] for x in soa_checks})
        out['diagnosis'] = assess_soa(soa_checks, complete)
        out['coverage_complete'] = complete
        # If the authority responds, distinguish missing address records from
        # a lame delegation. This does NOT follow CNAME chains or DNSSEC.
        if out['diagnosis'] == 'authoritative_responding':
            working = next(x['server'] for x in soa_checks if
                           x['rcode'] == 'NOERROR' and x.get('aa') and x.get('has_soa'))
            a = query_server(working, host + '.', 'A', timeout)
            aaaa = query_server(working, host + '.', 'AAAA', timeout)
            out['address_checks'] = [a, aaaa]
            if any(x.get('has_address') for x in (a, aaaa)):
                out['address_observation'] = 'address_record_present'
            elif all(x['rcode'] == 'NXDOMAIN' and x.get('aa') for x in (a, aaaa)):
                out['address_observation'] = 'host_nxdomain'
            elif all(x['rcode'] == 'NOERROR' and x.get('aa') and not x.get('has_answer')
                     for x in (a, aaaa)):
                out['address_observation'] = 'no_direct_address_observed'
            else:
                out['address_observation'] = 'inconclusive'
        return out
    out.update(diagnosis='delegation_hop_limit', error=f'exceeded {max_hops} delegations')
    return out


def store_audit(conn, audit):
    host = canonical_host(audit['hostname'])
    # Never alter jobs.status, risk, next_retry_at, or classifier_json.
    with conn:
        conn.execute('''INSERT INTO dns_host_audits
                    (hostname,diagnosis,delegated_zone,authority_rcodes,
                     details_json,checked_at,audit_error)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(hostname) DO UPDATE SET
                      diagnosis=excluded.diagnosis,
                      delegated_zone=excluded.delegated_zone,
                      authority_rcodes=excluded.authority_rcodes,
                      details_json=excluded.details_json,
                      checked_at=excluded.checked_at,
                      audit_error=excluded.audit_error''',
                     (host, audit['diagnosis'], audit.get('delegated_zone'),
                      json.dumps(audit.get('authority_rcodes', [])),
                      json.dumps(audit, ensure_ascii=False),
                      audit.get('checked_at', utcnow()), audit.get('error')))


def audit_backlog(conn, args):
    """Bounded per-host audit; reuse cached results and skip pending URLs."""
    seen, results, scanned, last_id = set(), {}, 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.cache_hours)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    max_scanned = args.max_domains * args.max_scan_multiplier
    resolver = new_resolver(args.upstreams, args.timeout)
    while len(results) < args.max_domains and scanned < max_scanned:
        rows = conn.execute('''SELECT id,url,dns_host FROM jobs
             WHERE id > ? AND status IN ('retry_http','inconclusive')
               AND error_category='dns_error'
             ORDER BY id LIMIT 250''', (last_id,)).fetchall()
        if not rows:
            break
        for id_, url, stored_host in rows:
            last_id = id_
            scanned += 1
            try:
                host = canonical_host(stored_host or url)
            except ValueError:
                continue
            if stored_host != host:
                with conn:
                    conn.execute('UPDATE jobs SET dns_host=? WHERE id=?', (host, id_))
            if host in seen:
                continue
            seen.add(host)
            existing = conn.execute('SELECT checked_at FROM dns_host_audits WHERE hostname=?',
                                    (host,)).fetchone()
            if existing and existing[0] >= cutoff and not args.force:
                continue
            details = audit_domain(host, resolver=resolver, timeout=args.timeout,
                                   max_authority_ips=args.max_authority_ips)
            store_audit(conn, details)
            results[host] = details['diagnosis']
            print(f'{host}: {details["diagnosis"]}', flush=True)
            if len(results) >= args.max_domains or scanned >= max_scanned:
                break
        if len(rows) < 250:
            break
    print(f'Audited {len(results)} distinct hostnames; scanned {scanned} jobs; '
          f'cached/seen {len(seen)-len(results)} hostnames')
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description='Optional .id authoritative DNS diagnostic (no crawler changes)')
    p.add_argument('--domain', help='Inspect one domain; e.g. anggrekbondowoso.web.id')
    p.add_argument('--db', help='Existing crawler SQLite DB (required for backlog mode)')
    p.add_argument('--max-domains', type=int, default=20)
    p.add_argument('--max-scan-multiplier', type=int, default=100,
                   help='Limit SQLite rows scanned to max-domains * this value')
    p.add_argument('--cache-hours', type=int, default=6)
    p.add_argument('--force', action='store_true', help='Refresh a cached hostname audit')
    p.add_argument('--timeout', type=float, default=2.0,
                   help='Seconds for each authoritative DNS query')
    p.add_argument('--upstreams', default=','.join(DEFAULT_UPSTREAMS),
                   help='Comma-separated public bootstrap resolvers')
    p.add_argument('--max-authority-ips', type=int, default=8)
    args = p.parse_args(argv)
    args.upstreams = tuple(s.strip() for s in args.upstreams.split(',') if s.strip())
    if not (0 < args.timeout <= 15 and 1 <= args.max_domains <= 100 and
            1 <= args.max_authority_ips <= 16 and 1 <= args.max_scan_multiplier <= 1000 and
            0 <= args.cache_hours <= 24 * 365):
        p.error('invalid bounds: timeout 0..15; max-domains 1..100; max-authority-ips 1..16')
    if not args.db and not args.domain:
        p.error('provide --db to audit failed jobs or --domain for one manual audit')
    resolver = new_resolver(args.upstreams, args.timeout)
    if args.domain:
        details = audit_domain(args.domain, resolver=resolver, timeout=args.timeout,
                               max_authority_ips=args.max_authority_ips)
        if args.db:
            conn = connect_db(args.db)
            try:
                store_audit(conn, details)
            finally:
                conn.close()
        print(json.dumps(details, indent=2, ensure_ascii=False))
    else:
        conn = connect_db(args.db)
        try:
            audit_backlog(conn, args)
        finally:
            conn.close()


if __name__ == '__main__':
    main()
