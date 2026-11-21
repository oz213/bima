"""Bounded, read-only DNS evidence for BIMA proactive .id Discovery.

Uses the existing dns_audit.audit_domain (delegation walk and authoritative
queries) ONLY for a small number of seeds whose web DNS lookup failed. No
independent DNS resolver is introduced. No SQLite writes, job status changes,
retry changes, or abuse verdicts. Import of dnspython is delayed until used.
"""
from __future__ import annotations

from typing import Any

_SAFE_RCODES = frozenset({
    'NOERROR', 'NXDOMAIN', 'SERVFAIL', 'REFUSED', 'NO_RESPONSE',
    'FORMERR', 'NOTIMP', 'YXDOMAIN', 'NOTAUTH', 'NOTZONE',
})


def summarize_dns_audit(hostname: str, audit: dict[str, Any]) -> dict[str, Any]:
    """Produce bounded, non-sensitive evidence; never classify SERVFAIL as lame.

    authoritative_refused is accepted only with confirmed child delegation,
    complete coverage, and every checked authoritative endpoint REFUSED. The
    existing auditor supplies coverage_complete only after referral probing.
    """
    raw_diagnosis = audit.get('diagnosis')
    zone = audit.get('delegated_zone')
    zone = zone.lower() if isinstance(zone, str) else None
    zone_host = zone.rstrip('.') if zone else ''
    # Require whole DNS label boundaries and a delegated .id child zone.
    if (not zone or not zone.endswith('.') or len(zone) > 254 or
            not zone_host.endswith('.id') or
            not (hostname == zone_host or hostname.endswith('.' + zone_host))):
        zone = None
    checks = audit.get('authority_checks')
    checks = checks if isinstance(checks, list) else []
    checks = [check for check in checks if isinstance(check, dict)]
    rcodes = sorted({check.get('rcode') for check in checks
                     if isinstance(check.get('rcode'), str)
                     and check.get('rcode') in _SAFE_RCODES})
    coverage = audit.get('coverage_complete') is True
    status = 'dns_audit_inconclusive'
    if (raw_diagnosis == 'authoritative_refused' and zone and coverage
            and checks and all(check.get('rcode') == 'REFUSED' for check in checks)):
        status = 'dns_lame_delegation'
    elif (zone and checks and all(check.get('rcode') == 'REFUSED' for check in checks)
          and not coverage):
        # Evidence of refusal, but coverage (all endpoints) was not proven.
        status = 'dns_lame_delegation_suspected'
    elif raw_diagnosis == 'delegated_ns_unresolvable' and zone:
        status = 'dns_delegated_ns_unresolvable'
    elif raw_diagnosis == 'authority_unreachable' and zone:
        status = 'dns_authority_unreachable'
    elif raw_diagnosis == 'nxdomain_authoritative':
        status = 'dns_nxdomain_authoritative'
    elif raw_diagnosis == 'authoritative_responding':
        address = audit.get('address_observation')
        if address == 'address_record_present':
            status = 'dns_authoritative_address_observed'
        elif address == 'host_nxdomain':
            status = 'dns_host_nxdomain_observed'
        elif address == 'no_direct_address_observed':
            status = 'dns_no_direct_address_observed'
        else:
            status = 'dns_authority_responding_address_unknown'
    elif raw_diagnosis == 'delegation_loop':
        status = 'dns_delegation_loop'
    # Other diagnoses (including bootstrap failure, incomplete coverage,
    # network timeout, SERVFAIL and mixed rcodes) remain inconclusive.
    return {
        'seed': hostname,
        'dns_status': status,
        'delegated_zone': zone,
        'authority_rcodes': rcodes,
        'authority_checks': len(checks),
        'coverage_complete': coverage,
        'authoritative_refused_observed': sum(
            check.get('rcode') == 'REFUSED' for check in checks),
    }


def audit_dns_failure(hostname: str, *, timeout: float) -> dict[str, Any]:
    """Invoke existing auditor lazily, fail closed; never include exception text.

    The existing audit_domain is already scoped to .id and validates that NS
    IPs are publicly routable. Its own limits are used for SOA verification.
    This wrapper never persists the audit result to the crawler database.
    """
    try:
        from dns_audit import audit_domain
        result = audit_domain(hostname, timeout=timeout, max_authority_ips=8,
                              max_parent_probes=3, max_hops=6)
        if not isinstance(result, dict):
            raise ValueError('invalid DNS auditor response')
        return summarize_dns_audit(hostname, result)
    except (Exception, SystemExit):
        # Do not leak resolver error strings, endpoint addresses or raw trace
        # to console/Telegram. Any dependency/network error is inconclusive.
        return {
            'seed': hostname,
            'dns_status': 'dns_audit_unavailable_or_error',
            'delegated_zone': None,
            'authority_rcodes': [],
            'authority_checks': 0,
            'coverage_complete': False,
            'authoritative_refused_observed': 0,
        }
