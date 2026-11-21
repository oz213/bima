"""Bounded, passive JavaScript navigation signals for BIMA .id Discovery.

NO script evaluation, browser/network calls, JS module downloads, or abuse verdicts.
Targets are transient until proactive_discovery validates and redacts them.
Unknown expressions are review hints, not redirect observations.
"""
from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import unquote
import warnings

MAX_HTML = 200_000
MAX_SCRIPT = 60_000
MAX_SIGNALS = 16
MAX_EXPRESSION = 4096
MAX_TARGET = 2048


@dataclass(frozen=True)
class JsSignal:
    kind: str
    target: str | None = None


# This is deliberately a recognizer for navigation *syntax*, not a JS engine.
# Script text inside quotes/comments must never be interpreted as navigation.
_NAV = re.compile(
    r"""(?<![\w$./])(?:window\s*\.\s*|document\s*\.\s*|top\s*\.\s*|self\s*\.\s*)?location(?:\s*\.\s*href|\s*\[\s*(?:'href'|"href")\s*\])?\s*=(?![=>])\s*|(?<![\w$./])(?:(?:window|document|top|self)\s*\.\s*)?location\s*\.\s*(?:assign|replace)\s*\(|(?<![\w$./])window\s*\.\s*open\s*\(""",
    re.I,
)
_CONST = re.compile(r"(?<![\w$])const\s+([A-Za-z_$][\w$]*)\s*=\s*")
_SIMPLE_NAME = re.compile(r"[A-Za-z_$][\w$]*\Z")
_STRING = re.compile(r"""(?s)(['"`])(?:\\.|(?!\1).)*\1\Z""")
_CALL = re.compile(r"(?is)^([A-Za-z_$][\w$.]*)\s*\((.*)\)$")
# Python versions differ on whether unknown escapes (for example \q) raise
# or merely warn. Accept only the conservative JS escapes we can interpret.
_JS_ESCAPE = re.compile(r"""\\(?:[\\'"/bfnrtv0]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})""")


def mask_noncode(js: str) -> str:
    """Preserve code offsets; erase quoted strings and // /* */ comments.

    Also masks JS backtick templates; we never execute/interpolate them.
    """
    result = list(js)
    i = 0
    n = len(js)
    while i < n:
        char = js[i]
        if char in ('\"', "'", '`'):
            quote = char
            result[i] = ' '
            i += 1
            while i < n:
                ch = js[i]
                result[i] = '\n' if ch == '\n' else ' '
                if ch == '\\':
                    i += 1
                    if i < n:
                        result[i] = '\n' if js[i] == '\n' else ' '
                elif ch == quote:
                    i += 1
                    break
                i += 1
            continue
        if js.startswith('//', i):
            while i < n and js[i] != '\n':
                result[i] = ' '
                i += 1
            continue
        if js.startswith('/*', i):
            result[i] = result[i + 1] = ' '
            i += 2
            while i < n and not js.startswith('*/', i):
                result[i] = '\n' if js[i] == '\n' else ' '
                i += 1
            if i < n:
                result[i] = result[i + 1] = ' '
                i += 2
            continue
        i += 1
    return ''.join(result)


