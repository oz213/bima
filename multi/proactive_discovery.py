#!/usr/bin/env python3
"""Conservative .id URL/subdomain discovery for the existing BIMA crawler.

Reads the existing Registry-derived urls.json as a *seed inventory* (never edits it).
Discovers links, sitemaps, redacted redirects with pinned HTTP and optional CT.
PREVIEW by default; --apply may append jobs to the existing SQLite queue.
No BIMA alert calls, browser use, or IndoBERT loading occur in this process.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from redirect_intelligence import analyze_html, build_redirect_chains
import http.client
import ipaddress
import json
from pathlib import Path
import re
import urllib.robotparser as robotparser
import socket
import sqlite3
import ssl
import time
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

USER_AGENT = 'PANDI-BIMA-Discovery/1.0 (+registry .id abuse research)'
HTTP_REDIRECTS = (301, 302, 303, 307, 308)
SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_discovery_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_discovery_candidates (
  url TEXT PRIMARY KEY,
  seed_hostname TEXT NOT NULL,
  source TEXT NOT NULL,
  priority_score INTEGER NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  queued INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proactive_candidates_seed
  ON proactive_discovery_candidates(seed_hostname);
CREATE TABLE IF NOT EXISTS proactive_redirect_observations (
  seed_hostname TEXT NOT NULL,
  source_url TEXT NOT NULL,
  destination_url TEXT NOT NULL,
  method TEXT NOT NULL,
  http_status INTEGER NOT NULL DEFAULT 0,
  external INTEGER NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'new',
  PRIMARY KEY(source_url, destination_url, method)
);
CREATE INDEX IF NOT EXISTS idx_proactive_redirect_seed
  ON proactive_redirect_observations(seed_hostname, last_seen);
"""
INTERESTING_TOKENS = {
    'judi': 25, 'slot': 25, 'togel': 25, 'casino': 25,
    'kasino': 25, 'taruhan': 20, 'bonus': 12, 'jackpot': 20,
    'hadiah': 7, 'promo': 6, 'verifikasi': 5, 'verify': 5,
    'login': 3, 'akun': 3, 'claim': 5,
}
SKIP_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico',
    '.woff', '.woff2', '.ttf', '.eot', '.css', '.js', '.map',
    '.pdf', '.zip', '.gz', '.rar', '.exe', '.dmg', '.apk',
    '.mp3', '.mp4', '.avi', '.mov', '.doc', '.docx', '.xls', '.xlsx',
)
SKIP_ACTIONS = re.compile(r'(^|/)(?:logout|signout|delete|remove|unsubscribe)(?:/|$)', re.I)
HOST_LABEL = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_url(raw: str) -> str | None:
    """Canonicalize a public .id HTTP URL, deliberately removing query/fragment.

    Canonicalization is used only for candidate URLs. Network safety is enforced
    separately during each connection (including redirects).
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    if '\\' in raw:
        return None
    if raw.startswith('//'):
        raw = 'https:' + raw
    elif '://' not in raw:
        raw = 'https://' + raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if len(host) > 253 or not host.endswith('.id') or host == 'id':
            return None
        if not all(HOST_LABEL.fullmatch(label) for label in host.split('.')):
            return None
        if parsed.port is not None:
            if parsed.port != (443 if parsed.scheme == 'https' else 80):
                return None
        path = parsed.path or '/'
        if not path.startswith('/') or path.startswith('//') or len(path) > 512:
            return None
        if any(seg in ('.', '..') for seg in path.split('/')):
            return None
        # Preserve existing valid percent-escapes; encode raw Unicode for HTTP.
        path = quote(path, safe="/-._~!$&'()*+,;=:@%")
        if SKIP_ACTIONS.search(path):
            return None
        if path.rstrip('/').lower().endswith(SKIP_EXTENSIONS):
            return None
        return urlunsplit((parsed.scheme, host, path, '', ''))
    except (ValueError, UnicodeError, UnicodeEncodeError):
        return None


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ''


def within_seed(url: str, seed_hostname: str) -> bool:
    host = host_of(url)
    return host == seed_hostname or host.endswith('.' + seed_hostname)


def load_seeds(input_filename: Path) -> tuple[list[str], str, int]:
    """Mirror crawler_multi.import_input_urls's JSON formats; seed by hostname."""
    raw = input_filename.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    parsed = json.loads(raw)
    entries = parsed.get('urls') if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list):
        raise ValueError('urls.json harus array URL atau object dengan key "urls" (array)')
    unique, rejected = {}, 0
    for entry in entries:
        url = normalize_url(entry)
        if url:
            unique[host_of(url)] = None
        else:
            rejected += 1
    return list(unique), digest, rejected


@dataclass(frozen=True)
class Candidate:
    url: str
    source: str
    seed_hostname: str
    priority_score: int


def make_candidate(value: str, *, source: str, seed: str) -> Candidate | None:
    url = normalize_url(value)
    if url is None or not within_seed(url, seed):
        return None
    # The seed's homepage is already handled by BIMA's urls.json import.
    if host_of(url) == seed and urlsplit(url).path == '/':
        return None
    path = urlsplit(url).path.lower()
    score = 5 if source == 'ct' else 1
    for token, weight in INTERESTING_TOKENS.items():
        if token in path or token in host_of(url):
            score += weight
    return Candidate(url, source, seed, min(score, 100))


class Links(HTMLParser):
    def __init__(self, max_links: int = 1500):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.max_links = max_links

    def handle_starttag(self, tag: str, attrs):
        if len(self.links) >= self.max_links:
            return
        attributes = dict(attrs)
        if tag == 'a' and attributes.get('href'):
            self.links.append(attributes['href'])
        if tag == 'link' and attributes.get('href'):
            rel = str(attributes.get('rel') or '').lower().split()
            if any(x in rel for x in ('canonical', 'alternate')):
                self.links.append(attributes['href'])


@dataclass(frozen=True)
class RedirectObservation:
    """Redacted redirect evidence. An external destination is NOT a crawl seed.

    The path may be retained for triage; queries/fragments and credentials are
    deliberately never stored. Static JS is only a signal, not proof of use.
    """
    seed_hostname: str
    source_url: str
    destination_url: str
    method: str
    http_status: int
    external: bool


