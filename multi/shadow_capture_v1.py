"""Opt-in, best-effort observation feed for an OUT-OF-PROCESS IndoBERT shadow model.

Never participates in BIMA routing, alert submission, or classifier decisions.
This module imports no torch/transformers and performs no network requests.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import urlsplit

VERSION = 'bima_indobert_shadow_capture_v1'
MAX_TEXT_CHARS = 16000  # More than 8 * 1800-character windows; capture is bounded.
MAX_EVENT_BYTES = 64000
DEFAULT_PENDING_LIMIT = 250


def _config():
    if os.environ.get('BIMA_INDOBERT_SHADOW_CAPTURE') != '1':
        return None
    try:
        rate = float(os.environ.get('BIMA_INDOBERT_SHADOW_RATE', '0.01'))
        limit = int(os.environ.get('BIMA_INDOBERT_SHADOW_PENDING_LIMIT',
                                   str(DEFAULT_PENDING_LIMIT)))
    except ValueError:
        return None
    if not (0.0 < rate <= 0.1 and 1 <= limit <= 5000):
        return None
    directory = os.environ.get('BIMA_INDOBERT_SHADOW_SPOOL_DIR', '').strip()
    if not directory or not os.path.isabs(directory):
        return None
    return rate, limit, Path(directory)


def _private_dir(path):
    """Never follow the final directory symlink; existing directory must be owner-only."""
    if path.is_symlink():
        return False
    if not path.exists():
        # Never automatically create a parent path from crawler user-supplied input.
        if not path.parent.is_dir() or path.parent.is_symlink():
            return False
        path.mkdir(mode=0o700)
    info = path.stat()
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0)


def _sample(url, rate):
    fraction = int.from_bytes(hashlib.sha256(url.encode('utf-8')).digest()[:8], 'big') / 2**64
    return fraction < rate


def build_event(url, current_url, text, source, result):
    if not isinstance(text, str) or not text.strip():
        return None
    # Avoid query strings and credentials in the audit metadata.
    hostname = (urlsplit(current_url or url).hostname or '').lower().rstrip('.')
    if not hostname or not hostname.endswith('.id'):
        return None
    capped = text[:MAX_TEXT_CHARS]
    raw_hash = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
    return {
        'version': VERSION,
        'event_id': uuid.uuid4().hex,
        'captured_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'hostname': hostname,
        'source': source,
        'text': capped,
        'original_text_sha256': raw_hash,
        'captured_text_sha256': hashlib.sha256(capped.encode('utf-8', 'replace')).hexdigest(),
        'text_truncated': len(text) > MAX_TEXT_CHARS,
        'original_classifier_status': str(result.get('status', ''))[:60],
        'original_indobert_used': bool(result.get('bert_used')),
        'original_indobert_conf': (float(result['indobert_conf'])
                                   if isinstance(result.get('indobert_conf'), (float, int))
                                   else None),
        'original_indobert_pred': str(result.get('indobert_pred', 'unknown'))[:50],
    }


def capture(url, current_url, text, source, result):
    """Fail-open, bounded publish: returns False on any error. No caller may rely on it."""
    try:
        config = _config()
        if config is None or not result.get('bert_used'):
            return False
        rate, pending_limit, directory = config
        if not _sample(url, rate) or not _private_dir(directory):
            return False
        # Bounded queue; no infinite on-disk raw text accumulation.
        pending = 0
        for item in os.scandir(directory):
            if item.name.endswith('.json') and item.is_file(follow_symlinks=False):
                pending += 1
                if pending >= pending_limit:
                    return False
        event = build_event(url, current_url, text, source, result)
        if event is None:
            return False
        body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_EVENT_BYTES:
            return False
        # Write to a private temporary file, then atomic rename to the pending queue.
        filename = event['event_id']
        tmp = directory / ('.' + filename + '.tmp')
        ready = directory / (filename + '.json')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(tmp), flags, 0o600)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, ready)
        finally:
            if tmp.exists():
                tmp.unlink()
        return True
    except Exception:
        # Opt-in telemetry must NEVER interrupt the production classifier.
        return False
