#!/usr/bin/env python3
"""Conservative .id URL/subdomain discovery for the existing BIMA crawler.

Reads the existing Registry-derived urls.json as a *seed inventory* (never edits it).
Discovers links and sitemaps with public-IP-pinned HTTP and optional crt.sh lookups.
PREVIEW by default; --apply may append jobs to the existing SQLite queue.
No BIMA alert calls, browser use, or IndoBERT loading occur in this process.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
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


@dataclass
class FetchResult:
    status: int
    final_url: str
    text: str
    error: str = ''


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
    """Only .id HTTPS/HTTP, safe public IPs, pinned connects, and scoped redirects."""
    def __init__(self, *, timeout: float, max_bytes: int, max_redirects: int = 3,
                 min_host_interval: float = 0.5):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.min_host_interval = min_host_interval
        self._next_host_access: dict[str, float] = {}

    def get(self, url: str, *, seed: str) -> FetchResult:
        current = normalize_url(url)
        if current is None or not within_seed(current, seed):
            return FetchResult(0, url, '', 'out_of_scope')
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
                connection.request('GET', parsed.path or '/', headers={
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
                    next_url = normalize_url(urljoin(current, destination)) if destination else None
                    if next_url is None or not within_seed(next_url, seed):
                        return FetchResult(status, current, '', 'redirect_out_of_scope')
                    current = next_url
                    continue
                if status != 200:
                    return FetchResult(status, current, '', 'http_status')
                ct = (response.getheader('Content-Type') or '').lower()
                if not any(marker in ct for marker in ('html', 'xml', 'text/')):
                    return FetchResult(status, current, '', 'non_text')
                length = response.getheader('Content-Length')
                if length and length.isdigit() and int(length) > self.max_bytes:
                    return FetchResult(status, current, '', 'oversize')
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    return FetchResult(status, current, '', 'oversize')
                return FetchResult(status, current, raw.decode('utf-8', 'replace'))
            except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
                return FetchResult(0, current, '', type(exc).__name__)
            finally:
                if connection is not None:
                    connection.close()
        return FetchResult(0, current, '', 'redirect_limit')


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


def discover_seed(seed: str, *, fetch: Callable[[str], FetchResult] | None,
                  include_ct: bool = False, ct_lookup: Callable[[str], list[str]] | None = None,
                  max_candidates: int = 30, max_sitemaps: int = 2) -> tuple[list[Candidate], dict]:
    """Discover, but never fetch any discovered page or trigger BIMA alerts."""
    found: dict[str, Candidate] = {}
    robots_policy = None
    counters = {'web_requests': 0, 'web_errors': 0, 'ct_errors': 0}

    def add(value: str, source: str):
        item = make_candidate(value, seed=seed, source=source)
        if item is not None:
            if robots_policy is not None and host_of(item.url) == seed:
                if not robots_policy.can_fetch(USER_AGENT, item.url):
                    return
            previous = found.get(item.url)
            if previous is None or previous.priority_score < item.priority_score:
                found[item.url] = item

    if fetch is not None:
        root_url = 'https://' + seed + '/'
        robots = fetch('https://' + seed + '/robots.txt')
        counters['web_requests'] += 1
        allowed = robots.status == 404
        rp = robotparser.RobotFileParser()
        sitemaps = []
        if robots.status == 200:
            rp.parse(robots.text.splitlines())
            robots_policy = rp
            allowed = rp.can_fetch(USER_AGENT, root_url)
            for line in robots.text.splitlines():
                if line.lower().lstrip().startswith('sitemap:'):
                    sitemaps.append(line.partition(':')[2].strip())
        elif robots.status != 404:
            counters['web_errors'] += 1
        if allowed:
            homepage = fetch(root_url)
            counters['web_requests'] += 1
            if homepage.status == 200:
                parser = Links()
                parser.feed(homepage.text)
                for link in parser.links:
                    add(urljoin(homepage.final_url, link), 'homepage')
            else:
                counters['web_errors'] += 1
            if not sitemaps:
                sitemaps.append('https://' + seed + '/sitemap.xml')
            visited_sitemaps = set()
            while sitemaps and len(visited_sitemaps) < max_sitemaps:
                sitemap = normalize_url(sitemaps.pop(0))
                if not sitemap or not within_seed(sitemap, seed) or sitemap in visited_sitemaps:
                    continue
                visited_sitemaps.add(sitemap)
                doc = fetch(sitemap)
                counters['web_requests'] += 1
                if doc.status != 200:
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
    ordered = sorted(found.values(), key=lambda c: (-c.priority_score, c.url))
    return ordered[:max_candidates], counters


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
                     max_pending: int) -> tuple[int, int, int]:
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
        fetcher = SafeFetcher(timeout=args.timeout, max_bytes=args.max_response_bytes,
                              min_host_interval=args.min_host_interval)
        candidates: dict[str, Candidate] = {}
        stats = {'web_requests': 0, 'web_errors': 0, 'ct_errors': 0}
        for seed in current_seeds:
            def fetch(url: str, *, _seed=seed):
                return fetcher.get(url, seed=_seed)
            items, counters = discover_seed(
                seed, fetch=fetch if args.scan_web else None,
                include_ct=args.ct,
                ct_lookup=(lambda name: ct_subdomains(name, timeout=args.timeout)) if args.ct else None,
                max_candidates=args.max_candidates_per_seed,
                max_sitemaps=args.max_sitemaps)
            for k, v in counters.items():
                stats[k] += v
            for item in items:
                old = candidates.get(item.url)
                if old is None or item.priority_score > old.priority_score:
                    candidates[item.url] = item
            if args.request_interval > 0 and (args.scan_web or args.ct):
                time.sleep(args.request_interval)
        ordered = sorted(candidates.values(), key=lambda c: (-c.priority_score, c.url))
        new = 0
        after = before
        held = 0
        if args.apply:
            assert conn is not None
            new, after, held = apply_candidates(conn, candidates=ordered, input_digest=digest,
                                          next_cursor=next_cursor, max_new=args.max_new,
                                          max_pending=args.max_pending)
        return {
            'mode': 'apply' if args.apply else 'preview',
            'seeds_total': len(seeds), 'seeds_this_run': len(current_seeds),
            'invalid_seed_entries': invalid, 'start_index': start,
            'next_index': next_cursor, 'candidate_urls': len(ordered),
            'new_jobs': new, 'held_candidates': held,
            'pending_before': before, 'pending_after': after,
            'scan_web': args.scan_web, 'ct': args.ct, **stats,
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
    parser.add_argument('--ct', action='store_true', help='Optional crt.sh public CT lookup (sends seed hostname)')
    parser.add_argument('--apply', action='store_true', help='ENQUEUE jobs into crawler DB; may trigger BIMA alerts')
    parser.add_argument('--seed-limit', type=int, default=20)
    parser.add_argument('--start-index', type=int, default=None, help='Preview offset override')
    parser.add_argument('--max-candidates-per-seed', type=int, default=30)
    parser.add_argument('--max-new', type=int, default=30)
    parser.add_argument('--max-pending', type=int, default=1000)
    parser.add_argument('--max-sitemaps', type=int, default=2)
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
