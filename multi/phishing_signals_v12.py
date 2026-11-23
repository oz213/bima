"""BIMA v12 passive credential-form signals (observation only).

No DNS/network, JavaScript, browser, model, alert, or user-content output.
Caller supplies only HTML already fetched by the existing crawler. These signals
identify cases for human review, never a confirmed phishing verdict.
"""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
from urllib.parse import urljoin, urlsplit

MAX_HTML_CHARS = 256_000
MAX_FORMS = 64
MAX_ACTIONS_PER_FORM = 8
VERSION = 'phishing_shadow_v12'


class _CredentialFormParser(HTMLParser):
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
            self._finish_form()
            if len(self.forms) < MAX_FORMS:
                self.current = {
                    'action': attrs.get('action', ''),
                    'method': attrs.get('method', '').lower().strip(),
                    'alternate': [], 'password': False, 'otp': False,
                }
        elif self.current is not None and tag in ('input', 'button'):
            if attrs.get('formaction') and len(self.current['alternate']) < MAX_ACTIONS_PER_FORM - 1:
                self.current['alternate'].append(attrs['formaction'])
            if tag == 'input':
                field_type = attrs.get('type', 'text').lower()
                hint = (attrs.get('name', '') + ' ' + attrs.get('autocomplete', '') + ' ' +
                        attrs.get('id', '')).lower()
                self.current['password'] |= field_type == 'password'
                if field_type != 'hidden':
                    self.current['otp'] |= any(x in hint for x in
                                               ('otp', 'one-time-code', 'one_time_code',
                                                'verification_code', 'kode_verifikasi'))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == 'form':
            self._finish_form()

    def handle_endtag(self, tag):
        if tag == 'form':
            self._finish_form()

    def _finish_form(self):
        if self.current is not None and len(self.forms) < MAX_FORMS:
            self.forms.append(self.current)
        self.current = None

    def finish(self):
        self._finish_form()


def _host(value):
    try:
        return (urlsplit(value).hostname or '').lower().rstrip('.')
    except (TypeError, ValueError, AttributeError):
        return ''


def extend_triage(legacy, html, page_url, initial_url=None):
    """Append bounded signals to the existing phishing shadow observation.

    Keeps existing legacy classifier decision untouched; never return HTML,
    URLs, form values, destination hostnames, or credentials.
    """
    if not isinstance(legacy, dict):
        raise ValueError('Invalid legacy triage')
    if legacy.get('status') in ('out_of_scope', 'no_html', 'parse_error', 'scanner_error'):
        return legacy
    if not isinstance(html, str) or not html:
        return legacy
    initial_host = _host(initial_url or page_url)
    if not initial_host.endswith('.id') or initial_host == '.id':
        return legacy
    page_host = _host(page_url)
    if not page_host:
        return legacy

    parser = _CredentialFormParser()
    parser.feed(html[:MAX_HTML_CHARS])
    parser.finish()

    base = page_url
    if parser.base_href:
        maybe = urljoin(page_url, parser.base_href)
        if urlsplit(maybe).scheme in ('http', 'https'):
            base = maybe

    signals = set()
    credential_forms = 0
    otp_only_forms = 0
    for form in parser.forms:
        if not form['password'] and not form['otp']:
            continue
        credential_forms += 1
        if form['otp'] and not form['password']:
            otp_only_forms += 1
        if form['password'] and form['method'] == 'get':
            signals.add('credential_explicit_get_method')
        if urlsplit(page_url).scheme == 'http':
            signals.add('credential_page_http')
        for action in [form['action'], *form['alternate']]:
            # Empty action means document URL, *not* a <base> target.
            destination = urljoin(base, action) if action else page_url
            try:
                parts = urlsplit(destination)
                host = (parts.hostname or '').lower().rstrip('.')
                port = parts.port  # Reject malformed ports without exposing them.
            except (ValueError, TypeError):
                signals.add('credential_action_malformed')
                continue
            if parts.scheme not in ('https', 'http') or not host:
                if form['otp'] and not form['password']:
                    signals.add('otp_non_http_action')
                continue
            if parts.username is not None or parts.password is not None:
                signals.add('credential_action_userinfo')
            if port is not None and port not in (80, 443):
                signals.add('credential_action_nonstandard_port')
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                signals.add('credential_action_ip_literal')
            if form['otp'] and not form['password']:
                if page_host != host:
                    signals.add('otp_cross_origin_action')
                if parts.scheme == 'http':
                    signals.add('otp_unencrypted_action')

    result = dict(legacy)
    result['version'] = VERSION
    result['credential_forms_v12'] = credential_forms
    result['otp_only_forms_v12'] = otp_only_forms
    result['v12_signal_codes'] = sorted(signals)
    result['signal_codes'] = sorted(set(legacy.get('signal_codes') or []).union(signals))
    # All statuses here are REVIEW TRIAGE; never a phishing verdict.
    actionable = {'credential_explicit_get_method', 'credential_page_http',
                  'credential_action_malformed', 'credential_action_userinfo',
                  'credential_action_nonstandard_port', 'credential_action_ip_literal',
                  'otp_cross_origin_action', 'otp_non_http_action',
                  'otp_unencrypted_action'}
    if signals & actionable:
        result['status'] = 'review_recommended'
    elif otp_only_forms and legacy.get('status') == 'insufficient_signal':
        result['status'] = 'otp_form_only'
    result['review_only'] = True
    return result