def evidence_destination(value: str, *, base_url: str) -> str | None:
    """Normalize a redirect location for passive *recording*, never fetching.

    Unlike normalize_url(), external hostnames are allowed, but no private IP,
    URL credentials, nonstandard port, unsupported scheme, query or fragment.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        return None
    try:
        resolved = urljoin(base_url, value.strip())
        parsed = urlsplit(resolved)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if len(host) > 253 or '.' not in host:
            return None
        if not all(HOST_LABEL.fullmatch(label) for label in host.split('.')):
            return None
        if parsed.port is not None and parsed.port != (443 if parsed.scheme == 'https' else 80):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
        path = parsed.path or '/'
        if not path.startswith('/') or len(path) > 512 or path.startswith('//'):
            return None
        if any(seg in ('.', '..') for seg in path.split('/')):
            return None
        return urlunsplit((parsed.scheme, host, quote(path, safe="/-._~!$&'()*+,;=:@%"), '', ''))
    except (ValueError, UnicodeError):
        return None


def make_redirect(source_url: str, destination: str, *, seed: str,
                  method: str, status: int = 0) -> RedirectObservation | None:
    source = normalize_url(source_url)
    if not source or not within_seed(source, seed):
        return None
    target = evidence_destination(destination, base_url=source_url)
    if not target or target == source:
        return None
    return RedirectObservation(seed, source, target, method, status,
                               not within_seed(target, seed))


class DeclarativeRedirectParser(HTMLParser):
    """Find meta-refresh / literal script redirects without executing scripts."""
    ASSIGN = re.compile(
        r"""(?:\b(?:window|document|top|self)\.)?location(?:\.href)?\s*=\s*(['"])([^'"\r\n]{1,2048})\1""",
        re.I,
    )
    CALL = re.compile(
        r"""\b(?:window\.)?location\.(?:assign|replace)\s*\(\s*(['"])([^'"\r\n]{1,2048})\1""",
        re.I,
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets: list[tuple[str, str]] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self._in_script = True
            self._script_parts = []
        elif tag == 'meta' and str(attrs.get('http-equiv') or '').lower().strip() == 'refresh':
            content = str(attrs.get('content') or '')
            match = re.search(r'(?:^|;)\s*url\s*=\s*(.*)$', content, re.I)
            if match:
                self.targets.append(('meta_refresh', match.group(1).strip().strip('\"\'')))

    def handle_data(self, data: str):
        if self._in_script and sum(map(len, self._script_parts)) < 200_000:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag == 'script' and self._in_script:
            script = ''.join(self._script_parts)
            for expression in (self.ASSIGN, self.CALL):
                for match in expression.finditer(script):
                    self.targets.append(('js_literal', match.group(2)))
                    if len(self.targets) >= 20:
                        break
            self._script_parts = []
            self._in_script = False


def inspect_html_redirects(html: str, *, page_url: str,
                           seed: str, max_results: int = 8) -> tuple[RedirectObservation, ...]:
    parser = DeclarativeRedirectParser()
    parser.feed(html[:200_000])
    observations = {}
    for method, destination in parser.targets[:20]:
        item = make_redirect(page_url, destination, seed=seed, method=method)
        if item:
            observations[(item.source_url, item.destination_url, item.method)] = item
        if len(observations) >= max_results:
            break
    return tuple(observations.values())


@dataclass
class FetchResult:
    status: int
    final_url: str
    text: str
    error: str = ''
    redirects: tuple[RedirectObservation, ...] = ()
    # Passive hints, not verified JavaScript navigation or abuse findings.
    js_review_hints: tuple[str, ...] = ()


# Fallback is a deliberate opt-in, not a way to bypass robots access rules.
# Only failures opening HTTPS are eligible. DNS errors and security-policy
# failures are deliberately excluded: HTTP would not make them trustworthy.
HTTPS_FALLBACK_ERRORS = frozenset({
    'TimeoutError', 'ConnectionRefusedError', 'ConnectionResetError',
    'ConnectionAbortedError', 'BrokenPipeError', 'RemoteDisconnected',
    'SSLError', 'SSLCertVerificationError', 'SSLEOFError',
})


def can_fallback_to_http(result: FetchResult) -> bool:
    """A transport failure, never a response, redirect, or scope violation."""
    return (result.status == 0 and not result.redirects
            and result.error in HTTPS_FALLBACK_ERRORS)


def fetch_failure_kind(result: FetchResult) -> str:
    """Bounded public error category; never include URL or exception text."""
    if result.error in ('gaierror', 'dns_no_answers'):
        return 'dns'
    if result.error == 'TimeoutError':
        return 'timeout'
    if result.error in ('SSLError', 'SSLCertVerificationError', 'SSLEOFError'):
        return 'tls'
    if result.error in ('ConnectionRefusedError', 'ConnectionResetError',
                        'ConnectionAbortedError', 'BrokenPipeError', 'RemoteDisconnected'):
        return 'connection'
    if result.error == 'redirect_origin_requires_robots':
        return 'origin_policy_required'
    if result.error == 'robots_redirect_disallow':
        return 'robots_denied_redirect'
    if result.error == 'redirect_out_of_scope':
        return 'external_redirect'
    if result.error in ('out_of_scope', 'redirect_invalid', 'ValueError',
                        'dns_non_public_address'):
        return 'unsafe_or_invalid'
    if result.status in (401, 403):
        return 'access_denied'
    if result.status in (429, 503):
        return 'rate_limited'
    if result.status >= 400:
        return 'http_status'
    if result.error in ('non_text', 'oversize', 'redirect_limit'):
        return result.error
    return 'other'


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def public_ip_for(host: str, port: int) -> str:
    """Reject mixed private/public DNS answers; connect to one validated address."""
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise ValueError('dns_no_answers')
    ips = []
    for entry in answers:
        ip = ipaddress.ip_address(entry[4][0].split('%', 1)[0])
        if not ip.is_global:
            raise ValueError('dns_non_public_address')
        ips.append(str(ip))
    return ips[0]


class SafeFetcher:
    """Fetch only .id hosts, but *record* off-scope redirect targets."""
    def __init__(self, *, timeout: float, max_bytes: int, max_redirects: int = 3,
                 min_host_interval: float = 0.5, advanced_js: bool = False):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.min_host_interval = min_host_interval
        self.advanced_js = advanced_js
        self._next_host_access: dict[str, float] = {}

    def get(self, url: str, *, seed: str,
            redirect_allowed: Callable[[str], bool] | None = None) -> FetchResult:
        current = normalize_url(url)
        if current is None or not within_seed(current, seed):
            return FetchResult(0, url, '', 'out_of_scope')
        observed: list[RedirectObservation] = []
        if redirect_allowed is None:
            redirect_allowed = getattr(self, '_redirect_allowed', None)
        for _ in range(self.max_redirects + 1):
            parsed = urlsplit(current)
            port = 443 if parsed.scheme == 'https' else 80
            connection = None
            try:
                due = self._next_host_access.get(parsed.hostname, 0.0)
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_host_access[parsed.hostname] = (
                    time.monotonic() + self.min_host_interval)
                ip = public_ip_for(parsed.hostname, port)
                factory = PinnedHTTPSConnection if parsed.scheme == 'https' else PinnedHTTPConnection
                connection = factory(parsed.hostname, ip, port, self.timeout)
                connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), headers={
                    'Host': parsed.hostname,
                    'User-Agent': USER_AGENT,
                    'Accept': 'text/html,application/xml,text/xml,text/plain;q=0.8',
                    'Accept-Encoding': 'identity',
                    'Connection': 'close',
                })
                response = connection.getresponse()
                status = response.status
                if status in HTTP_REDIRECTS:
                    destination = response.getheader('Location', '')
                    item = make_redirect(current, destination, seed=seed,
                                         method='http', status=status) if destination else None
                    if item is None:
                        return FetchResult(status, normalize_url(current) or current, '', 'redirect_invalid', tuple(observed))
                    observed.append(item)
                    # External landing pages are NOT fetched by discovery.
                    if item.external:
                        return FetchResult(status, normalize_url(current) or current, '', 'redirect_out_of_scope', tuple(observed))
                    # A redirect from apex to www (or HTTPS to HTTP) crosses an
                    # origin boundary. Never fetch that new origin before its
                    # own robots.txt has been checked by the caller.
                    origin = urlsplit(current)
                    target = urlsplit(item.destination_url)
                    if (origin.scheme, origin.hostname) != (target.scheme, target.hostname):
                        return FetchResult(status, normalize_url(current) or current, '',
                                           'redirect_origin_requires_robots', tuple(observed))
                    # Every redirect within the checked origin must also obey
                    # the origin's robots policy before connecting to its path.
                    # Query may be used for the robots decision, but never logged.
                    next_url = urljoin(current, destination.strip())
                    if redirect_allowed is not None and not redirect_allowed(next_url):
                        return FetchResult(status, normalize_url(current) or current,
                                           '', 'robots_redirect_disallow', tuple(observed))
                    # Follow internal same-origin redirects, maintaining IP pinning
                    # and public-IP checks at every single network connection.
                    # Keep an in-scope redirect's query during the HTTP request;
                    # do not record it in redirect evidence or output.
                    resolved = urlsplit(next_url)
                    current = urlunsplit((urlsplit(item.destination_url).scheme,
                                          urlsplit(item.destination_url).netloc,
                                          urlsplit(item.destination_url).path,
                                          resolved.query, ''))
                    continue
                if status != 200:
                    return FetchResult(status, normalize_url(current) or current, '', 'http_status', tuple(observed))
                ct = (response.getheader('Content-Type') or '').lower()
                if not any(marker in ct for marker in ('html', 'xml', 'text/')):
                    return FetchResult(status, normalize_url(current) or current, '', 'non_text', tuple(observed))
                length = response.getheader('Content-Length')
                if length and length.isdigit() and int(length) > self.max_bytes:
                    return FetchResult(status, normalize_url(current) or current, '', 'oversize', tuple(observed))
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    return FetchResult(status, normalize_url(current) or current, '', 'oversize', tuple(observed))
                text = raw.decode('utf-8', 'replace')
                hints: set[str] = set()
                if 'html' in ct:
                    observed.extend(inspect_html_redirects(text, page_url=current, seed=seed))
                    if self.advanced_js:
                        # Advanced signals are *passive*. External destinations are
                        # never fetched, and static JS is not runtime confirmation.
                        already_seen = {(x.source_url, x.destination_url)
                                        for x in observed if x.method == 'js_literal'}
                        for signal in analyze_html(text):
                            if signal.kind == 'js_dynamic':
                                hints.add('js_dynamic_location')
                                continue
                            if signal.kind == 'js_literal':
                                # Preserve legacy literal parser's observations.
                                continue
                            item = make_redirect(current, signal.target or '',
                                                 seed=seed, method=signal.kind)
                            if item and (item.source_url, item.destination_url) not in already_seen:
                                observed.append(item)
                                already_seen.add((item.source_url, item.destination_url))
                return FetchResult(status, normalize_url(current) or current, text,
                                   '', tuple(observed), tuple(sorted(hints)))
            except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
                return FetchResult(0, normalize_url(current) or current, '', type(exc).__name__, tuple(observed))
            finally:
                if connection is not None:
                    connection.close()
        return FetchResult(0, normalize_url(current) or current, '', 'redirect_limit', tuple(observed))


