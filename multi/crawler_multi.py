#!/usr/bin/env python3
"""Parallel HTTP + single shared IndoBERT + one isolated browser worker.

Compatible with the existing PANDI crawler detection functions. Does not modify
crawler_url.py, config.json, urls.json, or the legacy progress.txt.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import io
import itertools
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

from telegram_checks import CHECK_SCHEMA, normalize_input_url, save_results
from routine_queue import find_next_pending_job
from network_retry_queue import RetryPacer, choose_next_job
from discovery_priority import DiscoveryPacer

_HTTP_LOCAL = threading.local()
_INFERENCE_LOCK = threading.Lock()
_STOP = threading.Event()
_HOST_LOCK = threading.Lock()
_HOST_NEXT_ALLOWED = {}


def utcnow():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(message):
    print(f'[{utcnow()}] {message}', flush=True)


def arg_parser():
    p = argparse.ArgumentParser(description='PANDI parallel crawler; IndoBERT stays enabled')
    p.add_argument('--engine-file', default='/home/fauzi/crawler/crawler_url.py',
                   help='Existing single-worker script (imported for classifiers)')
    p.add_argument('--telegram-checks', action='store_true',
                   help='Prioritize Telegram checks and wait for new work after the routine queue drains')
    p.add_argument('--input', default='/home/fauzi/crawler/urls.json')
    p.add_argument('--db', default='/home/fauzi/crawler/crawler_multi.sqlite3')
    p.add_argument('--evidence-dir', default='',
                   help='Deprecated/ignored: BIMA screenshot is never saved locally')
    p.add_argument('--http-workers', type=int, default=3)
    p.add_argument('--import-limit', type=int, default=0,
                   help='Import only first N unique valid URLs; 0 = entire file')
    p.add_argument('--retry-network', action='store_true',
                   help='Retry due DNS/timeout jobs from earlier runs')
    p.add_argument('--auto-network-retry', action='store_true',
                   help='Opt-in: dispatch due DNS/timeout retries while running')
    p.add_argument('--auto-retry-every', type=int, default=5,
                   help='One retry after N-1 normal jobs, if due')
    p.add_argument('--auto-retry-max-per-minute', type=int, default=10,
                   help='Rolling 60 second cap on automatic retries')
    p.add_argument('--network-retry-minutes', type=int, default=30,
                   help='Retry delay (minutes) for transient DNS / HTTP timeouts')
    p.add_argument('--dns-no-address-retry-minutes', type=int, default=360,
                   help='Retry delay (minutes) for EAI_NODATA (-5); default 6 hours')
    p.add_argument('--dns-not-found-retry-minutes', type=int, default=1440,
                   help='Retry delay (minutes) for EAI_NONAME (-2); default 24 hours')
    p.add_argument('--max-network-attempts', type=int, default=3,
                   help='Total HTTP attempts before persistent errors become inconclusive')
    p.add_argument('--http-connect-timeout', type=float, default=3.0)
    p.add_argument('--http-read-timeout', type=float, default=8.0)
    p.add_argument('--http-max-bytes', type=int, default=2_000_000)
    p.add_argument('--min-host-interval', type=float, default=0.30,
                   help='Minimum seconds between HTTP requests to same host')
    p.add_argument('--browser-sample-rate', type=float, default=0.05,
                   help='Deprecated: legacy parity opens every HTTP-low-risk URL in Selenium')
    p.add_argument('--browser-queue-size', type=int, default=20)
    p.add_argument('--browser-restart-every', type=int, default=100)
    p.add_argument('--browser-max-age', type=int, default=900)
    p.add_argument('--chrome-mode', choices=('managed', 'system'), default='managed',
                   help='managed: pinned Chrome for Testing + matching patched ChromeDriver')
    p.add_argument('--chrome-cache-dir', default=str(Path.home() / '.cache/pandi-crawler/chrome'),
                   help='Persistent cache for matching Chrome and ChromeDriver binaries')
    p.add_argument('--chrome-update-days', type=int, default=7,
                   help='Check for Chrome security updates every N days; 0 disables auto-checks')
    p.add_argument('--chrome-refresh', action='store_true',
                   help='Force one Chrome for Testing update check on this run')
    p.add_argument('--chrome-binary', default='/usr/bin/google-chrome',
                   help='Used only in --chrome-mode system')
    p.add_argument('--bert-batch-size', type=int, default=2,
                   help='Per-URL token/chunk batch size used by existing IndoBERT')
    p.add_argument('--torch-threads', type=int, default=1,
                   help='PyTorch intra-op CPU threads (start with 1 on a 2-vCPU VM)')
    p.add_argument('--limit', type=int, default=1000,
                   help='Max NEW URL attempts this run; 0 = all pending')
    p.add_argument('--retry-failed', action='store_true')
    p.add_argument('--retry-dry-run', action='store_true',
                   help='Reprocess previous dry_run_alert jobs in production')
    p.add_argument('--retry-pending-alerts', action='store_true',
                   help='MANUAL: after checking BIMA for duplicates, recrawl pending alerts '
                        'and create new in-memory screenshots')
    p.add_argument('--dry-run', action='store_true',
                   help='Create screenshot in RAM but NEVER send alert to BIMA')
    p.add_argument('--http-only', action='store_true',
                   help='Benchmark HTTP+IndoBERT only; browser-required items remain queued')
    p.add_argument('--verbose-engine', action='store_true')
    return p


def validate_options(args):
    if not 1 <= args.http_workers <= 16:
        raise ValueError('http-workers must be 1..16 (start with 3 on this VM)')
    if not 0 <= args.browser_sample_rate <= 1:
        raise ValueError('browser-sample-rate must be between 0 and 1')
    if args.bert_batch_size < 1:
        raise ValueError('bert-batch-size must be positive')
    if not 1 <= getattr(args, 'torch_threads', 1) <= 8:
        raise ValueError('torch-threads must be 1..8')
    if args.browser_restart_every < 1:
        raise ValueError('browser-restart-every must be positive')
    if args.limit < 0:
        raise ValueError('limit must be >= 0')
    if args.import_limit < 0:
        raise ValueError('import-limit must be >= 0')
    if args.network_retry_minutes < 0:
        raise ValueError('network-retry-minutes must be >= 0')
    if getattr(args, 'dns_no_address_retry_minutes', 360) < 0:
        raise ValueError('dns-no-address-retry-minutes must be >= 0')
    if getattr(args, 'dns_not_found_retry_minutes', 1440) < 0:
        raise ValueError('dns-not-found-retry-minutes must be >= 0')
    if args.max_network_attempts < 1:
        raise ValueError('max-network-attempts must be >= 1')
    if getattr(args, 'auto_retry_every', 5) < 2:
        raise ValueError('auto-retry-every must be >= 2')
    if getattr(args, 'auto_retry_max_per_minute', 10) < 1:
        raise ValueError('auto-retry-max-per-minute must be >= 1')
    if getattr(args, 'auto_network_retry', False) and args.retry_network:
        raise ValueError('Use --auto-network-retry OR --retry-network, not both')
    if args.http_max_bytes < 1024:
        raise ValueError('http-max-bytes must be >= 1024')
    if args.min_host_interval < 0:
        raise ValueError('min-host-interval must be >= 0')
    if getattr(args, 'chrome_update_days', 7) < 0:
        raise ValueError('chrome-update-days must be >= 0')
    if not args.http_only and hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise RuntimeError('Chrome must not run as root. Run this script as a non-root user '
                           'with browser sandbox enabled. --http-only is available for tests.')


def load_engine(filename, args):
    path = Path(filename)
    if not path.is_file():
        raise FileNotFoundError(f'Detection engine not found: {path}')
    # Set CPU thread limits BEFORE torch initialization in the legacy module.
    threads = getattr(args, 'torch_threads', 1)
    os.environ.setdefault('OMP_NUM_THREADS', str(threads))
    os.environ.setdefault('MKL_NUM_THREADS', str(threads))
    spec = importlib.util.spec_from_file_location('pandi_legacy_detection', str(path))
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    if engine.torch is None:
        raise RuntimeError('PyTorch or transformers is not installed; IndoBERT is mandatory')
    try:
        engine.torch.set_num_threads(threads)
        engine.torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    engine.INDO_BERT_BATCH_SIZE = args.bert_batch_size
    if not engine.ENABLE_INDO_BERT or engine.indobert_model is None:
        engine.ENABLE_INDO_BERT = True
        engine.load_indobert_if_enabled()
    if not engine.ENABLE_INDO_BERT or engine.indobert_model is None or engine.indobert_tokenizer is None:
        raise RuntimeError('IndoBERT could not be loaded. Verify INDO_BERT_MODEL_PATH in config.json')
    if not args.verbose_engine:
        original_log = engine.log
        engine.log = lambda message: original_log(message) if (
            '[WARN]' in message or '[ERROR]' in message) else None
    log(f'IndoBERT loaded ONCE from {engine.INDO_BERT_MODEL_PATH}; '
        f'batch={args.bert_batch_size}; torch_threads={threads}')
    return engine


def validate_public_target(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Unsupported URL target or scheme')
    if parsed.username or parsed.password:
        raise ValueError('Credentials in URL not allowed')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if port not in (80, 443):
        raise ValueError('Only HTTP(S) standard ports are allowed')
    host = parsed.hostname
    if host.lower() in ('localhost', 'localhost.localdomain') or host.endswith('.localhost'):
        raise ValueError('Localhost is blocked')
    try:
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f'DNS lookup failed: {exc}') from exc
    if not addresses:
        raise ValueError('DNS lookup produced no addresses')
    for item in addresses:
        address = ipaddress.ip_address(item[4][0].split('%')[0])
        if not address.is_global:
            raise ValueError(f'Non-public address blocked: {address}')
    return True


def http_session():
    session = getattr(_HTTP_LOCAL, 'session', None)
    if session is None:
        session = requests.Session()
        session.trust_env = False  # Prevent automatic use of an unsafe proxy.
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; PANDI-Research-Crawler/1.0)',
            'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.3',
            'Accept-Language': 'id-ID,id;q=0.9,en;q=0.7',
        })
        _HTTP_LOCAL.session = session
    return session


def throttle_host(host, interval):
    if interval <= 0:
        return
    while True:
        now = time.monotonic()
        with _HOST_LOCK:
            due = _HOST_NEXT_ALLOWED.get(host, 0.0)
            if now >= due:
                _HOST_NEXT_ALLOWED[host] = now + interval
                return
        time.sleep(min(0.1, due-now))


def fetch_http(engine, input_url, args):
    started = time.monotonic()
    last_error = ''
    urls_to_try = [input_url]
    if input_url.startswith('https://'):
        urls_to_try.append('http://' + input_url[len('https://'):])
    for initial in urls_to_try:
        target = initial
        for redirect_count in range(6):
            response = None
            try:
                # Every redirect is validated. A network egress firewall is also required
                # to mitigate DNS rebinding between validation and connection.
                validate_public_target(target)
                throttle_host(urlparse(target).hostname, args.min_host_interval)
                response = http_session().get(
                    target, timeout=(args.http_connect_timeout, args.http_read_timeout),
                    allow_redirects=False, stream=True,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise ValueError('Redirect response without Location')
                    target = urljoin(target, location)
                    if redirect_count == 5:
                        raise ValueError('Too many redirects')
                    continue
                content_type = response.headers.get('Content-Type', '').lower()
                if content_type and not any(x in content_type for x in ('html', 'text/', 'xml')):
                    return {'ok': False, 'reason': 'non_html_content', 'error': content_type,
                            'final_url': target, 'status_code': response.status_code,
                            'duration_ms': round((time.monotonic()-started)*1000)}
                data_parts, total = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > args.http_max_bytes:
                        raise ValueError('HTTP body exceeds configured max bytes')
                    data_parts.append(chunk)
                body = b''.join(data_parts).decode(response.encoding or 'utf-8', 'replace')
                text, title, hidden, hits, evidence = engine.extract_text_from_html(
                    body, page_url=target)
                return {'ok': 200 <= response.status_code < 400,
                        'reason': 'http_ok' if 200 <= response.status_code < 400 else 'http_status',
                        'error': '' if response.status_code < 400 else f'HTTP {response.status_code}',
                        'final_url': target, 'status_code': response.status_code,
                        'html': body, 'text': text, 'title': title,
                        'hidden_gambling_hit': hidden, 'hidden_gambling_hits': hits,
                        'hidden_gambling_evidence': evidence,
                        'duration_ms': round((time.monotonic()-started)*1000)}
            except Exception as exc:
                last_error = str(exc)
                # https and http use the same hostname: after getaddrinfo() fails,
                # immediately defer DNS retry rather than issuing a duplicate DNS
                # lookup via the alternate scheme on the same host.
                if last_error.lower().startswith('dns lookup failed:'):
                    return {'ok': False, 'reason': 'fetch_failed',
                            'error': last_error, 'final_url': target, 'status_code': None,
                            'duration_ms': round((time.monotonic()-started)*1000)}
                # Do not fall back to HTTP when a target is blocked for safety reasons.
                if any(t in last_error.lower() for t in (
                    'non-public', 'localhost', 'unsupported', 'credentials',
                    'standard ports', 'http body exceeds', 'too many redirects')):
                    return {'ok': False, 'reason': 'unsafe_or_oversized_target',
                            'error': last_error, 'final_url': target, 'status_code': None,
                            'duration_ms': round((time.monotonic()-started)*1000)}
                break
            finally:
                if response is not None:
                    response.close()
    return {'ok': False, 'reason': 'fetch_failed', 'error': last_error,
            'final_url': input_url, 'status_code': None,
            'duration_ms': round((time.monotonic()-started)*1000)}


# Use the original crawler_url.py decision engine as the ONLY authority for
# detecting gambling. This module must not redefine any safe-context rules,
# ML/IndoBERT thresholds, or combinations used for BIMA decisions.
def capture_review_excerpt(classification, text):
    if (os.environ.get("BIMA_REVIEW_EXCERPTS") == "1"
            and classification.get("status") in ("medium_risk", "high_risk")):
        excerpt = " ".join((text or "")[:2400].split())[:600]
        if len(excerpt) >= 40:
            classification["review_excerpt"] = excerpt


def classify_shared(engine, url, current_url, text, title, source='requests', page_html=None):
    # The original process_url() checks minimum text lengths BEFORE invoking
    # classify_text_content(): 50 characters for requests, 30 for Selenium.
    minimum = 50 if source == 'requests' else 30
    if engine.is_captcha_or_antibot_page(text, title):
        return {'status': 'blocked_or_captcha', 'reason': 'captcha_or_antibot',
                'ml_conf': 0.0, 'bert_used': False, 'bert_ms': 0}
    if len(text or '') < minimum:
        return {'status': 'inconclusive', 'reason': 'short_or_empty_text',
                'ml_conf': 0.0, 'bert_used': False, 'bert_ms': 0}

    # Only wrap the actual legacy IndoBERT call for profiling. The legacy
    # function controls WHETHER to call it and HOW to combine predictions.
    # The shared inference lock protects the temporary instrumentation.
    with _INFERENCE_LOCK:
        original_bert = engine.is_gambling_content_indobert
        bert_durations = []
        bert_predictions = []

        def measured_legacy_bert(*bert_args, **bert_kwargs):
            start = time.monotonic()
            try:
                prediction = original_bert(*bert_args, **bert_kwargs)
                bert_predictions.append(prediction)
                return prediction
            finally:
                bert_durations.append(round((time.monotonic() - start) * 1000))

        engine.is_gambling_content_indobert = measured_legacy_bert
        try:
            legacy_result = engine.classify_text_content(
                source=source, url=url, page_text=text,
                page_title=title, current_url=current_url,
                **({"page_html": page_html} if hasattr(engine, "has_slot_thailand_registration") else {}))
        finally:
            engine.is_gambling_content_indobert = original_bert

    result = dict(legacy_result)
    result['bert_used'] = bool(bert_durations)
    result['bert_ms'] = sum(bert_durations)
    if bert_predictions:
        last = bert_predictions[-1]
        if isinstance(last, (tuple, list)) and len(last) >= 3:
            result['indobert_conf'] = float(last[1])
            result['indobert_pred'] = str(last[2])
    return result


_legacy_classify_shared = classify_shared


def classify_shared(engine, url, current_url, text, title, source='requests', page_html=None):
    """Preserve the existing IndoBERT/gambling decision; append opt-in shadow metadata."""
    result = _legacy_classify_shared(engine, url, current_url, text, title,
                                     source=source, page_html=page_html)
    if os.environ.get('BIMA_PHISHING_SHADOW') != '1':
        return result
    try:
        from phishing_signals import enrich_classification
        return enrich_classification(result, initial_url=url,
                                     current_url=current_url, page_html=page_html)
    except Exception:
        # A telemetry-only detector may never cause a crawler failure or new alert.
        safe = dict(result)
        safe['phishing_triage'] = {
            'version': 'phishing_shadow_v1', 'status': 'scanner_error', 'signal_codes': []}
        return safe


def browser_sampling_selected(url, rate):
    fraction = int.from_bytes(hashlib.sha256(url.encode()).digest()[:8], 'big') / (1 << 64)
    return fraction < rate


def route_to_browser(engine, url, http, classification, sample_rate):
    # Legacy process_url() takes a screenshot for HTTP hidden links and HTTP
    # high/medium risks, and ALWAYS continues to Selenium for other usable
    # pages. It does NOT sample just 5% of HTTP-low-risk pages.
    if http.get('hidden_gambling_hit'):
        return 'hidden_link_http'
    if classification.get('status') in ('high_risk', 'medium_risk'):
        return 'risky_http'
    if not http.get('ok'):
        # Keep the multi-worker's SSRF/size protections; unsafe/non-HTML
        # targets must not be sent to a browser.
        if http.get('reason') in ('unsafe_or_oversized_target', 'non_html_content'):
            return None
        return 'http_failure_or_block'
    if classification.get('status') in ('blocked_or_captcha', 'inconclusive'):
        return 'inconclusive_http'
    return 'legacy_selenium_followup'


def classify_http_error(http):
    """Classify transport failures; never equate an error with a safe page."""
    reason = str(http.get('reason') or '').lower()
    error = str(http.get('error') or '').lower()
    if reason == 'unsafe_or_oversized_target':
        return 'blocked_target'
    if reason == 'non_html_content':
        return 'non_html'
    if reason == 'http_status':
        return 'http_status'
    if not http.get('ok'):
        if any(x in error for x in (
            'dns lookup failed', 'name or service not known', 'temporary failure in name resolution',
            'failed to resolve', 'nameresolutionerror', 'getaddrinfo failed',
            'nodename nor servname provided', 'no address associated with hostname',
            'err_name_not_resolved')):
            return 'dns_error'
        if any(x in error for x in (
            'timed out', 'timeout', 'read timed out', 'connection timeout')):
            return 'timeout'
        if any(x in error for x in (
            'certificate', 'ssl', 'tls', 'hostname mismatch', 'wrong version number')):
            return 'ssl_error'
        if 'connection refused' in error:
            return 'connection_refused'
        if any(x in error for x in (
            'connection reset', 'remotedisconnected', 'connection aborted',
            'remote end closed connection')):
            return 'connection_reset'
        return 'other_network_error'
    return 'none'



def diagnose_dns_error(http):
    """Describe getaddrinfo() failures without changing legacy error_category.

    The coarse error_category='dns_error' stays compatible with existing SQL,
    while this new field distinguishes transient failures from missing DNS data.
    Do not interpret any failure as proof that a domain is safe or malicious.
    """
    if classify_http_error(http) != 'dns_error':
        return 'none', None
    error = str(http.get('error') or '')
    match = re.search(r'\[Errno\s+(-?\d+)\]', error)
    errno = int(match.group(1)) if match else None
    code_map = {
        getattr(socket, 'EAI_AGAIN', -3): 'temporary_failure',
        getattr(socket, 'EAI_NODATA', -5): 'no_address',
        getattr(socket, 'EAI_NONAME', -2): 'not_found',
    }
    if errno in code_map:
        return code_map[errno], errno
    lowered = error.lower()
    if 'temporary failure' in lowered:
        return 'temporary_failure', errno
    if 'no address associated' in lowered:
        return 'no_address', errno
    if 'name or service not known' in lowered:
        return 'not_found', errno
    return 'unclassified', errno


def network_retry_delay_minutes(args, dns_result):
    """Use a longer cooldown for absent/no-address names than for temporary errors."""
    if dns_result == 'no_address':
        return getattr(args, 'dns_no_address_retry_minutes', 360)
    if dns_result == 'not_found':
        return getattr(args, 'dns_not_found_retry_minutes', 1440)
    return args.network_retry_minutes


def plan_http_outcome(engine, url, http, classification, sample_rate, attempts, max_attempts):
    """Return (database status, route reason, error category).

    DNS/timeout results are DEFERRED for HTTP retry, never counted as safe.
    Once retry budget is exhausted they become inconclusive for review.
    Other failures preserve the previous browser-routing policy.
    """
    category = classify_http_error(http)
    if http.get('reason') == 'fetch_failed' and category in ('dns_error', 'timeout'):
        if attempts < max_attempts:
            return 'retry_http', f'deferred_{category}', category
        return 'inconclusive', f'retry_exhausted_{category}', category
    route = route_to_browser(engine, url, http, classification, sample_rate)
    if route:
        return 'browser_queued', route, category
    if classification.get('status') == 'low_risk':
        return 'complete', 'http_classification_complete', category
    return 'inconclusive', 'http_inconclusive', category

def is_browser_version_error(exc):
    """Only version/driver-compatibility errors trigger a browser re-download."""
    message = str(exc).lower()
    return any(phrase in message for phrase in (
        'this version of chromedriver only supports chrome version',
        'current browser version is',
        'session not created: chrome version',
        'cannot find chrome binary',
        'no chrome binary at',
    ))


def create_browser_driver(args):
    """Launch UC with version-locked Chrome and an official matching driver.

    UC patches its *own copy* of the exact driver from Chrome for Testing.
    Updating system Chrome does not change managed-mode browser versions.
    On an actual version mismatch, re-provision once and retry in-place.
    """
    import undetected_chromedriver as uc
    from chrome_runtime import (ensure_browser, prepare_uc_driver, read_binary_version)

    mode = getattr(args, 'chrome_mode', 'managed')
    for attempt in range(3):
        profile = tempfile.mkdtemp(prefix='pandi-crawler-browser-')
        try:
            chrome_bin = args.chrome_binary
            kwargs = {}
            if mode == 'managed':
                refresh = bool(getattr(args, 'chrome_refresh', False))
                # --chrome-refresh applies once; periodic browser recycling is local.
                args.chrome_refresh = False
                runtime = ensure_browser(
                    args.chrome_cache_dir, update_days=args.chrome_update_days,
                    force_refresh=refresh, logger=log,
                )
                chrome_bin = str(runtime.chrome)
                kwargs['driver_executable_path'] = str(prepare_uc_driver(
                    runtime, args.chrome_cache_dir))
                kwargs['version_main'] = int(runtime.version.split('.')[0])
            else:
                browser_version = read_binary_version(chrome_bin)
                kwargs['version_main'] = int(browser_version.split('.')[0])

            opts = uc.ChromeOptions()
            # Do NOT use --no-sandbox; crawl under non-root with egress controls.
            opts.add_argument('--headless=new')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-extensions')
            opts.add_argument('--disable-background-networking')
            opts.add_argument('--disable-sync')
            opts.add_argument('--disable-notifications')
            opts.add_argument('--window-size=1366,1800')
            opts.add_argument('--no-first-run')
            opts.page_load_strategy = 'eager'
            opts.add_argument(f'--user-data-dir={profile}')
            driver = uc.Chrome(
                options=opts, use_subprocess=True, no_sandbox=False,
                browser_executable_path=chrome_bin, **kwargs,
            )
            driver.set_page_load_timeout(25)
            driver.set_script_timeout(15)
            return driver, profile
        except Exception as exc:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
            if attempt < 2 and is_browser_version_error(exc) and mode == 'managed':
                if attempt == 0:
                    log(f'WARN: ChromeDriver version issue; rebuilding driver from cache: {exc}')
                    runtime = ensure_browser(
                        args.chrome_cache_dir, update_days=args.chrome_update_days,
                        logger=log,
                    )
                else:
                    log('WARN: Local driver repair was insufficient; downloading '
                        'a fresh matching Chrome + ChromeDriver pair')
                    runtime = ensure_browser(
                        args.chrome_cache_dir, update_days=args.chrome_update_days,
                        force_refresh=True, force_reinstall=True, logger=log,
                    )
                prepare_uc_driver(runtime, args.chrome_cache_dir, force_rebuild=True)
                continue
            if attempt == 0 and is_browser_version_error(exc) and mode == 'system':
                log('WARN: Chrome system driver mismatch; retry UC driver selection once. '
                    'Managed mode is recommended for reliability.')
                continue
            raise


def close_browser(driver, profile):
    import shutil
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


def evidence_screenshot(engine, driver, url, evidence, output_dir=None):
    """Return watermarked PNG bytes in RAM; never persist screenshot on disk.

    1. Inject the original hidden-link evidence panel when applicable.
    2. Use original CDP/fallback screenshot routine.
    3. Apply original two-line BIMA watermark using engine.add_watermark().
    4. Encode PNG to memory for the unchanged engine.send_alert() payload.

    output_dir is ignored for compatibility with older CLI/test callers.
    """
    from PIL import Image

    if evidence:
        engine.inject_hidden_evidence_panel(driver, url, evidence)

    screenshot_png = engine.take_screenshot_png(driver)
    original_image = Image.open(io.BytesIO(screenshot_png))
    watermarked_image = None
    try:
        timestamp = utcnow()
        watermark1 = f'Screenshot taken at {timestamp}'
        watermark2 = f'from BIMA: {url}'
        watermarked_image = engine.add_watermark(original_image, watermark1, watermark2)
        with io.BytesIO() as memory_file:
            watermarked_image.save(memory_file, format='PNG')
            return memory_file.getvalue()
    finally:
        original_image.close()
        if watermarked_image is not None:
            watermarked_image.close()


class TelegramBrowserQueue(queue.PriorityQueue):
    """Serve requested pages first, keeping FIFO within each priority."""
    def __init__(self, maxsize):
        super().__init__(maxsize)
        self.sequence = itertools.count()

    def _put(self, task):
        priority = 2 if task is None else (0 if task.get('telegram_requested') else 1)
        super()._put((priority, next(self.sequence), task))

    def _get(self):
        return super()._get()[2]


def browser_loop(engine, tasks, results, args):
    # Follow original crawler_url.py process_url() alert decision order:
    # 1) HTTP hidden gambling evidence => screenshot + alert (confidence 0.60)
    # 2) HTTP high/medium classification => screenshot + alert on HTTP result
    # 3) Otherwise, render with Selenium and classify via ORIGINAL function
    #    before deciding whether to alert. No new review-only decision gate.
    driver = None
    profile = None
    opened_at = 0.0
    total_browser_urls = 0
    try:
        while True:
            task = tasks.get()
            if task is None:
                tasks.task_done()
                return
            started = time.monotonic()
            record = {'id': task['id'], 'url': task['url']}
            try:
                if (driver is None or
                    total_browser_urls >= args.browser_restart_every or
                    time.monotonic() - opened_at >= args.browser_max_age):
                    close_browser(driver, profile)
                    driver, profile = create_browser_driver(args)
                    opened_at = time.monotonic()
                    total_browser_urls = 0
                total_browser_urls += 1
                input_url = task['url']
                http = task['http_result']
                http_cls = task['http_classification']
                # Original crawler uses Requests' final URL for browser
                # navigation if Requests successfully reached that URL.
                active_url = ((http.get('final_url') or input_url)
                              if http.get('ok') else input_url)
                validate_public_target(active_url)
                opened_url = engine.selenium_get_with_scheme_fallback(driver, active_url)
                try:
                    engine.wait_page_loaded(driver, timeout=15)
                except Exception:
                    engine.stop_page_loading(driver)
                final_url = driver.current_url or opened_url or active_url
                validate_public_target(final_url)
                record['final_url'] = final_url

                alert_url = engine.choose_alert_url_for_id_endpoint(
                    input_url, current_url=final_url)
                http_hidden = bool(http.get('hidden_gambling_hit'))
                http_positive = http_cls.get('status') in ('high_risk', 'medium_risk')
                if http_hidden or http_positive:
                    # Legacy HTTP-positive path DOES NOT reclassify the page
                    # before sending the already-decided alert to BIMA.
                    if not alert_url:
                        record.update(status='inconclusive', reason='no_id_alert_url')
                        continue
                    evidence = ((http.get('hidden_gambling_evidence') or [])
                                if http_hidden else [])
                    screenshot_png = evidence_screenshot(
                        engine, driver, alert_url, evidence)
                    record.update(
                        url=alert_url,
                        status='pending_alert',
                        reason=('legacy_http_hidden_alert' if http_hidden else
                                'legacy_http_classifier_alert'),
                        screenshot_png=screenshot_png,
                        confidence=(0.60 if http_hidden else
                                    max(float(http_cls.get('ml_conf') or 0),
                                        float(engine.MIN_ALERT_CONFIDENCE))),
                    )
                    continue

                # Original browser path: get_page_text() and driver.title,
                # not an additional independent hidden-link classifier.
                text = engine.get_page_text(driver)
                title = getattr(driver, 'title', '') or ''
                if engine.is_captcha_or_antibot_page(text, title):
                    finished = engine.wait_until_antibot_finished(
                        driver, max_wait=engine.ANTIBOT_MAX_WAIT_SECONDS)
                    final_url = driver.current_url or final_url
                    validate_public_target(final_url)
                    record['final_url'] = final_url
                    text = engine.get_page_text(driver)
                    title = getattr(driver, 'title', '') or ''
                    if not finished or engine.is_captcha_or_antibot_page(text, title):
                        record.update(status='inconclusive', reason='captcha_or_antibot')
                        continue
                if len(text or '') < 30:
                    record.update(status='inconclusive', reason='short_or_empty_text')
                    continue
                browser_cls = classify_shared(
                    engine, input_url, final_url, text, title, source='selenium',
                    page_html=lambda: driver.page_source)
                # The flat top-level decision stays queryable as before.
                # Store the HTTP intermediate result only as audit metadata.
                capture_review_excerpt(browser_cls, text)
                record['browser_classification'] = {
                    **browser_cls, 'http_classification': http_cls}
                record['reason'] = browser_cls.get('reason')
                if browser_cls['status'] in ('high_risk', 'medium_risk'):
                    if not alert_url:
                        record.update(status='inconclusive', reason='no_id_alert_url')
                        continue
                    screenshot_png = evidence_screenshot(
                        engine, driver, alert_url, [])
                    record.update(
                        url=alert_url,
                        status='pending_alert',
                        reason='legacy_browser_classifier_alert',
                        screenshot_png=screenshot_png,
                        confidence=max(float(browser_cls.get('ml_conf') or 0),
                                       float(engine.MIN_ALERT_CONFIDENCE)),
                    )
                elif browser_cls['status'] in ('low_risk', 'blocked_or_captcha'):
                    record['status'] = 'complete' if browser_cls['status'] == 'low_risk' else 'inconclusive'
                else:
                    record['status'] = 'inconclusive'
            except Exception as exc:
                record.update(status='inconclusive', reason='browser_error', error=str(exc)[:500])
                close_browser(driver, profile)
                driver, profile = None, None
            finally:
                record['browser_ms'] = round((time.monotonic() - started) * 1000)
                results.put(record)
                tasks.task_done()
    finally:
        close_browser(driver, profile)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 url TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 http_ms INTEGER,
 browser_ms INTEGER,
 http_status INTEGER,
 reason TEXT,
 risk TEXT,
 classifier_json TEXT,
 final_url TEXT,
 evidence_path TEXT,
 alert_confidence REAL,
 error TEXT,
 updated_at TEXT,
 route_reason TEXT,
 error_category TEXT,
 classification_ms INTEGER,
 next_retry_at TEXT,
 bert_ms INTEGER,
 dns_result TEXT,
 dns_errno INTEGER,
 dns_host TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE TABLE IF NOT EXISTS dns_host_audits (
 hostname TEXT PRIMARY KEY,
 diagnosis TEXT NOT NULL,
 delegated_zone TEXT,
 authority_rcodes TEXT,
 details_json TEXT NOT NULL,
 checked_at TEXT NOT NULL,
 audit_error TEXT
);
'''


def connect_db(db_filename):
    Path(db_filename).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_filename, timeout=30)
    conn.executescript(SCHEMA + CHECK_SCHEMA)
    if os.environ.get('BIMA_PREALERT_ENABLED') == '1':
        from selective_prealert import PREALERT_SCHEMA
        conn.executescript(PREALERT_SCHEMA)
    if 'progress_sent' not in {r[1] for r in conn.execute('PRAGMA table_info(telegram_checks)')}:
        conn.execute('ALTER TABLE telegram_checks ADD COLUMN progress_sent INTEGER NOT NULL DEFAULT 0')
    # Safe additive migration for existing v1.2.1 databases.
    existing = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
    for col, sqltype in (('route_reason','TEXT'), ('error_category','TEXT'),
                         ('classification_ms','INTEGER'), ('next_retry_at','TEXT'),
                         ('bert_ms','INTEGER'), ('dns_result','TEXT'), ('dns_errno','INTEGER'),
                         ('dns_host','TEXT')):
        if col not in existing:
            conn.execute(f'ALTER TABLE jobs ADD COLUMN {col} {sqltype}')
    journal_mode = os.environ.get('BIMA_SQLITE_JOURNAL_MODE', 'WAL').upper()
    if journal_mode not in ('WAL', 'DELETE'):
        conn.close()
        raise ValueError('BIMA_SQLITE_JOURNAL_MODE must be WAL or DELETE')
    actual_mode = conn.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0]
    if actual_mode.upper() != journal_mode:
        conn.close()
        raise RuntimeError(f'SQLite journal mode is {actual_mode}, expected {journal_mode}')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.commit()
    return conn


def import_input_urls(conn, input_file, import_limit=0):
    with open(input_file, 'r', encoding='utf-8') as source:
        root = json.load(source)
    urls = root['urls'] if isinstance(root, dict) else root
    if not isinstance(urls, list):
        raise ValueError('Input must contain a JSON array, or object with "urls" array')
    # dict preserves source order; unlike set, smoke tests use the FIRST N URLs.
    unique_urls = {}
    skipped = 0
    bad_indices = []
    stopped_early = False
    for index, item in enumerate(urls, start=1):
        clean = normalize_input_url(item)
        if clean:
            unique_urls[clean] = None
            if import_limit and len(unique_urls) >= import_limit:
                stopped_early = True
                break
        else:
            skipped += 1
            if len(bad_indices) < 10:
                # Log indexes, not raw URLs (which might contain private tokens).
                bad_indices.append(index)
    before = conn.total_changes
    with conn:
        conn.executemany('INSERT OR IGNORE INTO jobs(url,updated_at) VALUES (?,?)',
                         ((url, utcnow()) for url in unique_urls))
    added = conn.total_changes - before
    log(f'Input imported: {len(unique_urls)} unique .id URLs; new={added}; invalid/non-.id={skipped}'
        + ('; import limit reached (partial input)' if stopped_early else ''))
    if bad_indices:
        log(f'WARN: Skipped invalid/non-.id input positions (1-based, first 10): {bad_indices}')


def set_status(conn, id_, status, **updates):
    allowed = {'http_ms', 'browser_ms', 'http_status', 'reason', 'risk', 'classifier_json',
               'final_url', 'evidence_path', 'alert_confidence', 'error',
               'route_reason', 'error_category', 'classification_ms', 'next_retry_at',
               'bert_ms', 'dns_result', 'dns_errno', 'dns_host'}
    updates = {k: v for k, v in updates.items() if k in allowed}
    columns = ['status=?', 'updated_at=?'] + [f'{k}=?' for k in updates]
    params = [status, utcnow(), *updates.values(), id_]
    with conn:
        conn.execute(f'UPDATE jobs SET {", ".join(columns)} WHERE id=?', params)
        save_results(conn, id_, status, updates.get('reason'))


def build_browser_result_updates(result):
    """Preserve original HTTP risk when Selenium only captures alert evidence.

    The HTTP decision and classifier_json were persisted when the browser
    task was queued. No browser classification exists for the legacy
    HTTP-positive/hidden-link screenshot paths, so do not erase those fields.
    """
    updates = {'browser_ms': result.get('browser_ms'),
               'reason': result.get('reason'),
               'final_url': result.get('final_url'),
               'evidence_path': None,
               'alert_confidence': result.get('confidence'),
               'error': result.get('error')}
    browser_cls = result.get('browser_classification')
    if isinstance(browser_cls, dict) and browser_cls:
        updates['risk'] = browser_cls.get('status')
        updates['classifier_json'] = json.dumps(browser_cls)
    elif result.get('status') != 'pending_alert':
        # Preserve legacy behavior for non-alert browser failures.
        updates['risk'] = None
    return updates


def send_pending_alert(engine, conn, id_, url, confidence, screenshot_png, dry_run):
    """Send screenshot directly from RAM to original BIMA endpoint.

    No screenshot bytes/base64 are written to SQLite or disk. A failed or
    ambiguous response remains pending_alert; a later retry MUST recapture the
    webpage and should be requested only after checking BIMA for duplicates.
    """
    if not isinstance(screenshot_png, bytes) or not screenshot_png.startswith(b'\x89PNG\r\n\x1a\n'):
        set_status(conn, id_, 'inconclusive', reason='missing_evidence_in_memory',
                   evidence_path=None)
        return False
    if dry_run:
        set_status(conn, id_, 'dry_run_alert', reason='dry_run_no_bima_call',
                   evidence_path=None)
        return True
    try:
        b64 = base64.b64encode(screenshot_png).decode('ascii')
        # Original BIMA payload and decision are unchanged.
        ok = engine.send_alert(url, b64, confidence)
        if ok:
            set_status(conn, id_, 'alerted', reason='bima_alert_sent',
                       evidence_path=None)
            log(f'BIMA alert sent: {url}')
            return True
        set_status(conn, id_, 'pending_alert',
                   reason='bima_alert_send_failed_check_bima_before_retry',
                   evidence_path=None)
        log(f'WARN: BIMA response not confirmed, manual retry required: {url}')
        return False
    except Exception as exc:
        set_status(conn, id_, 'pending_alert', error=str(exc)[:500],
                   reason='bima_alert_exception_check_bima_before_retry',
                   evidence_path=None)
        log(f'WARN: BIMA send exception; check duplicate before retry: {exc}')
        return False


def requeue_pending_alerts(conn, enabled=False):
    """Only requeue an unconfirmed BIMA alert with explicit operator intent.

    The original screenshot is intentionally not persisted; the URL must be
    fetched and reclassified and a fresh screenshot taken before a new send.
    """
    count = conn.execute('SELECT COUNT(*) FROM jobs WHERE status="pending_alert"').fetchone()[0]
    if not count:
        return
    if not enabled:
        log(f'WARN: {count} unconfirmed BIMA alert(s) retained; check BIMA, then '
            'use --retry-pending-alerts to recrawl them (no screenshot retained)')
        return
    with conn:
        conn.execute('UPDATE jobs SET status="pending", evidence_path=NULL, '
                     'reason="manual_pending_alert_recrawl" '
                     'WHERE status="pending_alert"')
    log(f'Requeued {count} unconfirmed BIMA alerts for recrawl; '
        'check BIMA for duplicates BEFORE enabling this option')


def print_stats(conn, elapsed, processed):
    counts = dict(conn.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status').fetchall())
    attempted = sum(counts.get(x, 0) for x in (
        'complete', 'alerted', 'inconclusive', 'review_required', 'dry_run_alert',
        'pending_alert', 'retry_http', 'browser_queued', 'awaiting_review',
        'review_approved', 'review_rejected', 'review_needs_evidence',
        'prealert_send_unknown', 'prealert_error'))
    log(f'Run finished; http attempts this run={processed}; elapsed={elapsed:.1f}s; '
        f'throughput={processed/max(elapsed,0.01)*60:.2f} URL/minute')
    log(f'Stored states: {json.dumps(counts, ensure_ascii=False)}')
    log(f'Attempted outcomes (all runs)={attempted}; completion is NOT the same as attempted')
    metrics = conn.execute("SELECT COUNT(*), ROUND(AVG(http_ms)), "
                           "ROUND(AVG(classification_ms)) FROM jobs "
                           "WHERE attempts>0").fetchone()
    log(f'HTTP timing, all attempted: rows={metrics[0]}, avg_http_ms={metrics[1]}, '
        f'avg_classification_ms={metrics[2]}')
    measured = conn.execute(
        "SELECT COUNT(*), ROUND(AVG(bert_ms)), ROUND(SUM(bert_ms)), "
        "ROUND(MAX(bert_ms)) FROM jobs WHERE bert_ms IS NOT NULL AND bert_ms>0"
    ).fetchone()
    log(f'IndoBERT timing, classified pages={measured[0]}, '
        f'avg_bert_ms={measured[1]}, total_bert_ms={measured[2]}, '
        f'max_bert_ms={measured[3]}')
    dns_counts = conn.execute(
        "SELECT dns_result, COUNT(*) FROM jobs WHERE status='retry_http' "
        "AND error_category='dns_error' "
        "GROUP BY dns_result ORDER BY COUNT(*) DESC"
    ).fetchall()
    if dns_counts:
        log(f'Deferred DNS details: {json.dumps(dict(dns_counts))}')


def run(args):
    validate_options(args)
    interactive = getattr(args, 'telegram_checks', False)
    if interactive and (args.limit or args.http_only):
        raise ValueError('--telegram-checks requires --limit 0 and browser support')
    if getattr(args, 'auto_network_retry', False) and not interactive:
        raise ValueError('--auto-network-retry requires --telegram-checks --limit 0')
    engine = load_engine(args.engine_file, args)
    conn = connect_db(args.db)
    started = time.monotonic()
    try:
        import_input_urls(conn, args.input, import_limit=args.import_limit)
        with conn:
            conn.execute("UPDATE jobs SET status='pending' WHERE status='http_inflight'")
            if not args.http_only:
                conn.execute("UPDATE jobs SET status='pending' WHERE status='browser_queued'")
            if args.retry_failed:
                conn.execute("UPDATE jobs SET status='pending' WHERE status IN ('failed','inconclusive')")
            if args.retry_network:
                now = utcnow()
                count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='retry_http' "
                                     "AND (next_retry_at IS NULL OR next_retry_at<=?)",
                                     (now,)).fetchone()[0]
                conn.execute("UPDATE jobs SET status='pending', next_retry_at=NULL "
                             "WHERE status='retry_http' "
                             "AND (next_retry_at IS NULL OR next_retry_at<=?)", (now,))
                log(f'Network retries ready: {count} (retry-network enabled)')
            if args.retry_dry_run:
                conn.execute("UPDATE jobs SET status='pending' WHERE status='dry_run_alert'")
        # A crash after recording pending_alert may leave Telegram callers waiting.
        with conn:
            conn.execute("""UPDATE telegram_checks SET (result,risk,reason,checked_at) =
                (SELECT status,risk,reason,updated_at FROM jobs WHERE jobs.id=telegram_checks.job_id)
                WHERE result IS NULL AND job_id IN (SELECT id FROM jobs WHERE status='pending_alert')""")
        requeue_pending_alerts(conn, getattr(args, 'retry_pending_alerts', False))
        queue_type = TelegramBrowserQueue if interactive else queue.Queue
        tasks = queue_type(maxsize=args.browser_queue_size)
        # One in-memory screenshot at a time to cap RAM usage on 2-vCPU / 4-GiB VMs.
        browser_results = queue.Queue(maxsize=1)
        browser_thread = None
        if not args.http_only:
            browser_thread = threading.Thread(
                target=browser_loop, args=(engine, tasks, browser_results, args),
                name='one-browser-worker', daemon=True,
            )
            browser_thread.start()
        processed, waiting_browser, last_id, logged = 0, 0, 0, 0
        futures = {}
        retry_pacer = (RetryPacer(getattr(args, 'auto_retry_every', 5),
                                  getattr(args, 'auto_retry_max_per_minute', 10))
                       if getattr(args, 'auto_network_retry', False) else None)
        if retry_pacer is not None:
            log(f'Auto network retry enabled: every={retry_pacer.every}, '
                f'max_per_minute={retry_pacer.max_per_minute}')
        discovery_pacer = DiscoveryPacer(max_burst=4)
        limit = args.limit or float('inf')

        def handle_browser_result(result):
            nonlocal waiting_browser
            waiting_browser -= 1
            id_ = result['id']
            state = result['status']
            updates = build_browser_result_updates(result)
            set_status(conn, id_, state, **updates)
            if state == 'pending_alert':
                # Only selected candidates wait for Telegram; non-selected ones
                # keep the existing engine.send_alert() transport unchanged.
                if os.environ.get('BIMA_PREALERT_ENABLED') == '1' and not args.dry_run:
                    try:
                        from selective_prealert import enqueue_candidate
                        if enqueue_candidate(conn, id_, result['url'], result['confidence'],
                                             result.get('screenshot_png')):
                            log(f'Pre-alert selected for private review: job_id={id_}')
                            result.pop('screenshot_png', None)
                            return
                    except Exception as exc:
                        # Ambiguous queue state: NEVER bypass reviewer approval.
                        log(f'ERROR: pre-alert queue failed for job_id={id_}: {type(exc).__name__}')
                        set_status(conn, id_, 'prealert_error', reason='prealert_queue_check_required')
                        result.pop('screenshot_png', None)
                        return
                # Original direct BIMA sender for EVERY non-selected candidate.
                send_pending_alert(engine, conn, id_, result['url'],
                                   result['confidence'], result.get('screenshot_png'), args.dry_run)
                result.pop('screenshot_png', None)

        def drain_browser_results():
            while True:
                try:
                    item = browser_results.get_nowait()
                except queue.Empty:
                    return
                handle_browser_result(item)
                browser_results.task_done()

        def submit_one(pool):
            nonlocal last_id
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                priority = None
                if interactive:
                    priority = conn.execute(
                        "SELECT j.id,j.url FROM telegram_checks t CROSS JOIN jobs j ON j.id=t.job_id "
                        "WHERE t.result IS NULL AND j.status='pending' ORDER BY t.id LIMIT 1"
                    ).fetchone()
                # Leave capacity for interactive work when Selenium is backlogged.
                if interactive and priority is None and waiting_browser + len(futures) >= args.browser_queue_size:
                    return False
                now_mono = time.monotonic()
                row, kind = choose_next_job(
                    conn, last_id, interactive=interactive, priority=priority,
                    pacer=retry_pacer, now_utc=utcnow(), now_monotonic=now_mono,
                    max_attempts=args.max_network_attempts,
                    discovery_pacer=discovery_pacer)
                if row is None:
                    return False
                id_, url = row
                if kind == 'routine':
                    last_id = id_
                conn.execute("UPDATE jobs SET status='http_inflight', attempts=attempts+1, "
                             "updated_at=? WHERE id=?", (utcnow(), id_))
                if retry_pacer is not None:
                    retry_pacer.record(kind, now_mono)
                discovery_pacer.record(kind)
            futures[pool.submit(fetch_http, engine, url, args)] = (id_, url)
            return True

        # In interactive mode the executor must not hold routine work for a busy slot.
        submission_limit = args.http_workers if interactive else args.http_workers * 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.http_workers) as pool:
            while not _STOP.is_set():
                drain_browser_results()
                while (len(futures) < submission_limit and
                       processed + len(futures) < limit):
                    if not submit_one(pool):
                        break
                if not futures:
                    if interactive:
                        _STOP.wait(1)
                        continue
                    break
                done, _ = concurrent.futures.wait(
                    futures, timeout=0.3,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    id_, url = futures.pop(future)
                    processed += 1
                    # Pick the next URL before synchronous classification or browser backpressure.
                    if interactive and not _STOP.is_set():
                        submit_one(pool)
                    try:
                        http = future.result()
                        class_started = time.monotonic()
                        cls = (classify_shared(engine, url, http.get('final_url', url),
                                               http.get('text', ''), http.get('title', ''),
                                               source='requests', page_html=http.get('html'))
                               if (http.get('ok') or http.get('html')) and
                                  not http.get('hidden_gambling_hit') else
                               {'status': ('hidden_link_evidence'
                                           if http.get('hidden_gambling_hit') else 'inconclusive'),
                                'reason': ('hidden_link_http'
                                           if http.get('hidden_gambling_hit') else http.get('reason')),
                                'bert_used': False, 'ml_conf': 0.0})
                        capture_review_excerpt(cls, http.get("text"))
                        class_ms = round((time.monotonic() - class_started) * 1000)
                        attempts = conn.execute('SELECT attempts FROM jobs WHERE id=?',
                                                (id_,)).fetchone()[0]
                        next_status, route_reason, error_category = plan_http_outcome(
                            engine, url, http, cls, args.browser_sample_rate,
                            attempts, args.max_network_attempts)
                        dns_result, dns_errno = diagnose_dns_error(http)
                        dns_host = None
                        if error_category == 'dns_error':
                            dns_host = (urlparse(http.get('final_url') or url).hostname or '').lower().rstrip('.')
                        updates = {'http_ms': http.get('duration_ms'),
                                   'classification_ms': class_ms,
                                   'bert_ms': cls.get('bert_ms', 0),
                                   'dns_result': dns_result,
                                   'dns_errno': dns_errno,
                                   'dns_host': dns_host,
                                   'error_category': error_category,
                                   'route_reason': route_reason,
                                   'http_status': http.get('status_code'),
                                   'reason': cls.get('reason'), 'risk': cls.get('status'),
                                   'classifier_json': json.dumps(cls, ensure_ascii=False),
                                   'final_url': http.get('final_url'),
                                   'error': http.get('error')}
                        if next_status == 'retry_http':
                            retry_minutes = network_retry_delay_minutes(args, dns_result)
                            retry_at = (datetime.now(timezone.utc) + timedelta(
                                minutes=retry_minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
                            set_status(conn, id_, 'retry_http', next_retry_at=retry_at, **updates)
                        elif next_status == 'browser_queued':
                            set_status(conn, id_, 'browser_queued', **updates)
                            if not args.http_only:
                                requested = interactive and conn.execute(
                                    'SELECT 1 FROM telegram_checks WHERE job_id=? AND result IS NULL LIMIT 1',
                                    (id_,)).fetchone() is not None
                                while True:
                                    try:
                                        tasks.put({'id': id_, 'url': url, 'telegram_requested': requested,
                                                   'http_classification': cls,
                                                   'http_result': http}, timeout=0.5)
                                        waiting_browser += 1
                                        break
                                    except queue.Full:
                                        drain_browser_results()
                                        if _STOP.is_set():
                                            break
                        else:
                            set_status(conn, id_, next_status, **updates)
                    except Exception as exc:
                        set_status(conn, id_, 'failed', error=str(exc)[:500],
                                   reason='http_or_classifier_exception')
                        log(f'WARN: failed URL {url}: {exc}')
                    if processed - logged >= 25:
                        deferred_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='retry_http'").fetchone()[0]
                        browser_queued_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='browser_queued'").fetchone()[0]
                        log(f'HTTP attempted={processed}, browser_queued={browser_queued_count}, '
                            f'http_retry_pending={deferred_count}, browser_inflight={waiting_browser}, '
                            f'elapsed={time.monotonic()-started:.0f}s')
                        if retry_pacer is not None:
                            log(f'auto_retry_dispatched={retry_pacer.retries_dispatched}')
                        logged = processed
        if browser_thread is not None:
            while waiting_browser:
                try:
                    item = browser_results.get(timeout=2)
                    handle_browser_result(item)
                    browser_results.task_done()
                except queue.Empty:
                    if not browser_thread.is_alive():
                        log('ERROR: browser worker exited unexpectedly')
                        break
            if browser_thread.is_alive():
                tasks.put(None)
            browser_thread.join(timeout=5)
        print_stats(conn, time.monotonic()-started, processed)
    finally:
        conn.close()


def main():
    from bima_private_config import apply_private_config
    apply_private_config('crawler')
    args = arg_parser().parse_args()
    def stop_handler(signum, frame):
        _STOP.set()
        log(f'Signal {signum}: stop accepting more HTTP tasks')
    for signame in ('SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, signame), stop_handler)
    try:
        run(args)
    except Exception as exc:
        log(f'FATAL: {exc}')
        if '--verbose-engine' in sys.argv:
            raise
        sys.exit(1)


if __name__ == '__main__':
    main()