def take_expression(js: str, start: int, *, call: bool) -> tuple[str, int, str]:
    """Return bounded first argument/assignment RHS; no evaluation."""
    end = min(len(js), start + MAX_EXPRESSION)
    i, nested = start, 0
    quote: str | None = None
    while i < end:
        ch = js[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('\"', "'", '`'):
            quote = ch
        elif ch in '([{':
            nested += 1
        elif ch in ')]}':
            if nested:
                nested -= 1
            elif (call and ch == ')') or (not call and ch == '}'):
                return js[start:i].strip(), i, ch
        elif nested == 0:
            if call and ch == ',':
                return js[start:i].strip(), i, ','
            if not call and ch in ';\r\n':
                return js[start:i].strip(), i, ch
        i += 1
    if end < len(js):
        return '', end, 'overflow'
    return js[start:i].strip(), i, ''


def split_top_level(value: str, delim: str) -> list[str]:
    parts = []
    start = 0
    i = 0
    nested = 0
    quote = None
    while i < len(value):
        ch = value[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('\"', "'", '`'):
            quote = ch
        elif ch in '([{':
            nested += 1
        elif ch in ')]}':
            nested -= 1
            if nested < 0:
                return []
        elif ch == delim and nested == 0:
            parts.append(value[start:i].strip())
            start = i + 1
        i += 1
    if quote or nested != 0:
        return []
    parts.append(value[start:].strip())
    return parts


def literal_string(value: str) -> str | None:
    if len(value) > MAX_TARGET + 80 or not _STRING.fullmatch(value):
        return None
    if value.startswith('`'):
        if '${' in value or '\\' in value:
            return None
        return value[1:-1]
    # Do not ask ast.literal_eval to decide whether a JS escape is valid:
    # Python 3.11 accepts unknown escapes with only a DeprecationWarning.
    # Decode \/ only when it is an actual escape (not the second half of
    # an escaped backslash), preserving JavaScript string semantics.
    normalized = []
    i = 1
    while i < len(value) - 1:
        if value[i] != '\\':
            normalized.append(value[i])
            i += 1
            continue
        match = _JS_ESCAPE.match(value, i)
        if match is None or match.end() >= len(value):
            return None
        escape = match.group(0)
        if escape == r'\0' and match.end() < len(value) - 1 and value[match.end()].isdigit():
            return None  # Avoid ambiguous legacy octal escapes.
        normalized.append('/' if escape == r'\/' else escape)
        i = match.end()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', (SyntaxWarning, DeprecationWarning))
            result = ast.literal_eval(value[0] + ''.join(normalized) + value[-1])
        return result if isinstance(result, str) else None
    except (SyntaxError, SyntaxWarning, ValueError, TypeError, MemoryError):
        return None


def resolve_static(value: str, bindings: dict[str, str] | None = None,
                   depth: int = 0) -> tuple[str | None, str]:
    """Resolve only unambiguous, bounded constant expressions.

    Does NOT resolve JS expressions that require runtime state/functions.
    """
    value = value.strip()
    if depth >= 4 or not value or len(value) > MAX_EXPRESSION:
        return None, 'dynamic'
    result = literal_string(value)
    if result is not None:
        return (result, 'literal') if len(result) <= MAX_TARGET else (None, 'dynamic')
    if _SIMPLE_NAME.fullmatch(value) and bindings is not None and value in bindings:
        return bindings[value], 'constant'
    parts = split_top_level(value, '+')
    if 1 < len(parts) <= 8:
        chunks = []
        for part in parts:
            piece, _ = resolve_static(part, bindings, depth + 1)
            if piece is None:
                return None, 'dynamic'
            chunks.append(piece)
        result = ''.join(chunks)
        return (result, 'concat') if len(result) <= MAX_TARGET else (None, 'dynamic')
    call = _CALL.fullmatch(value)
    if not call:
        return None, 'dynamic'
    name, raw = call.groups()
    if name in ('atob', 'decodeURIComponent', 'decodeURI', 'unescape'):
        argument, _ = resolve_static(raw, bindings, depth + 1)
        if argument is None or len(argument) > MAX_TARGET:
            return None, 'dynamic'
        try:
            if name == 'atob':
                if len(argument) > 3000 or not re.fullmatch(r'[A-Za-z0-9+/]*={0,2}', argument):
                    return None, 'dynamic'
                # atob accepts some unpadded encodings; pad only after a
                # strict alphabet/size check. No dynamic JS is evaluated.
                padded = argument + '=' * ((4 - len(argument) % 4) % 4)
                data = base64.b64decode(padded, validate=True)
                result = data.decode('utf-8', 'strict')
            else:
                result = unquote(argument, encoding='utf-8', errors='strict')
        except (ValueError, UnicodeError, base64.binascii.Error):
            return None, 'dynamic'
        return (result, 'decoded') if len(result) <= MAX_TARGET else (None, 'dynamic')
    if name in ('String.fromCharCode', 'String.fromCodePoint'):
        numbers = split_top_level(raw, ',')
        if not 1 <= len(numbers) <= 512:
            return None, 'dynamic'
        try:
            decoded = []
            maximum = 65535 if name == 'String.fromCharCode' else 0x10ffff
            for num in numbers:
                if not re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|\d{1,7})', num):
                    return None, 'dynamic'
                digit = int(num, 0) if num.lower().startswith('0x') else int(num, 10)
                if not 0 <= digit <= maximum or 0xd800 <= digit <= 0xdfff:
                    return None, 'dynamic'
                decoded.append(chr(digit))
            result = ''.join(decoded)
            return result, 'decoded'
        except (ValueError, OverflowError):
            return None, 'dynamic'
    return None, 'dynamic'


def scan_js(js: str, *, max_signals: int = MAX_SIGNALS) -> list[JsSignal]:
    """Scan one inline script. Static signals != observed browser navigation."""
    js = js[:MAX_SCRIPT]
    mask = mask_noncode(js)
    signals: list[JsSignal] = []
    bindings: dict[str, str] = {}
    tokens = [(m.start(), 'const', m) for m in _CONST.finditer(js)]
    tokens.extend((m.start(), 'nav', m) for m in _NAV.finditer(js))
    tokens.sort(key=lambda item: item[0])
    for start, kind, match in tokens:
        if not mask[start:start + 4].strip():
            continue
        if kind == 'const':
            if len(bindings) >= 24:
                continue
            expr, _, _ = take_expression(js, match.end(), call=False)
            value, _ = resolve_static(expr, bindings)
            if value is not None:
                bindings[match.group(1)] = value
            continue
        call = match.group(0).rstrip().endswith('(')
        expr, end, delim = take_expression(js, match.end(), call=call)
        if not expr or delim == 'overflow':
            continue
        if 'open' in match.group(0).lower() and call:
            if delim != ',':
                continue
            second, _, _ = take_expression(js, end + 1, call=True)
            if literal_string(second) not in ('_self', '_top'):
                continue
        value, method = resolve_static(expr, bindings)
        if value is None:
            signals.append(JsSignal('js_dynamic'))
        elif value:
            if 'open' in match.group(0).lower() and call:
                method = 'open_self'
            elif method == 'literal' and '[' in match.group(0):
                method = 'computed_literal'
            signals.append(JsSignal('js_' + ('static_' + method if method not in (
                'literal', 'dynamic') else method), value))
        if len(signals) >= max_signals:
            break
    return signals


class _InlineScripts(HTMLParser):
    def __init__(self, max_signals: int):
        super().__init__(convert_charrefs=True)
        self.max_signals = max_signals
        self.signals: list[JsSignal] = []
        self.in_script = False
        self.parts: list[str] = []
        self.remaining = MAX_HTML

    def handle_starttag(self, tag: str, attrs):
        if tag == 'script':
            self.in_script = True
            self.parts = []

    def handle_data(self, data: str):
        if self.in_script and self.remaining > 0 and len(self.signals) < self.max_signals:
            chunk = data[:self.remaining]
            self.remaining -= len(chunk)
            self.parts.append(chunk)

    def handle_endtag(self, tag: str):
        if tag == 'script' and self.in_script:
            if self.parts and len(self.signals) < self.max_signals:
                self.signals.extend(scan_js(''.join(self.parts), max_signals=self.max_signals - len(self.signals)))
            self.in_script = False
            self.parts = []


def analyze_html(html: str, *, max_signals: int = MAX_SIGNALS) -> list[JsSignal]:
    parser = _InlineScripts(max(0, min(max_signals, MAX_SIGNALS)))
    parser.feed(html[:MAX_HTML])
    return parser.signals[:max_signals]


def build_redirect_chains(observations, *, limit: int = 10, max_hops: int = 4) -> list[dict]:
    """Preview observed HTTP paths separately from unverified JS/meta hints.

    No network requests. Loops, branches, and hop counts are bounded. These
    edges may have been observed in different fetches; not a replay guarantee.
    """
    if limit <= 0:
        return []
    limit = min(limit, 100)
    max_hops = max(1, min(max_hops, 10))
    edges = list(observations)[:2000]
    http_edges = [x for x in edges if x.method == 'http']
    by_source: dict[tuple[str, str], list] = {}
    incoming = set()
    for edge in http_edges:
        by_source.setdefault((edge.seed_hostname, edge.source_url), []).append(edge)
        incoming.add((edge.seed_hostname, edge.destination_url))
    result: list[dict] = []
    consumed: set[tuple[str, str, str, int]] = set()

    def key(edge):
        return (edge.seed_hostname, edge.source_url, edge.destination_url, edge.http_status)

    def append_path(edge):
        if key(edge) in consumed or len(result) >= limit:
            return
        hops = []
        seen_urls = {edge.source_url}
        current = edge
        while current is not None and len(hops) < max_hops:
            if key(current) in consumed:
                break
            consumed.add(key(current))
            hops.append({'method': 'http', 'http_status': current.http_status,
                         'destination': current.destination_url,
                         'external': current.external})
            if current.external or current.destination_url in seen_urls:
                break
            seen_urls.add(current.destination_url)
            next_edges = by_source.get((current.seed_hostname, current.destination_url), [])
            current = next((x for x in next_edges if key(x) not in consumed), None)
        if hops:
            result.append({'seed': edge.seed_hostname, 'confidence': 'http_observed',
                           'source': edge.source_url, 'hops': hops})

    for edge in http_edges:
        if (edge.seed_hostname, edge.source_url) not in incoming:
            append_path(edge)
    # Disconnected cycles / secondary branches have no unique root.
    for edge in http_edges:
        append_path(edge)
    for edge in edges:
        if len(result) >= limit:
            break
        if edge.method != 'http':
            result.append({'seed': edge.seed_hostname, 'confidence': 'static_signal',
                           'method': edge.method, 'source': edge.source_url,
                           'destination': edge.destination_url, 'external': edge.external})
    return result
