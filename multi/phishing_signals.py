#!/usr/bin/env python3
"""Bounded, passive HTML phishing triage for .id seed pages.

No fetching, browser activity, ML verdict, alert, or raw credential storage.
Rules identify cases worth human review, never confirmed phishing.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

VERSION = 'phishing_shadow_v1'
MAX_HTML_CHARS = 256_000
MAX_FORMS = 64


def _host(url):
    try:
        return (urlsplit(url or '').hostname or '').lower().rstrip('.')
    except (ValueError, AttributeError, TypeError):
        return ''


def _is_id(host):
    return bool(host and host.endswith('.id') and host != '.id')


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.current = None
        self.base_href = None

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): (v or '') for k, v in attrs if k}
        if tag == 'base' and self.base_href is None:
            self.base_href = attrs.get('href') or None
        elif tag == 'form':
            if self.current is not None:
                self._close_form()
            self.current = {'action': attrs.get('action', ''),
                            'alternate_actions': [],
                            'has_password': False, 'has_otp': False,
                            'has_identity': False}
        elif tag in ('button', 'input') and self.current is not None:
            if attrs.get('formaction'):
                self.current['alternate_actions'].append(attrs['formaction'])
            if tag != 'input':
                return
            kind = attrs.get('type', 'text').lower()
            name = (attrs.get('name', '') + ' ' + attrs.get('autocomplete', '')).lower()
            self.current['has_password'] |= kind == 'password'
            self.current['has_otp'] |= ('one-time-code' in name or
                                         ('otp' in name and kind != 'hidden'))
            self.current['has_identity'] |= (kind == 'email' or
                         any(w in name for w in ('username', 'user_name', 'login', 'email')))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == 'form':
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'form':
            self._close_form()

    def _close_form(self):
        if self.current is not None and len(self.forms) < MAX_FORMS:
            self.forms.append(self.current)
        self.current = None

    def finish(self):
        self._close_form()


def inspect_html(html, page_url, initial_url=None):
    """Return safe metadata only, never URLs, DOM contents or field values."""
    initial_host = _host(initial_url or page_url)
    result = {'version': VERSION, 'status': 'insufficient_signal',
              'signal_codes': [], 'forms': 0, 'password_forms': 0,
              'truncated': False, 'html_prefix_sha256': None}
    if not _is_id(initial_host):
        result['status'] = 'out_of_scope'
        return result
    if not isinstance(html, str) or not html:
        result['status'] = 'no_html'
        return result
    limited = html[:MAX_HTML_CHARS]
    result['truncated'] = len(html) > MAX_HTML_CHARS
    result['html_prefix_sha256'] = hashlib.sha256(limited.encode('utf-8')).hexdigest()
    parser = _FormParser()
    try:
        parser.feed(limited)
        parser.finish()
    except (ValueError, TypeError, AssertionError):
        result['status'] = 'parse_error'
        return result

    page_host = _host(page_url)
    base = page_url
    if parser.base_href:
        maybe_base = urljoin(page_url, parser.base_href)
        if urlsplit(maybe_base).scheme in ('http', 'https'):
            base = maybe_base

    signals = set()
    for form in parser.forms:
        if not form['has_password']:
            continue
        result['password_forms'] += 1
        if form['has_identity']:
            signals.add('login_fields_present')
        if form['has_otp']:
            signals.add('otp_field_present')
        # A submitter's formaction can override the form action. Never execute it.
        for action in [form['action'], *form['alternate_actions']]:
            # Empty action submits to the current document, regardless of <base>.
            destination = urljoin(base, action) if action else page_url
            try:
                parts = urlsplit(destination)
                target_host = (parts.hostname or '').lower().rstrip('.')
            except (ValueError, TypeError):
                parts, target_host = None, ''
            if not parts or parts.scheme not in ('http', 'https') or not target_host:
                signals.add('non_http_credential_action')
            elif parts.scheme == 'http':
                signals.add('unencrypted_credential_action')
            if target_host and page_host and target_host != page_host:
                signals.add('cross_origin_credential_action')
    result['forms'] = len(parser.forms)
    result['signal_codes'] = sorted(signals)
    strong = {'cross_origin_credential_action', 'unencrypted_credential_action',
              'non_http_credential_action'}
    if strong.intersection(signals):
        result['status'] = 'review_recommended'
    elif result['password_forms']:
        result['status'] = 'login_form_only'
    return result


def enrich_classification(result, *, initial_url, current_url, page_html, environ=None):
    """Opt-in, deterministic sampled enrichment; never changes legacy verdict."""
    env = environ if environ is not None else os.environ
    if env.get('BIMA_PHISHING_SHADOW') != '1':
        return result
    try:
        rate = float(env.get('BIMA_PHISHING_SHADOW_RATE', '0.05'))
        if not (0.0 <= rate <= 1.0):
            raise ValueError('rate outside [0,1]')
    except (ValueError, TypeError):
        return result
    key = initial_url if isinstance(initial_url, str) else ''
    fraction = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') / (1 << 64)
    if fraction >= rate:
        return result
    try:
        html = page_html() if callable(page_html) else page_html
        triage = inspect_html(html, current_url, initial_url)
        # v12 is a separately opt-in, metadata-only extension. Failure must
        # never erase the v1 observation or alter the gambling decision.
        if env.get('BIMA_PHISHING_V12') == '1':
            try:
                from phishing_signals_v12 import extend_triage
                triage = extend_triage(triage, html, current_url, initial_url)
            except Exception:
                triage = dict(triage)
                triage['v12_error'] = 'scanner_error'
    except Exception:
        triage = {'version': VERSION, 'status': 'scanner_error', 'signal_codes': []}
    output = dict(result)
    output['phishing_triage'] = triage
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline phishing HTML triage; no network calls')
    parser.add_argument('--html-file', type=Path, required=True)
    parser.add_argument('--url', required=True, help='URL halaman yang diamati')
    parser.add_argument('--initial-url', default=None, help='URL .id awal sebelum redirect')
    parser.add_argument('--v12', action='store_true',
                        help='Offline opt-in: inspect extended v12 credential-form indicators')
    args = parser.parse_args(argv)
    with args.html_file.open('r', encoding='utf-8', errors='replace') as f:
        html = f.read(MAX_HTML_CHARS + 1)
    triage = inspect_html(html, args.url, args.initial_url)
    if args.v12:
        from phishing_signals_v12 import extend_triage
        triage = extend_triage(triage, html, args.url, args.initial_url)
    print(json.dumps(triage, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
