#!/usr/bin/env python3
"""BIMA Investigation Engine v1: bounded, on-demand, HTTP-only evidence.

No browser, ML, model loading, Telegram, alert transmission, or DB writes.
Only public DNS destinations; DNS resolution is pinned for each TCP connection.
HTML/content are never executed or persisted. This is not an abuse verdict.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import stat
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_HTML_BYTES = 512 * 1024
MAX_LINKS = 500
MAX_HOPS = 4
MAX_TIMEOUT = 8
USER_AGENT = 'BIMA-Investigation/1.0 (read-only abuse review; no browser)'
_STRONG_ANCHORS = (
    'slot gacor', 'slot online', 'rtp slot', 'judi online', 'situs judi',
    'togel online', 'bandar togel', 'agen togel', 'casino online',
    'link alternatif judi', 'situs slot', 'daftar judi',
)
_PROMO_WORDS = ('login', 'daftar', 'bonus')


class InvestigationError(Exception):
    """Expected, reportable failure. Do not treat as a safe site."""


def _clean_host(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise InvestigationError('invalid_hostname')
    try:
        ascii_host = host.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError as exc:
        raise InvestigationError('invalid_hostname') from exc
    if (not ascii_host or '..' in ascii_host or any(
            not re.fullmatch(r'[a-z0-9-]{1,63}', part)
            or part.startswith('-') or part.endswith('-')
            for part in ascii_host.split('.'))):
        # Numeric IP literals are valid only as redirects and are handled separately.
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError as exc:
            raise InvestigationError('invalid_hostname') from exc
    return ascii_host


def _validate_url(raw, *, initial):
    if not isinstance(raw, str) or not raw or len(raw) > 2048 or any(
            ord(ch) < 33 or ord(ch) == 127 for ch in raw):
        raise InvestigationError('invalid_url')
    if '://' not in raw:
        raw = 'https://' + raw
    if '\\' in raw:
        raise InvestigationError('invalid_url')
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in ('http', 'https') or not parts.netloc or '@' in parts.netloc:
            raise InvestigationError('blocked_url_scheme_or_auth')
        host = _clean_host(parts.hostname)
        port = parts.port or (443 if scheme == 'https' else 80)
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvestigationError('invalid_url') from exc
    if port != (443 if scheme == 'https' else 80):
        raise InvestigationError('blocked_nonstandard_port')
    if initial and not (host.endswith('.id') and len(host) > 3):
        raise InvestigationError('initial_target_must_be_id_domain')
    netloc = host if ':' not in host else '[' + host + ']'
    path = parts.path or '/'
    # URL fragments are never sent. Queries may be sent, but never persisted.
    return urlunsplit((scheme, netloc, path, parts.query, ''))


def _public_ip_for(host, port):
    try:
        entries = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise InvestigationError('dns_resolution_failed') from exc
    ips = sorted({item[4][0] for item in entries})
    if not ips:
        raise InvestigationError('dns_no_addresses')
    # Fail closed if any address is private/local/reserved; prevent DNS rebinding
    # by connecting only to one address from this validated resolution result.
    try:
        if any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise InvestigationError('blocked_nonpublic_dns_address')
    except ValueError as exc:
        raise InvestigationError('invalid_dns_address') from exc
    ips.sort(key=lambda ip: (':' in ip, ip))  # Prefer IPv4 if available.
    return ips[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, *, pinned_ip, **kwargs):
        self.pinned_ip = pinned_ip
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, *, pinned_ip, **kwargs):
        self.pinned_ip = pinned_ip
        super().__init__(host, context=ssl.create_default_context(), **kwargs)

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _request_one(url, timeout):
    parts = urlsplit(url)
    host = _clean_host(parts.hostname)
    port = 443 if parts.scheme == 'https' else 80
    ip = _public_ip_for(host, port)
    connection_type = _PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP
    conn = connection_type(host, pinned_ip=ip, port=port, timeout=timeout)
    try:
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        conn.request('GET', path, headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml;q=0.9',
            'Accept-Encoding': 'identity',
            'Connection': 'close',
        })
        response = conn.getresponse()
        status = response.status
        location = response.getheader('Location')
        content_type = response.getheader('Content-Type', '') or ''
        encoding = response.getheader('Content-Encoding', 'identity').lower().strip()
        if status in (301, 302, 303, 307, 308):
            return {'status': status, 'location': location, 'content_type': content_type,
                    'body': b''}
        if not (200 <= status < 300):
            return {'status': status, 'location': None, 'content_type': content_type,
                    'body': b''}
        if not ('text/html' in content_type.lower() or
                'application/xhtml+xml' in content_type.lower()):
            raise InvestigationError('non_html_content')
        if encoding not in ('', 'identity'):
            raise InvestigationError('compressed_content_unsupported')
        body = response.read(MAX_HTML_BYTES + 1)
        if len(body) > MAX_HTML_BYTES:
            raise InvestigationError('html_body_limit_exceeded')
        return {'status': status, 'location': None, 'content_type': content_type,
                'body': body}
    except (TimeoutError, ConnectionError, socket.timeout, OSError,
            http.client.HTTPException, ssl.SSLError) as exc:
        raise InvestigationError('network_or_tls_failure') from exc
    finally:
        conn.close()


def _sanitized_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def fetch_chain(target, *, timeout=5, max_hops=MAX_HOPS, fetch_one=_request_one):
    current = _validate_url(target, initial=True)
    chain = []
    visited = set()
    for _ in range(max_hops + 1):
        current = _validate_url(current, initial=False)
        if current in visited:
            raise InvestigationError('redirect_loop')
        visited.add(current)
        chain.append(_sanitized_url(current))
        item = fetch_one(current, timeout)
        if item['status'] in (301, 302, 303, 307, 308):
            location = item.get('location')
            if not location or len(location) > 2048:
                raise InvestigationError('redirect_missing_or_long_location')
            current = urljoin(current, location)
            continue
        return {'final_url': current, 'chain': chain, **item}
    raise InvestigationError('redirect_hop_limit_exceeded')


def _hidden_reason(attrs):
    values = dict(attrs)
    style = re.sub(r'\s+', '', (values.get('style') or '').lower())
    if values.get('hidden') is not None or 'hidden' in values:
        return 'hidden_attribute'
    if str(values.get('aria-hidden') or '').lower() == 'true':
        return 'aria-hidden'
    for marker in ('display:none', 'visibility:hidden', 'opacity:0'):
        if marker in style:
            return 'style:' + marker
    return ''


class _EvidenceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # (tag, effective hidden reason)
        self.active_link = None
        self.links = []
        self.link_cap_reached = False
        self.title = ''
        self.visible_text = ''

    def handle_starttag(self, tag, attrs):
        parent_hidden = self.stack[-1][1] if self.stack else ''
        hidden = parent_hidden or _hidden_reason(attrs)
        self.stack.append((tag, hidden))
        if tag == 'a':
            if self.active_link:
                self._close_link()
            href = dict(attrs).get('href', '') or ''
            self.active_link = {'href': href, 'text': '', 'hidden_method': hidden,
                                'line': self.getpos()[0]}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.active_link:
            self.active_link['text'] = (self.active_link['text'] + data)[:256]
        hidden = self.stack[-1][1] if self.stack else ''
        if not hidden and self.stack:
            tag_names = {tag for tag, _ in self.stack}
            if 'title' in tag_names:
                self.title = (self.title + data)[:240]
            if not tag_names.intersection(('script', 'style', 'noscript')):
                self.visible_text = (self.visible_text + ' ' + data)[:16000]

    def _close_link(self):
        if self.active_link:
            if len(self.links) < MAX_LINKS:
                self.links.append(self.active_link)
            else:
                self.link_cap_reached = True
        self.active_link = None

    def handle_endtag(self, tag):
        if tag == 'a':
            self._close_link()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def close(self):
        super().close()
        self._close_link()


def _word_has_gambling_signal(text):
    norm = ' '.join(re.findall(r'[a-z0-9]+', (text or '').lower()))
    return [term for term in _STRONG_ANCHORS if term in norm]


def analyse_html(body, final_url):
    parser = _EvidenceHTML()
    parser.feed(body.decode('utf-8', errors='replace'))
    parser.close()
    page_host = _clean_host(urlsplit(final_url).hostname)
    hidden_links = []
    promo_links = []
    for link in parser.links:
        href = link['href'].strip()
        if not href or len(href) > 2048:
            continue
        target = urlsplit(urljoin(final_url, href))
        if target.scheme not in ('http', 'https') or not target.hostname:
            continue
        try:
            destination = _clean_host(target.hostname)
        except InvestigationError:
            continue
        external = destination != page_host and not destination.endswith('.' + page_host)
        anchor_text = ' '.join(link['text'].split())
        strong_terms = _word_has_gambling_signal(anchor_text)
        if external and link['hidden_method'] and strong_terms:
            hidden_links.append({
                'anchor_text': anchor_text[:80],
                'signals': strong_terms[:5],
                'destination_host': destination,
                'hidden_method': link['hidden_method'],
                'html_line': link['line'],
            })
        if external and not link['hidden_method'] and any(
                t in anchor_text.lower() for t in _PROMO_WORDS):
            promo_links.append({'anchor_text': anchor_text[:80],
                                'destination_host': destination})
    name_text = (parser.title + ' ' + parser.visible_text[:2500]).lower()
    has_gambling_brand = any(token in name_text for token in
                             ('togel', 'judi online', 'slot gacor', 'casino online'))
    if hidden_links:
        category = 'hidden_gambling_seo_spam'
        verdict = 'strong_indicator_review_required'
        reason = 'hidden_external_links_with_gambling_promotion_terms'
    elif has_gambling_brand and len(promo_links) >= 2:
        category = 'suspected_gambling_promotion'
        verdict = 'strong_indicator_review_required'
        reason = 'visible_gambling_brand_with_outbound_registration_or_bonus_links'
    else:
        category = 'not_determined'
        verdict = 'insufficient_evidence'
        reason = 'no_sufficient_indicator_in_bounded_html'
    return {
        'page_title': ' '.join(parser.title.split())[:240],
        'html_sha256': hashlib.sha256(body).hexdigest(),
        'html_bytes': len(body),
        'hidden_gambling_links_count': len(hidden_links),
        'hidden_gambling_links_sample': hidden_links[:15],
        'outbound_promotion_links_count': len(promo_links),
        'outbound_promotion_links_sample': promo_links[:8],
        'links_examined': len(parser.links),
        'links_capped': parser.link_cap_reached,
        'category': category,
        'assessment': verdict,
        'reason': reason,
    }


def read_job(db_file, job_id):
    if not db_file or job_id is None:
        return None
    source = Path(db_file).resolve()
    if not source.is_file():
        raise InvestigationError('db_not_found')
    conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute('SELECT id,url,final_url,status,risk,reason,route_reason,'
                           'classifier_json,updated_at FROM jobs WHERE id=?',
                           (job_id,)).fetchone()
    except sqlite3.Error as exc:
        raise InvestigationError('db_read_failed') from exc
    finally:
        conn.close()
    if not row:
        raise InvestigationError('job_not_found')
    jid, url, final_url, status, risk, reason, route, classifier_raw, updated_at = row
    try:
        classifier = json.loads(classifier_raw or '{}')
        if not isinstance(classifier, dict):
            classifier = {}
    except (TypeError, ValueError):
        classifier = {}
    ml_pred = classifier.get('ml_pred')
    bert_pred = classifier.get('indobert_pred')
    mismatch = (classifier.get('bert_used') is True
                and ml_pred == 'gambling' and bert_pred == 'non-gambling')
    def report_url(value):
        try:
            return _sanitized_url(value) if value else None
        except ValueError:
            return None
    return {
        'job_id': jid, 'initial_url': report_url(url), 'final_url': report_url(final_url),
        'status': status, 'risk': risk, 'reason': reason, 'route_reason': route,
        'status_updated_at': updated_at, 'model_disagreement': mismatch,
        'ml_pred': ml_pred, 'ml_source': classifier.get('ml_source'),
        'ml_gambling_score': classifier.get('ml_conf'),
        'indobert_pred': bert_pred,
        'indobert_gambling_score': classifier.get('indobert_conf'),
        'classification_reason': classifier.get('reason'),
        'strong_hits': classifier.get('strong_hits', [])[:12]
        if isinstance(classifier.get('strong_hits'), list) else [],
    }


@contextlib.contextmanager
def single_investigation(lock_path):
    path = Path(lock_path)
    if not path.parent.is_dir() or path.is_symlink():
        raise InvestigationError('lock_directory_missing_or_unsafe')
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InvestigationError('lock_open_failed') from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InvestigationError('invalid_lock_file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InvestigationError('investigation_already_running') from exc
        yield
    finally:
        os.close(fd)


def investigate(target, *, timeout=5, max_hops=MAX_HOPS, db=None, job_id=None,
                fetch_one=_request_one):
    normalized = _validate_url(target, initial=True)
    job = read_job(db, job_id) if job_id is not None else None
    if job:
        saved = job.get('initial_url') or ''
        if _clean_host(urlsplit(saved).hostname) != _clean_host(urlsplit(normalized).hostname):
            raise InvestigationError('job_initial_host_mismatch')
    report = {
        'schema_version': 1,
        'tool': 'BIMA Investigation Engine v1',
        'mode': 'on_demand_http_only_read_only',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'target': _sanitized_url(normalized),
        'saved_job': job,
        'observation': None,
        'limitations': [
            'One bounded current HTTP observation; no history or ownership proof.',
            'No JavaScript, Selenium, download, external link following or inference.',
            'Hidden SEO spam can suggest compromise, not establish who injected it.',
            'An absent indicator does not prove a domain safe.',
            'No new alert, block, or registrar action is performed.',
        ],
    }
    try:
        retrieved = fetch_chain(normalized, timeout=timeout, max_hops=max_hops,
                                fetch_one=fetch_one)
        obs = {'http_status': retrieved['status'],
               'final_url': _sanitized_url(retrieved['final_url']),
               'redirect_chain': retrieved['chain']}
        if retrieved['body']:
            obs.update(analyse_html(retrieved['body'], retrieved['final_url']))
        else:
            obs.update({'category': 'not_determined',
                        'assessment': 'insufficient_evidence',
                        'reason': 'no_html_body_available'})
        report['observation'] = obs
    except InvestigationError as exc:
        report['observation'] = {'category': 'not_determined',
                                 'assessment': 'inconclusive', 'reason': str(exc)}
    return report


def save_private(path, report):
    filename = Path(path)
    if not filename.parent.is_dir() or filename.is_symlink():
        raise InvestigationError('output_directory_missing_or_unsafe')
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(filename, flags, 0o600)
    except OSError as exc:
        raise InvestigationError('output_open_failed') from exc
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='Public .id domain or URL')
    parser.add_argument('--db', help='Existing BIMA jobs SQLite, read-only')
    parser.add_argument('--job-id', type=int, help='Exact job ID for optional correlation')
    parser.add_argument('--timeout', type=int, default=5, choices=range(1, MAX_TIMEOUT + 1))
    parser.add_argument('--max-hops', type=int, default=MAX_HOPS, choices=range(MAX_HOPS + 1))
    parser.add_argument('--out', help='Create private JSON (0600); never overwrite')
    parser.add_argument('--lock-file', default=str(Path(__file__).resolve().parent /
                                                'state' / '.investigation-v1.lock'))
    args = parser.parse_args(argv)
    if (args.job_id is None) != (args.db is None):
        parser.error('--db and --job-id must be supplied together')
    try:
        with single_investigation(args.lock_file):
            report = investigate(args.target, timeout=args.timeout,
                                 max_hops=args.max_hops, db=args.db,
                                 job_id=args.job_id)
            if args.out:
                save_private(args.out, report)
                print(json.dumps({'saved': args.out, 'assessment':
                                  report['observation']['assessment'],
                                  'reason': report['observation']['reason']}))
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
    except (InvestigationError, ValueError) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
