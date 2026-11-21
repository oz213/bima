#!/usr/bin/env python3
"""BIMA v4.3.5: bounded, multi-provider .id discovery, preview-only.

Google CSE, Brave Search, and urlscan.io are read-only sources.  The only
persistent option is an explicit, local hash-based deduplication state file.
This module cannot enqueue jobs, crawl candidate websites, or send BIMA alerts.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from brave_quota import BraveQuotaError, reserve_request
from google_discovery import (
    GoogleCSEProvider, GoogleDiscoveryError, GOOGLE_CSE_END,
    best_scope, clean_keywords, google_query, load_scopes,
    normalize_candidate, read_config, resolve_secret,
)

PROVIDERS = ('google', 'brave', 'urlscan')
BRAVE_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
URLSCAN_ENDPOINT = 'https://urlscan.io/api/v1/search/'
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SEEN_HASHES = 100000
_HASH_PATTERN = re.compile(r'^[a-f0-9]{64}$')


class DiscoveryError(ValueError):
    """Curated error: never include a credential or raw request URL."""


def parse_providers(raw):
    names = [x.strip().lower() for x in raw.split(',') if x.strip()]
    if not names or len(names) != len(set(names)) or any(x not in PROVIDERS for x in names):
        raise DiscoveryError('invalid --providers; choose comma-separated google,brave,urlscan')
    return names


def _bounded_json(session, provider, endpoint, params, headers, timeout):
    """Read at most 1 MiB from a fixed HTTPS API; no redirects or retries."""
    response = None
    try:
        response = session.get(
            endpoint, params=params, headers=headers,
            timeout=(min(5.0, timeout), timeout),
            allow_redirects=False, stream=True,
        )
    except Exception as exc:
        raise DiscoveryError(provider + ' transport failure: ' + type(exc).__name__) from None
    try:
        if response.status_code != 200:
            raise DiscoveryError(provider + ' HTTP status ' + str(response.status_code))
        data = bytearray()
        try:
            for part in response.iter_content(chunk_size=65536):
                if not part:
                    continue
                data.extend(part)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise DiscoveryError(provider + ' response exceeds 1 MiB limit')
        except DiscoveryError:
            raise
        except Exception as exc:
            raise DiscoveryError(provider + ' read failure: ' + type(exc).__name__) from None
        try:
            payload = json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise DiscoveryError(provider + ' invalid JSON response') from None
        if not isinstance(payload, dict):
            raise DiscoveryError(provider + ' invalid JSON object')
        return payload
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass


def _session():
    import requests
    session = requests.Session()
    session.headers.update({'User-Agent': 'PANDI-BIMA-Discovery/4.3.5'})
    return session


class BraveSearchProvider:
    def __init__(self, key, timeout=8.0, session=None):
        if not key:
            raise DiscoveryError('BRAVE_SEARCH_API_KEY is required')
        self.key = key
        self.timeout = timeout
        self.session = session or _session()

    def search(self, query, num):
        payload = _bounded_json(
            self.session, 'brave', BRAVE_ENDPOINT,
            {'q': query, 'count': min(num, 10), 'offset': 0, 'safesearch': 'moderate'},
            {'X-Subscription-Token': self.key, 'Accept': 'application/json'},
            self.timeout,
        )
        web = payload.get('web', {})
        if not isinstance(web, dict) or not isinstance(web.get('results', []), list):
            raise DiscoveryError('brave invalid results')
        return [item['url'] for item in web.get('results', [])[:num]
                if isinstance(item, dict) and isinstance(item.get('url'), str)]


class UrlscanSearchProvider:
    def __init__(self, key, timeout=8.0, session=None):
        if not key:
            raise DiscoveryError('URLSCAN_API_KEY is required')
        self.key = key
        self.timeout = timeout
        self.session = session or _session()

    def search(self, query, num):
        payload = _bounded_json(
            self.session, 'urlscan', URLSCAN_ENDPOINT,
            {'q': query, 'size': min(num, 10)},
            {'API-Key': self.key, 'Accept': 'application/json'},
            self.timeout,
        )
        items = payload.get('results', [])
        if not isinstance(items, list):
            raise DiscoveryError('urlscan invalid results')
        results = []
        for item in items[:num]:
            if not isinstance(item, dict):
                continue
            task, page = item.get('task', {}), item.get('page', {})
            # Original task URL is preferred. Redirect destination is not
            # evidence the original was within the queried suffix.
            task = task if isinstance(task, dict) else {}
            page = page if isinstance(page, dict) else {}
            url = task.get('url') or page.get('url')
            if isinstance(url, str):
                results.append(url)
        return results


def query_for(provider, scope, keywords):
    if provider in ('google', 'brave'):
        return google_query(scope, keywords)
    # urlscan uses Elasticsearch query-string syntax, not Google `site:`.
    # Restrict to a recent window. This searches indexed scan metadata only:
    # it does not submit any URL to be scanned.
    terms = ' OR '.join('(page.title:' + k + ' OR task.url:' + k + ')'
                        for k in keywords)
    return 'page.domain:' + scope + ' AND (' + terms + ') AND date:>now-7d'


def choose_scopes(scopes, budget, day, provider):
    """Deterministic per-day rotation: small budgets cover all scopes in time."""
    if not scopes or budget < 1:
        return []
    count = min(len(scopes), budget)
    salt = PROVIDERS.index(provider)
    start = ((day.toordinal() * count) + (salt * count)) % len(scopes)
    ordered = scopes[start:] + scopes[:start]
    return ordered[:count]


def hash_url(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def read_state(path):
    if not path:
        return []
    filename = Path(path)
    if filename.is_symlink():
        raise DiscoveryError('state file must not be a symlink')
    if not filename.exists():
        return []
    if filename.stat().st_size > 8 * 1024 * 1024:
        raise DiscoveryError('state file exceeds 8 MiB')
    try:
        data = json.loads(filename.read_text(encoding='utf-8'))
    except (ValueError, OSError, UnicodeError):
        raise DiscoveryError('state file invalid or unreadable') from None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('seen'), list):
        raise DiscoveryError('state file invalid schema')
    values = data['seen']
    if len(values) > MAX_SEEN_HASHES or any(not isinstance(x, str) or not _HASH_PATTERN.fullmatch(x) for x in values):
        raise DiscoveryError('state file invalid URL hashes')
    return list(dict.fromkeys(values))


def write_state(path, seen):
    filename = Path(path)
    if filename.is_symlink():
        raise DiscoveryError('state file must not be a symlink')
    parent = filename.parent
    if not parent.is_dir():
        raise DiscoveryError('state directory does not exist')
    fd, tmp = tempfile.mkstemp(prefix='.bima-multisource-', suffix='.tmp', dir=str(parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'version': 1, 'seen': list(seen)[-MAX_SEEN_HASHES:],
                       'updated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if filename.is_symlink():
            raise DiscoveryError('state file changed to symlink')
        os.replace(tmp, filename)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def provider_keys(config):
    return {
        'google': (resolve_secret(config, 'GOOGLE_API_KEY'), resolve_secret(config, 'GOOGLE_CX')),
        # An environment-only secret is supported; no change to config.json is needed.
        'brave': (resolve_secret(config, 'BRAVE_SEARCH_API_KEY') or
                  os.environ.get('BRAVE_SEARCH_API_KEY', '').strip(),),
        'urlscan': (resolve_secret(config, 'URLSCAN_API_KEY'),),
    }


def make_provider(name, keys, timeout, injected=None):
    if injected and name in injected:
        return injected[name]
    if name == 'google':
        return GoogleCSEProvider(keys[0], keys[1], timeout)
    if name == 'brave':
        return BraveSearchProvider(keys[0], timeout)
    return UrlscanSearchProvider(keys[0], timeout)


def discover(args, providers=None, now=None):
    scopes = load_scopes(args.domains)[:args.scope_limit]
    config = read_config(args.config)
    keywords = clean_keywords(config.get('QUERY_KEYWORDS', []), args.max_keywords)
    chosen = parse_providers(args.providers)
    if not keywords:
        raise DiscoveryError('QUERY_KEYWORDS must be a nonempty list')
    day = (now or datetime.now(timezone.utc)).date()
    secrets = provider_keys(config)
    budgets = {'google': args.max_google_requests,
               'brave': args.max_brave_requests,
               'urlscan': args.max_urlscan_requests}
    result = {
        'ok': True, 'mode': 'api_preview' if args.execute else 'plan',
        'version': '4.3.5', 'scopes_total': len(scopes),
        'scopes': ['.' + s for s in scopes],
        'keywords_used': keywords, 'date_utc': day.isoformat(),
        'api_requests': 0, 'successful_requests': 0,
        'raw_results': 0, 'valid_results': 0,
        'duplicate_results': 0, 'outside_scope_or_invalid': 0,
        'new_candidates': 0, 'already_seen_candidates': 0,
        'state_file_enabled': bool(args.state_file), 'state_file_updated': False,
        'providers': {}, 'planned_queries': [], 'preview_candidates': [],
        'db_writes': 0, 'new_jobs': 0, 'alerts_sent': 0,
        'browser_launched': False,
        'discovery_only': True,
        'provider_deprecation': 'Google CSE: 2027-01-01',
        # Brave URL responses are transient. Only aggregate counts are returned.
        'brave_transient_valid_results': 0,
        'brave_transient_unique_results': 0,
        'brave_monthly_request_cap': getattr(args, 'brave_monthly_request_cap', 900),
    }
    calls = {}
    for name in chosen:
        plan_scopes = choose_scopes(scopes, budgets[name], day, name)
        items = [{'scope': '.' + s, 'query': query_for(name, s, keywords)}
                 for s in plan_scopes]
        calls[name] = items
        configured = bool(all(secrets[name]))
        expired = name == 'google' and day >= GOOGLE_CSE_END
        result['providers'][name] = {
            'configured': configured, 'status': 'planned',
            'requests': 0, 'successful_requests': 0,
            'raw_results': 0, 'valid_results': 0,
            'error': None, 'scopes_this_run': [x['scope'] for x in items],
            'expired': expired,
            'quota_reserved_this_run': 0,
            'transient_only': name == 'brave',
        }
        result['planned_queries'].extend(
            [{'provider': name, **x} for x in items])
    if not args.execute:
        return result
    if not args.acknowledge_quota:
        raise DiscoveryError('--execute requires --acknowledge-quota')

    prior = read_state(args.state_file)
    seen = OrderedDict((h, None) for h in prior)
    unique = OrderedDict()
    # Discard this set at process exit; it never enters logs or the state file.
    brave_unique = set()
    for name in chosen:
        details = result['providers'][name]
        if details['expired']:
            details['status'] = 'skipped_expired'
            continue
        if not details['configured']:
            details['status'] = 'skipped_missing_credentials'
            continue
        if name == 'brave' and not getattr(args, 'brave_monthly_ledger', ''):
            details['status'] = 'skipped_missing_quota_ledger'
            continue
        adapter = make_provider(name, secrets[name], args.timeout, injected=providers)
        details['status'] = 'ok'
        for idx, item in enumerate(calls[name]):
            if name == 'brave':
                try:
                    allowed, _ = reserve_request(
                        args.brave_monthly_ledger,
                        getattr(args, 'brave_monthly_request_cap', 900),
                        now=now)
                except (BraveQuotaError, OSError) as exc:
                    details['status'] = 'error'
                    details['error'] = (str(exc) if isinstance(exc, BraveQuotaError)
                                        else 'Brave quota ledger IO failure')
                    result['ok'] = False
                    break
                if not allowed:
                    details['status'] = 'skipped_monthly_cap'
                    break
                details['quota_reserved_this_run'] += 1
            result['api_requests'] += 1
            details['requests'] += 1
            try:
                found = adapter.search(item['query'], args.results_per_request)
                if not isinstance(found, list):
                    raise DiscoveryError(name + ' invalid provider result')
            except (DiscoveryError, GoogleDiscoveryError) as exc:
                # Both exceptions contain curated messages, never raw URLs/secrets.
                details['error'] = str(exc)
                details['status'] = 'error'
                result['ok'] = False
                break  # No retry, do not affect other sources.
            except Exception as exc:
                details['error'] = name + ' failure: ' + type(exc).__name__
                details['status'] = 'error'
                result['ok'] = False
                break
            result['successful_requests'] += 1
            details['successful_requests'] += 1
            for url in found[:args.results_per_request]:
                result['raw_results'] += 1
                details['raw_results'] += 1
                normalized = normalize_candidate(url, item['scope'][1:], scopes)
                if normalized is None:
                    result['outside_scope_or_invalid'] += 1
                    continue
                canonical, matched = normalized
                details['valid_results'] += 1
                if name == 'brave':
                    # Brave terms permit transient operational use, not a
                    # persistent search-result database. Never output or hash
                    # URLs from Brave into the state cache or JSONL.
                    result['brave_transient_valid_results'] += 1
                    brave_unique.add(canonical)
                    continue
                if canonical in unique:
                    result['duplicate_results'] += 1
                    record = unique[canonical]
                    if item['scope'] not in record['found_via_scopes']:
                        record['found_via_scopes'].append(item['scope'])
                    continue
                unique[canonical] = {
                    'url': canonical,
                    'matched_scope': matched,
                    'sources': [name],
                    'found_via_scopes': [item['scope']],
                    'evidence_level': ('historical_scan_metadata' if name == 'urlscan'
                                       else 'search_index_signal'),
                }
            if idx + 1 < len(calls[name]) and args.request_interval:
                time.sleep(args.request_interval)
    result['valid_results'] = len(unique)
    result['brave_transient_unique_results'] = len(brave_unique)
    novel = []
    for url, record in unique.items():
        digest = hash_url(url)
        if digest in seen:
            result['already_seen_candidates'] += 1
        else:
            novel.append(record)
            seen[digest] = None
    result['new_candidates'] = len(novel)
    result['preview_candidates'] = novel[:args.preview_limit]
    result['preview_truncated'] = len(novel) > args.preview_limit
    # Only persist hashes for candidates actually emitted in JSON output.
    # This prevents irreversible loss of unseen, truncated candidates.
    if args.state_file and result['preview_candidates']:
        known = OrderedDict((h, None) for h in prior)
        for rec in result['preview_candidates']:
            known[hash_url(rec['url'])] = None
        write_state(args.state_file, known)
        result['state_file_updated'] = True
    return result


def make_parser():
    p = argparse.ArgumentParser(description='BIMA v4.3.5 Multi-Source Discovery: safe plan by default')
    p.add_argument('--domains', default='domains.json')
    p.add_argument('--config', default='config.json')
    p.add_argument('--providers', default='google,brave,urlscan')
    p.add_argument('--scope-limit', type=int, default=14)
    p.add_argument('--max-keywords', type=int, default=6)
    p.add_argument('--max-google-requests', type=int, default=2)
    p.add_argument('--max-brave-requests', type=int, default=2)
    p.add_argument('--brave-monthly-ledger', default='',
                   help='Private per-month attempt ledger (required for Brave execution)')
    p.add_argument('--brave-monthly-request-cap', type=int, default=900,
                   help='Local Brave reservation ceiling; 900 recommended; <=1000')
    p.add_argument('--max-urlscan-requests', type=int, default=2)
    p.add_argument('--results-per-request', type=int, default=5)
    p.add_argument('--preview-limit', type=int, default=500)
    p.add_argument('--timeout', type=float, default=8.0)
    p.add_argument('--request-interval', type=float, default=1.0)
    p.add_argument('--state-file', default='',
                   help='Optional state JSON path for deduplication across days (no crawler DB)')
    p.add_argument('--execute', action='store_true', help='Opt-in network requests to selected APIs')
    p.add_argument('--acknowledge-quota', action='store_true', help='Accept each provider API quota/cost')
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if not (1 <= args.scope_limit <= 100 and 1 <= args.max_keywords <= 10
                and all(1 <= x <= 14 for x in (args.max_google_requests,
                         args.max_brave_requests, args.max_urlscan_requests))
                and 1 <= args.brave_monthly_request_cap <= 1000
                and 1 <= args.results_per_request <= 10
                and 0 <= args.preview_limit <= 500
                and 2.0 <= args.timeout <= 15.0
                and 0 <= args.request_interval <= 30):
            raise DiscoveryError('invalid bounded discovery parameters')
        result = discover(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        text = str(exc) if isinstance(exc, DiscoveryError) else type(exc).__name__
        result = {'ok': False, 'mode': 'error', 'error': text,
                  'db_writes': 0, 'new_jobs': 0, 'alerts_sent': 0}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