def parse_sitemap(xml_text: str, *, max_urls: int = 2000) -> tuple[list[str], list[str]]:
    """Return (<loc> page URLs, child sitemap URLs); no external XML fetching."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []
    tag = root.tag.rsplit('}', 1)[-1].lower()
    if tag not in ('urlset', 'sitemapindex'):
        return [], []
    locs = []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1].lower() == 'loc' and node.text:
            locs.append(node.text.strip())
            if len(locs) >= max_urls:
                break
    return (locs, []) if tag == 'urlset' else ([], locs)


def ct_subdomains(seed: str, *, timeout: float = 8, max_bytes: int = 400_000) -> list[str]:
    """Optional passive CT lookup; sends only this public .id seed to crt.sh."""
    query_url = 'https://crt.sh/?' + urlencode({'q': '%.' + seed, 'output': 'json'})
    request = Request(query_url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return []
    records = json.loads(raw)
    if not isinstance(records, list):
        return []
    names = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for name in str(record.get('name_value') or '').splitlines():
            name = name.strip().removeprefix('*.')
            url = normalize_url(name)
            if url and within_seed(url, seed) and host_of(url) != seed:
                names.add(url)
    return sorted(names)


def canonical_www_origin(result: FetchResult, *, origin: str,
                         source_path: str, seed: str) -> str | None:
    """Allow only exact apex -> www apex redirects with the same scheme/path.

    The new origin is NOT authorized by the old robots file; the caller must
    GET www.<seed>/robots.txt independently before visiting any www page.
    """
    if (result.error != 'redirect_origin_requires_robots' or
            not result.redirects or seed.startswith('www.')):
        return None
    hop = result.redirects[-1]
    src, dest, requested = (urlsplit(hop.source_url),
                            urlsplit(hop.destination_url), urlsplit(origin))
    if (hop.method != 'http' or hop.external or
            src.scheme != requested.scheme or src.hostname != seed or
            src.path != source_path or dest.scheme != requested.scheme or
            dest.hostname != 'www.' + seed or dest.path != source_path):
        return None
    return f'{dest.scheme}://{dest.hostname}/'


def discover_seed(seed: str, *, fetch: Callable[[str], FetchResult] | None,
                  include_ct: bool = False, ct_lookup: Callable[[str], list[str]] | None = None,
                  max_candidates: int = 30, max_sitemaps: int = 2,
                  probe_candidates: int = 0,
                  redirect_observer: Callable[[RedirectObservation], None] | None = None,
                  fetch_with_robots: Callable[[str, object | None], FetchResult] | None = None,
                  http_fallback: bool = False,
                  canonical_www: bool = False,
                  browser_review_limit: int = 0,
                  ) -> tuple[list[Candidate], dict]:
    """Conservative discovery; never trust apex robots for a www origin.

    Canonical www handling is opt-in and bounded to one verified host switch.
    The fetcher stops at cross-origin redirects before fetching the new host.
    """
    found: dict[str, Candidate] = {}
    robots_policy = None
    root_url = 'https://' + seed + '/'
    allowed = False
    counters = {'web_requests': 0, 'web_errors': 0, 'ct_errors': 0,
                'redirect_observations': 0, 'external_redirects': 0,
                'probed_pages': 0, 'probe_errors': 0,
                'http_fallback_attempts': 0, 'http_fallback_success': 0,
                'http_fallback_blocked': 0, 'fetch_failure_types': {},
                'canonical_www_attempts': 0, 'canonical_www_verified': 0,
                'canonical_www_blocked': 0, 'canonical_www_homepage_success': 0,
                'robots_reasons': {}, 'http_response_codes': {},
                'redirect_stop_reasons': {}, 'js_static_redirects': 0,
                'js_dynamic_pages': 0, 'browser_review_candidates': []}
    review_by_key: dict[tuple[str, str, str], dict] = {}
    last_robots_reason = 'not_checked'
    homepage_status = 'not_checked'

    def tally(key: str, value: str):
        group = counters[key]
        group[value] = group.get(value, 0) + 1

    def add_review(source: str, reason: str, score: int, destination: str = ''):
        if browser_review_limit <= 0:
            return
        # Only redacted .id source URLs and redacted evidence destinations.
        safe_source = normalize_url(source)
        if safe_source is None:
            return
        key = (safe_source, reason, destination)
        review_by_key[key] = {'source': safe_source, 'reason': reason,
                              'priority': score, 'destination': destination or None}

    def observe(result: FetchResult):
        if result.status:
            tally('http_response_codes', str(result.status))
        if result.error in ('redirect_origin_requires_robots', 'redirect_out_of_scope',
                            'redirect_limit', 'redirect_invalid', 'robots_redirect_disallow'):
            tally('redirect_stop_reasons', result.error)
        if result.js_review_hints:
            counters['js_dynamic_pages'] += 1
            for reason in result.js_review_hints:
                add_review(result.final_url, reason, 30)
        for item in result.redirects:
            counters['redirect_observations'] += 1
            counters['external_redirects'] += int(item.external)
            if item.method.startswith('js_'):
                counters['js_static_redirects'] += 1
            # robots.txt redirects are access-control events, not a browser
            # verification priority signal for the website's actual content.
            if item.external and not urlsplit(item.source_url).path.endswith('/robots.txt'):
                if item.method == 'http':
                    add_review(item.source_url, 'external_http_redirect', 95,
                               item.destination_url)
                elif item.method == 'meta_refresh':
                    add_review(item.source_url, 'external_meta_refresh', 85,
                               item.destination_url)
                elif item.method.startswith('js_'):
                    add_review(item.source_url, 'external_js_static', 75,
                               item.destination_url)
            if redirect_observer is not None:
                redirect_observer(item)

    def note_failure(result: FetchResult):
        tally('fetch_failure_types', fetch_failure_kind(result))

    def fetch_page(url: str):
        # Production provides a SafeFetcher wrapper that enforces robots on
        # *each* same-origin HTTP redirect; mocks keep their existing API.
        return (fetch_with_robots(url, robots_policy)
                if fetch_with_robots is not None else fetch(url))

    def add(value: str, source: str):
        item = make_candidate(value, seed=seed, source=source)
        if item is not None:
            # Only the *selected* origin has been robots-verified; other
            # subdomains may be retained as candidates but are never probed.
            if (robots_policy is not None and host_of(item.url) == host_of(root_url) and
                    urlsplit(item.url).scheme == urlsplit(root_url).scheme):
                if not robots_policy.can_fetch(USER_AGENT, item.url):
                    return
            previous = found.get(item.url)
            if previous is None or previous.priority_score < item.priority_score:
                found[item.url] = item

    def get_robots(origin: str):
        """Fetch robots for this exact origin, not for a redirected host."""
        nonlocal last_robots_reason
        response = fetch(origin + 'robots.txt')
        counters['web_requests'] += 1
        observe(response)
        category = fetch_failure_kind(response)
        if response.error == 'redirect_origin_requires_robots':
            reason = 'robots_origin_redirect'
        elif response.error == 'redirect_out_of_scope':
            reason = 'robots_external_redirect'
        elif category in ('dns', 'timeout', 'tls', 'connection'):
            reason = 'robots_' + category
        elif response.status in (401, 403):
            reason = 'robots_access_denied'
        elif response.status in (429, 503):
            reason = 'robots_rate_limited'
        elif response.error in ('oversize', 'non_text'):
            reason = 'robots_invalid_content'
        elif not ((response.status == 404 and response.error in ('', 'http_status')) or
                  (response.status == 200 and not response.error)):
            reason = 'robots_http_error'
        else:
            actual = urlsplit(response.final_url)
            expected = urlsplit(origin)
            if ((actual.scheme, actual.hostname) != (expected.scheme, expected.hostname) or
                    actual.path != '/robots.txt'):
                # In particular, a 200 response from /auth/signin is NOT a robots grant.
                reason = 'robots_origin_or_path_mismatch'
                tally('fetch_failure_types', 'unsafe_or_invalid')
            elif response.status == 404:
                reason = 'robots_not_found'
                last_robots_reason = reason
                tally('robots_reasons', reason)
                return True, None, [], response, origin
            else:
                policy = robotparser.RobotFileParser()
                policy.parse(response.text.splitlines())
                sitemaps = []
                for line in response.text.splitlines():
                    if line.lower().lstrip().startswith('sitemap:'):
                        sitemaps.append(line.partition(':')[2].strip())
                allowed_origin = policy.can_fetch(USER_AGENT, origin)
                reason = 'robots_allowed' if allowed_origin else 'robots_disallow'
                last_robots_reason = reason
                tally('robots_reasons', reason)
                return allowed_origin, policy, sitemaps, response, origin
        last_robots_reason = reason
        tally('robots_reasons', reason)
        if reason != 'robots_origin_or_path_mismatch' and reason != 'robots_disallow':
            note_failure(response)
        return False, None, [], response, origin

    def verify_canonical_www(response: FetchResult, *, previous_origin: str,
                             path: str):
        if not canonical_www or counters['canonical_www_attempts']:
            return None
        canonical = canonical_www_origin(response, origin=previous_origin,
                                         source_path=path, seed=seed)
        if canonical is None:
            return None
        counters['canonical_www_attempts'] += 1
        checked = get_robots(canonical)
        if checked[0]:
            counters['canonical_www_verified'] += 1
        else:
            counters['canonical_www_blocked'] += 1
        return checked

    if fetch is not None:
        allowed, robots_policy, sitemaps, robots, root_url = get_robots(root_url)
        canonical = verify_canonical_www(robots, previous_origin=root_url,
                                         path='/robots.txt')
        if canonical is not None:
            allowed, robots_policy, sitemaps, robots, root_url = canonical
        fallback_tried = False
        if (not allowed and robots.status not in (200, 404) and
                http_fallback and can_fallback_to_http(robots)):
            fallback_tried = True
            counters['http_fallback_attempts'] += 1
            root_url = 'http://' + seed + '/'
            allowed, robots_policy, sitemaps, robots, root_url = get_robots(root_url)
            canonical = verify_canonical_www(robots, previous_origin=root_url,
                                             path='/robots.txt')
            if canonical is not None:
                allowed, robots_policy, sitemaps, robots, root_url = canonical
            if not allowed:
                counters['http_fallback_blocked'] += 1
        if not allowed:
            counters['web_errors'] += 1
        else:
            homepage_origin = root_url
            homepage = fetch_page(root_url)
            counters['web_requests'] += 1
            observe(homepage)
            if homepage.status != 200 or homepage.error:
                note_failure(homepage)
            canonical = verify_canonical_www(homepage, previous_origin=homepage_origin,
                                             path='/')
            if canonical is not None:
                allowed, robots_policy, sitemaps, robots, root_url = canonical
                if allowed:
                    homepage = fetch_page(root_url)
                    counters['web_requests'] += 1
                    observe(homepage)
                    if homepage.status != 200 or homepage.error:
                        note_failure(homepage)
            # An HTTPS robots grant is not a grant for HTTP. Check HTTP robots
            # independently on *transport* failures, never HTTP 403 / redirect.
            if (allowed and http_fallback and not fallback_tried and
                    root_url.startswith('https://') and can_fallback_to_http(homepage)):
                fallback_tried = True
                counters['http_fallback_attempts'] += 1
                http_root = 'http://' + seed + '/'
                http_allowed, http_policy, http_maps, http_robots, http_root = get_robots(http_root)
                canonical = verify_canonical_www(http_robots, previous_origin=http_root,
                                                 path='/robots.txt')
                if canonical is not None:
                    http_allowed, http_policy, http_maps, http_robots, http_root = canonical
                if http_allowed:
                    allowed = True
                    robots_policy, sitemaps, root_url = http_policy, http_maps, http_root
                    homepage = fetch_page(root_url)
                    counters['web_requests'] += 1
                    observe(homepage)
                    if homepage.status != 200 or homepage.error:
                        note_failure(homepage)
                else:
                    allowed = False
                    counters['http_fallback_blocked'] += 1
                    counters['web_errors'] += 1
            if allowed:
                # Mocked or future fetchers must not silently bypass origin policy.
                same_origin = (urlsplit(homepage.final_url).scheme,
                               host_of(homepage.final_url)) == (urlsplit(root_url).scheme,
                                                               host_of(root_url))
                if homepage.status == 200 and not homepage.error and same_origin:
                    homepage_status = 'ok'
                    if (counters['canonical_www_verified'] and
                            host_of(root_url) == 'www.' + seed):
                        counters['canonical_www_homepage_success'] += 1
                    parser = Links()
                    parser.feed(homepage.text)
                    for link in parser.links:
                        add(urljoin(homepage.final_url, link), 'homepage')
                else:
                    homepage_status = 'origin_mismatch' if not same_origin else fetch_failure_kind(homepage)
                    if homepage_status == 'origin_mismatch':
                        tally('fetch_failure_types', 'unsafe_or_invalid')
                    if homepage.error != 'redirect_out_of_scope':
                        counters['web_errors'] += 1
                if fallback_tried and ((homepage.status == 200 and not homepage.error and same_origin) or
                                       homepage.error == 'redirect_out_of_scope'):
                    counters['http_fallback_success'] += 1
                if not sitemaps:
                    sitemaps.append(root_url + 'sitemap.xml')
                visited_sitemaps = set()
                while sitemaps and len(visited_sitemaps) < max_sitemaps:
                    sitemap = normalize_url(sitemaps.pop(0))
                    if (not sitemap or host_of(sitemap) != host_of(root_url) or
                            urlsplit(sitemap).scheme != urlsplit(root_url).scheme or
                            sitemap in visited_sitemaps):
                        continue
                    if robots_policy is not None and not robots_policy.can_fetch(USER_AGENT, sitemap):
                        continue
                    visited_sitemaps.add(sitemap)
                    doc = fetch_page(sitemap)
                    counters['web_requests'] += 1
                    observe(doc)
                    if doc.status != 200 or doc.error or (urlsplit(doc.final_url).scheme,
                           host_of(doc.final_url)) != (urlsplit(root_url).scheme, host_of(root_url)):
                        note_failure(doc)
                        counters['web_errors'] += 1
                        continue
                    pages, nested = parse_sitemap(doc.text)
                    for page in pages:
                        add(page, 'sitemap')
                    sitemaps.extend(nested)
    if include_ct and ct_lookup is not None:
        try:
            for url in ct_lookup(seed):
                add(url, 'ct')
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
            counters['ct_errors'] += 1
    ordered = sorted(found.values(), key=lambda c: (-c.priority_score, c.url))[:max_candidates]
    if fetch is not None and probe_candidates > 0 and allowed:
        for item in (x for x in ordered if host_of(x.url) == host_of(root_url) and
                     urlsplit(x.url).scheme == urlsplit(root_url).scheme):
            if counters['probed_pages'] >= probe_candidates:
                break
            if robots_policy is not None and not robots_policy.can_fetch(USER_AGENT, item.url):
                continue
            result = fetch_page(item.url)
            counters['probed_pages'] += 1
            counters['web_requests'] += 1
            observe(result)
            if result.status != 200 or result.error:
                note_failure(result)
                if result.error != 'redirect_out_of_scope':
                    counters['probe_errors'] += 1
            if any(obs.external and obs.source_url == item.url and
                   obs.method in ('http', 'meta_refresh') for obs in result.redirects):
                found[item.url] = Candidate(item.url, item.source,
                                            item.seed_hostname,
                                            min(100, item.priority_score + 30))
        ordered = sorted(found.values(), key=lambda c: (-c.priority_score, c.url))[:max_candidates]
    counters['browser_review_candidates'] = sorted(
        review_by_key.values(), key=lambda item: (-item['priority'], item['source'],
                                                   item['reason'], item['destination'] or ''))[:browser_review_limit]
    counters['diagnostic'] = {
        'seed': seed, 'selected_origin': root_url if allowed else None,
        'robots_outcome': last_robots_reason, 'homepage_outcome': homepage_status,
        'candidates': len(ordered),
        'robots_reasons': dict(counters['robots_reasons']),
        'http_response_codes': dict(counters['http_response_codes']),
        'fetch_failure_types': dict(counters['fetch_failure_types']),
        'redirect_stop_reasons': dict(counters['redirect_stop_reasons']),
        'canonical_www_attempts': counters['canonical_www_attempts'],
        'canonical_www_verified': counters['canonical_www_verified'],
        'canonical_www_blocked': counters['canonical_www_blocked'],
        'js_static_redirects': counters['js_static_redirects'],
        'js_dynamic_pages': counters['js_dynamic_pages'],
    }
    return ordered, counters


def open_jobs_db(path: Path, *, apply: bool) -> sqlite3.Connection | None:
    if not path.is_file():
        if apply:
            raise FileNotFoundError(f'Existing BIMA SQLite DB tidak ditemukan: {path}')
        return None
    # Use URI mode=rw/ro to avoid silently creating a new 'production' database.
    uri = path.resolve().as_uri() + ('?mode=rw' if apply else '?mode=ro')
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        conn.close()
        raise ValueError('BIMA database tidak memiliki tabel jobs')
    return conn


def state_of(conn: sqlite3.Connection | None) -> tuple[str | None, int]:
    if conn is None:
        return None, 0
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_discovery_state'").fetchone()
    if not exists:
        return None, 0
    rows = dict(conn.execute('SELECT key, value FROM proactive_discovery_state'))
    try:
        index = max(0, int(rows.get('cursor', '0')))
    except ValueError:
        index = 0
    return rows.get('input_sha256'), index


def pending_count(conn: sqlite3.Connection | None) -> int:
    if conn is None:
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0])


def apply_candidates(conn: sqlite3.Connection, *, candidates: list[Candidate],
                     input_digest: str, next_cursor: int, max_new: int,
                     max_pending: int,
                     redirects: list[RedirectObservation] | None = None) -> tuple[int, int, int]:
    """Persist ALL finds, queue a bounded batch, and advance cursor atomically.

    Candidates that do not fit in today's job backlog remain queued=0 here;
    subsequent runs can promote them without re-fetching that seed.
    """
    conn.executescript(SCHEMA)
    inserted = 0
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        timestamp = utcnow()
        for item in candidates:
            conn.execute("""INSERT INTO proactive_discovery_candidates
                (url,seed_hostname,source,priority_score,first_seen,last_seen,queued)
                VALUES (?,?,?,?,?,?,0)
                ON CONFLICT(url) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  priority_score=MAX(proactive_discovery_candidates.priority_score,
                                     excluded.priority_score)
                """, (item.url, item.seed_hostname, item.source, item.priority_score,
                      timestamp, timestamp))
        for observation in (redirects or []):
            conn.execute("""INSERT INTO proactive_redirect_observations
                (seed_hostname,source_url,destination_url,method,http_status,
                 external,first_seen,last_seen,review_status)
                VALUES (?,?,?,?,?,?,?,?,'new')
                ON CONFLICT(source_url,destination_url,method) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  http_status=excluded.http_status
                """, (observation.seed_hostname, observation.source_url,
                      observation.destination_url, observation.method,
                      observation.http_status, int(observation.external),
                      timestamp, timestamp))
        pending = pending_count(conn)
        if pending < max_pending:
            # Scan more than max_new, so URLs already present in jobs do not
            # starve a batch of fresh candidates. Every row is idempotent.
            entries = conn.execute("""SELECT url FROM proactive_discovery_candidates
                WHERE queued=0 ORDER BY priority_score DESC, first_seen ASC, url ASC
                LIMIT ?""", (max(max_new * 5, 100),)).fetchall()
            for (url,) in entries:
                if inserted >= max_new or pending >= max_pending:
                    break
                result = conn.execute('INSERT OR IGNORE INTO jobs(url,updated_at) VALUES (?,?)',
                                      (url, timestamp))
                if result.rowcount:
                    inserted += 1
                    pending += 1
                conn.execute('UPDATE proactive_discovery_candidates SET queued=1 WHERE url=?',
                             (url,))
        held = int(conn.execute('SELECT COUNT(*) FROM proactive_discovery_candidates WHERE queued=0').fetchone()[0])
        conn.executemany('INSERT OR REPLACE INTO proactive_discovery_state(key,value) VALUES (?,?)',
                         [('input_sha256', input_digest), ('cursor', str(next_cursor))])
    return inserted, pending, held


def run(args) -> dict:
    seeds, digest, invalid = load_seeds(Path(args.input))
    if not seeds:
        raise ValueError('Tidak ada seed domain .id valid di urls.json')
    conn = open_jobs_db(Path(args.db), apply=args.apply)
    try:
        saved_digest, saved_cursor = state_of(conn)
        start = args.start_index if args.start_index is not None else (
            saved_cursor if saved_digest == digest else 0)
        start %= len(seeds)
        size = min(args.seed_limit, len(seeds))
        current_seeds = [seeds[(start + i) % len(seeds)] for i in range(size)]
        next_cursor = (start + size) % len(seeds)
        before = pending_count(conn)
        # Keep the v4.2.1 default constructor unchanged (also for integrations
        # that provide their own SafeFetcher); opt in to advanced parsing.
        fetcher_options = ({'advanced_js': True} if getattr(args, 'advanced_js', False) else {})
        fetcher = SafeFetcher(timeout=args.timeout, max_bytes=args.max_response_bytes,
                              min_host_interval=args.min_host_interval,
                              **fetcher_options)
        candidates: dict[str, Candidate] = {}
        stats = {'web_requests': 0, 'web_errors': 0, 'ct_errors': 0,
                 'redirect_observations': 0, 'external_redirects': 0,
                 'probed_pages': 0, 'probe_errors': 0,
                 'http_fallback_attempts': 0, 'http_fallback_success': 0,
                 'http_fallback_blocked': 0, 'fetch_failure_types': {},
                 'canonical_www_attempts': 0, 'canonical_www_verified': 0,
                 'canonical_www_blocked': 0, 'canonical_www_homepage_success': 0,
                 'robots_reasons': {}, 'http_response_codes': {},
                 'redirect_stop_reasons': {}, 'js_static_redirects': 0,
                 'js_dynamic_pages': 0}
        review_candidates: list[dict] = []
        seed_diagnostics: list[dict] = []
        redirects_by_key: dict[tuple[str, str, str], RedirectObservation] = {}
        def observe_redirect(item: RedirectObservation):
            redirects_by_key[(item.source_url, item.destination_url, item.method)] = item

        for seed in current_seeds:
            def fetch(url: str, *, _seed=seed):
                return fetcher.get(url, seed=_seed)
            def fetch_with_robots(url: str, policy, *, _seed=seed):
                # No policy (robots 404) means no disallow directives.
                allowed_redirect = (lambda target: policy.can_fetch(USER_AGENT, target)) \
                    if policy is not None else None
                # Single-threaded Discovery: scope this policy to one request,
                # without changing legacy SafeFetcher.get callers/mocks.
                previous = getattr(fetcher, '_redirect_allowed', None)
                fetcher._redirect_allowed = allowed_redirect
                try:
                    return fetcher.get(url, seed=_seed)
                finally:
                    fetcher._redirect_allowed = previous
            items, counters = discover_seed(
                seed, fetch=fetch if args.scan_web else None,
                include_ct=args.ct,
                ct_lookup=(lambda name: ct_subdomains(name, timeout=args.timeout)) if args.ct else None,
                max_candidates=args.max_candidates_per_seed,
                max_sitemaps=args.max_sitemaps,
                probe_candidates=args.probe_candidates,
                redirect_observer=observe_redirect,
                fetch_with_robots=fetch_with_robots if args.scan_web else None,
                http_fallback=getattr(args, 'http_fallback', False),
                canonical_www=getattr(args, 'canonical_www', False),
                browser_review_limit=getattr(args, 'browser_review_limit', 0))
            if len(seed_diagnostics) < getattr(args, 'diagnostic_seeds', 0):
                seed_diagnostics.append(counters['diagnostic'])
            for k, v in counters.items():
                if k == 'diagnostic':
                    continue
                if k == 'browser_review_candidates':
                    review_candidates.extend(v)
                    continue
                if k in ('fetch_failure_types', 'robots_reasons',
                         'http_response_codes', 'redirect_stop_reasons'):
                    for category, count in v.items():
                        totals = stats[k]
                        totals[category] = totals.get(category, 0) + count
                else:
                    stats[k] += v
            for item in items:
                old = candidates.get(item.url)
                if old is None or item.priority_score > old.priority_score:
                    candidates[item.url] = item
            if args.request_interval > 0 and (args.scan_web or args.ct):
                time.sleep(args.request_interval)
        ordered = sorted(candidates.values(), key=lambda c: (-c.priority_score, c.url))
        redirect_events = list(redirects_by_key.values())
        new = 0
        after = before
        held = 0
        if args.apply:
            assert conn is not None
            new, after, held = apply_candidates(conn, candidates=ordered, input_digest=digest,
                                          next_cursor=next_cursor, max_new=args.max_new,
                                          max_pending=args.max_pending,
                                          redirects=redirect_events)
        return {
            'mode': 'apply' if args.apply else 'preview',
            'seeds_total': len(seeds), 'seeds_this_run': len(current_seeds),
            'invalid_seed_entries': invalid, 'start_index': start,
            'next_index': next_cursor, 'candidate_urls': len(ordered),
            'new_jobs': new, 'held_candidates': held,
            'pending_before': before, 'pending_after': after,
            'scan_web': args.scan_web, 'ct': args.ct,
            'http_fallback': getattr(args, 'http_fallback', False),
            'canonical_www': getattr(args, 'canonical_www', False),
            'advanced_js': getattr(args, 'advanced_js', False), **stats,
            'preview_diagnostics': seed_diagnostics if not args.apply else [],
            'preview_browser_review': sorted(
                {((item['source'], item['reason'], item['destination'] or '')): item
                 for item in review_candidates}.values(),
                key=lambda item: (-item['priority'], item['source'], item['reason']))
                [:getattr(args, 'browser_review_limit', 0)] if not args.apply else [],
            'preview_redirect_chains': build_redirect_chains(
                redirect_events, limit=getattr(args, 'redirect_chain_preview', 0))
                if not args.apply else [],
            'unique_redirects': len(redirect_events),
            'preview_redirects': [
                {'source': item.source_url, 'destination': item.destination_url,
                 'method': item.method, 'http_status': item.http_status,
                 'external': item.external}
                for item in redirect_events[:min(10, args.preview_samples)]
            ],
            'preview_candidates': [
                {'url': item.url, 'source': item.source, 'priority': item.priority_score}
                for item in ordered[:min(10, args.preview_samples)]
            ],
        }
    finally:
        if conn is not None:
            conn.close()


def arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='BIMA .id proactive discovery (PREVIEW by default)')
    parser.add_argument('--input', default='urls.json', help='Registry-derived urls.json (read-only)')
    parser.add_argument('--db', default='multi/state/crawler_production.sqlite3')
    parser.add_argument('--scan-web', action='store_true', help='Fetch public .id homepage/robots/sitemap')
    parser.add_argument('--http-fallback', action='store_true',
                        help='Opt-in: HTTPS transport failure -> HTTP only after separate robots check')
    parser.add_argument('--canonical-www', action='store_true',
                        help='Opt-in: independently check www.<seed>/robots.txt on apex redirect')
    parser.add_argument('--advanced-js', action='store_true',
                        help='Opt-in: passive decode of bounded inline JS navigation literals; no execution')
    parser.add_argument('--browser-review-limit', type=int, default=0,
                        help='Preview up to N manual browser review leads (0 disables; NO browser launched)')
    parser.add_argument('--redirect-chain-preview', type=int, default=0,
                        help='Preview up to N observed HTTP chains and passive JS/meta signals')
    parser.add_argument('--diagnostic-seeds', type=int, default=0,
                        help='Preview per-seed safe diagnostics for up to 100 seeds (0 disables)')
    parser.add_argument('--ct', action='store_true', help='Optional crt.sh public CT lookup (sends seed hostname)')
    parser.add_argument('--apply', action='store_true', help='ENQUEUE jobs into crawler DB; may trigger BIMA alerts')
    parser.add_argument('--seed-limit', type=int, default=20)
    parser.add_argument('--start-index', type=int, default=None, help='Preview offset override')
    parser.add_argument('--max-candidates-per-seed', type=int, default=30)
    parser.add_argument('--max-new', type=int, default=30)
    parser.add_argument('--max-pending', type=int, default=1000)
    parser.add_argument('--max-sitemaps', type=int, default=2)
    parser.add_argument('--probe-candidates', type=int, default=0,
                        help='Fetch up to N discovered same-host pages per seed (0 disables)')
    parser.add_argument('--max-response-bytes', type=int, default=300_000)
    parser.add_argument('--timeout', type=float, default=6.0)
    parser.add_argument('--request-interval', type=float, default=0.25)
    parser.add_argument('--min-host-interval', type=float, default=0.5)
    parser.add_argument('--preview-samples', type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = arg_parser().parse_args(argv)
    if args.seed_limit < 1 or args.max_candidates_per_seed < 1 or args.max_sitemaps < 0:
        raise SystemExit('seed-limit/candidates-per-seed must be positive and max-sitemaps >= 0')
    if args.max_new < 1 or args.max_pending < 1 or args.max_response_bytes < 1024:
        raise SystemExit('max-new/max-pending must be positive, max-response-bytes >= 1024')
    if not 0 <= args.probe_candidates <= args.max_candidates_per_seed:
        raise SystemExit('probe-candidates must be 0..max-candidates-per-seed')
    if args.probe_candidates and not args.scan_web:
        raise SystemExit('--probe-candidates requires --scan-web')
    if args.http_fallback and not args.scan_web:
        raise SystemExit('--http-fallback requires --scan-web')
    if args.canonical_www and not args.scan_web:
        raise SystemExit('--canonical-www requires --scan-web')
    if args.advanced_js and not args.scan_web:
        raise SystemExit('--advanced-js requires --scan-web')
    if args.advanced_js and args.apply:
        raise SystemExit('--advanced-js is preview-only in v4.3 (no --apply)')
    if not 0 <= args.browser_review_limit <= 100 or not 0 <= args.redirect_chain_preview <= 100:
        raise SystemExit('browser-review-limit/redirect-chain-preview must be 0..100')
    if (args.browser_review_limit or args.redirect_chain_preview) and (not args.scan_web or args.apply):
        raise SystemExit('browser review/chain preview requires --scan-web and preview (no --apply)')
    if not 0 <= args.diagnostic_seeds <= 100:
        raise SystemExit('--diagnostic-seeds must be between 0 and 100')
    if args.diagnostic_seeds and (not args.scan_web or args.apply):
        raise SystemExit('--diagnostic-seeds requires --scan-web and preview (no --apply)')
    if args.timeout <= 0 or args.request_interval < 0 or args.preview_samples < 0 or args.min_host_interval < 0:
        raise SystemExit('timeout must be positive; interval/samples must be nonnegative')
    if args.start_index is not None and args.start_index < 0:
        raise SystemExit('start-index must be >= 0')
    try:
        result = run(args)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)[:300]}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
