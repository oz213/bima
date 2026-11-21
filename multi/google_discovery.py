#!/usr/bin/env python3
"""BIMA .id Google-index discovery: opt-in, preview-only source adapter.

This module DOES NOT import crawler_google.py (legacy Selenium/alert path),
does not mutate the crawler SQLite DB, and never issues BIMA alerts.

Google Custom Search JSON API ends for existing customers 2027-01-01:
keep provider-specific network calls behind the GoogleCSEProvider adapter.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from proactive_discovery import normalize_url, host_of

API_ENDPOINT = 'https://www.googleapis.com/customsearch/v1'
GOOGLE_CSE_END = date(2027, 1, 1)
SCOPE_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*id$')


class GoogleDiscoveryError(ValueError):
    """Safe error that must not contain API secrets or raw request URLs."""


def normalize_scope(value):
    """Convert leading-dot suffix notation to DNS suffix without the dot.

    A scope names a DNS suffix, NOT a live host to be crawled.  Examples:
      '.co.id' => 'co.id'; '.id' => 'id'.
    """
    if not isinstance(value, str):
        raise GoogleDiscoveryError('domain scope must be a string')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise GoogleDiscoveryError('invalid control character in domain scope')
    value = value.strip().lower()
    if not value.startswith('.') or value.endswith('.') or '/' in value:
        raise GoogleDiscoveryError('domain scopes must have leading dot, e.g. .co.id')
    scope = value[1:]
    if len(scope) > 253 or not SCOPE_RE.fullmatch(scope):
        raise GoogleDiscoveryError('invalid .id domain scope')
    return scope


def load_scopes(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = obj.get('domains') if isinstance(obj, dict) else obj
    if not isinstance(entries, list) or not entries:
        raise GoogleDiscoveryError('domains.json requires a nonempty domains list')
    unique = {}
    for entry in entries:
        normalized = normalize_scope(entry)
        unique[normalized] = None
    # Query narrow scopes first; broad .id last. This does not imply full coverage.
    return sorted(unique, key=lambda x: (-x.count('.'), x == 'id', x))


def hostname_matches_scope(hostname, scope):
    """Requires a registrable subdomain under the configured DNS suffix."""
    return hostname.endswith('.' + scope) and hostname != scope


def best_scope(hostname, scopes):
    for scope in scopes:  # ordered narrow -> broad
        if hostname_matches_scope(hostname, scope):
            return '.' + scope
    return None


def normalize_candidate(raw_url, requested_scope, all_scopes):
    """Return (canonical URL, best suffix) only within requested .id scope."""
    normalized = normalize_url(raw_url)
    if normalized is None:
        return None
    host = host_of(normalized)
    if not hostname_matches_scope(host, requested_scope):
        return None
    matched = best_scope(host, all_scopes)
    if matched is None:
        return None
    return normalized, matched


def clean_keywords(keywords, maximum):
    if not isinstance(keywords, list):
        return []
    cleaned = []
    for value in keywords:
        if not isinstance(value, str):
            continue
        # Do not permit a config keyword to insert a new site:/search operator.
        value = re.sub(r'[^\w\s-]+', ' ', value, flags=re.UNICODE)
        value = ' '.join(value.split())[:64].strip()
        if value and value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= maximum:
            break
    return cleaned


def google_query(scope, keywords):
    # Google documents site: as an operator, not as a DNS delegation audit.
    # Use .id for TLD-wide search and co.id for narrow SLDs. Independently
    # validate every returned hostname; never trust search operators as ACLs.
    site = '.id' if scope == 'id' else scope
    terms = ' OR '.join('"' + keyword + '"' for keyword in keywords)
    return 'site:' + site + (' (' + terms + ')' if terms else '')


def read_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise GoogleDiscoveryError('config.json must be a JSON object')
    return cfg


def resolve_secret(cfg, key):
    value = cfg.get(key)
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if value.startswith('${') and value.endswith('}') and len(value) > 3:
        variable = value[2:-1]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', variable):
            return ''
        return os.environ.get(variable, '').strip()
    return value


class GoogleCSEProvider:
    """Bounded network adapter; errors never disclose API keys or raw URLs."""

    def __init__(self, api_key, cx, timeout, session=None):
        if not api_key or not cx:
            raise GoogleDiscoveryError('GOOGLE_API_KEY and GOOGLE_CX are required')
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = True
        self.session = session
        self.api_key = api_key
        self.cx = cx
        self.timeout = timeout

    def search(self, query, num):
        params = {'key': self.api_key, 'cx': self.cx, 'q': query,
                  'num': num, 'start': 1}
        try:
            response = self.session.get(
                API_ENDPOINT, params=params,
                timeout=(min(self.timeout, 5.0), self.timeout),
                allow_redirects=False,
            )
        except Exception as exc:
            # Exception strings from HTTP clients can contain the API key.
            raise GoogleDiscoveryError('Google CSE transport failure: ' +
                                       type(exc).__name__) from None
        if response.status_code != 200:
            raise GoogleDiscoveryError('Google CSE HTTP status ' +
                                       str(response.status_code))
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise GoogleDiscoveryError('Google CSE invalid JSON response') from None
        if not isinstance(payload, dict):
            raise GoogleDiscoveryError('Google CSE invalid JSON object')
        items = payload.get('items', [])
        if not isinstance(items, list):
            raise GoogleDiscoveryError('Google CSE invalid result items')
        return [item.get('link') for item in items[:num]
                if isinstance(item, dict) and isinstance(item.get('link'), str)]


def preview(args, provider=None):
    scopes = load_scopes(args.domains)
    cfg = read_config(args.config)
    keywords = clean_keywords(cfg.get('QUERY_KEYWORDS', []), args.max_keywords)
    selected = scopes[:args.scope_limit]
    queries = [{'scope': '.' + scope, 'query': google_query(scope, keywords)}
               for scope in selected]
    result = {
        'ok': True, 'mode': 'google_api_preview' if args.execute_google_api else 'plan',
        'source': 'google_cse_index', 'evidence_level': 'search_index_signal',
        'domains_total': len(scopes), 'scopes_this_run': len(selected),
        'scopes': ['.' + scope for scope in scopes],
        'queries': queries, 'keywords_used': len(keywords),
        'api_key_configured': bool(resolve_secret(cfg, 'GOOGLE_API_KEY')),
        'cx_configured': bool(resolve_secret(cfg, 'GOOGLE_CX')),
        'api_requests': 0, 'successful_requests': 0,
        'raw_results': 0, 'valid_results': 0,
        'outside_scope_or_invalid': 0, 'duplicate_results': 0,
        'new_jobs': 0, 'db_writes': 0, 'alerts_sent': 0,
        'browser_launched': False, 'preview_candidates': [],
        'provider_deprecation': 'Google Custom Search JSON API: 2027-01-01',
    }
    if not args.execute_google_api:
        return result
    if not args.acknowledge_quota:
        raise GoogleDiscoveryError('--execute-google-api requires --acknowledge-quota')
    if not keywords:
        raise GoogleDiscoveryError('Google API execution requires QUERY_KEYWORDS')
    if datetime.now(timezone.utc).date() >= GOOGLE_CSE_END:
        raise GoogleDiscoveryError('Google Custom Search JSON API end date reached')
    if provider is None:
        provider = GoogleCSEProvider(
            resolve_secret(cfg, 'GOOGLE_API_KEY'),
            resolve_secret(cfg, 'GOOGLE_CX'), args.timeout)
    unique = {}
    for query in queries[:args.max_api_requests]:
        scope = query['scope'][1:]
        # Count attempted requests even when Google returns 403/429.
        # Stop on provider failure, without retries or exposed credentials.
        result['api_requests'] += 1
        try:
            found = provider.search(query['query'], args.results_per_request)
        except GoogleDiscoveryError as exc:
            result['provider_error'] = str(exc)
            result['ok'] = False
            break
        result['successful_requests'] += 1
        for item in found:
            result['raw_results'] += 1
            normalized = normalize_candidate(item, scope, scopes)
            if normalized is None:
                result['outside_scope_or_invalid'] += 1
                continue
            url, best = normalized
            if url in unique:
                result['duplicate_results'] += 1
                if query['scope'] not in unique[url]['found_via_scopes']:
                    unique[url]['found_via_scopes'].append(query['scope'])
                continue
            unique[url] = {
                'url': url, 'matched_scope': best,
                'found_via_scopes': [query['scope']],
                'source': 'google_cse_index',
                'evidence_level': 'search_index_signal',
            }
        if args.request_interval > 0 and result['api_requests'] < min(
                args.max_api_requests, len(queries)):
            time.sleep(args.request_interval)
    result['valid_results'] = len(unique)
    result['preview_candidates'] = list(unique.values())[:args.preview_limit]
    # No --apply mode or implicit calls to the crawler DB, Telegram, Selenium.
    return result


def make_parser():
    p = argparse.ArgumentParser(description='BIMA 4.3.3 Google .id Discovery: read-only, plan by default')
    p.add_argument('--domains', default='domains.json')
    p.add_argument('--config', default='config.json')
    p.add_argument('--scope-limit', type=int, default=14)
    p.add_argument('--max-keywords', type=int, default=6)
    p.add_argument('--preview-limit', type=int, default=20)
    p.add_argument('--execute-google-api', action='store_true',
                   help='Opt-in: make actual Google CSE API calls (never enqueue)')
    p.add_argument('--acknowledge-quota', action='store_true',
                   help='Acknowledge that Google CSE requests consume quota')
    p.add_argument('--max-api-requests', type=int, default=2)
    p.add_argument('--results-per-request', type=int, default=10)
    p.add_argument('--request-interval', type=float, default=1.0)
    p.add_argument('--timeout', type=float, default=8.0)
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    if not (1 <= args.scope_limit <= 100 and 1 <= args.max_keywords <= 10
            and 0 <= args.preview_limit <= 100
            and 1 <= args.max_api_requests <= 14
            and 1 <= args.results_per_request <= 10
            and 0 <= args.request_interval <= 30
            and 2 <= args.timeout <= 15):
        raise SystemExit('Invalid bounded Google discovery parameters')
    try:
        result = preview(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # File parsing errors may contain secrets; expose only exception type
        # for JSON errors. GoogleDiscoveryError messages are curated.
        message = str(exc) if isinstance(exc, GoogleDiscoveryError) else type(exc).__name__
        result = {'ok': False, 'mode': 'error', 'error': message}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
