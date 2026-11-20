#!/usr/bin/env python3
"""Version-locked Chrome for Testing + ChromeDriver for the PANDI crawler.

Downloads matching binaries from the official Google Chrome for Testing feed;
keeps a non-root, private cache and reuses the installed pair. The ChromeDriver
copy handed to undetected_chromedriver is private, so UC can patch it without
modifying the pristine, version-matched driver. No automatic OS/package changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile

import requests

METADATA_URL = (
    'https://googlechromelabs.github.io/chrome-for-testing/'
    'last-known-good-versions-with-downloads.json'
)
DOWNLOAD_PREFIX = 'https://storage.googleapis.com/chrome-for-testing-public/'
VERSION_RE = re.compile(r'^\d+\.\d+\.\d+\.\d+$')
DOWNLOAD_BYTE_CAPS = {'chrome': 450 * 1024 * 1024, 'chromedriver': 75 * 1024 * 1024}
EXPANDED_BYTE_CAPS = {'chrome': 1400 * 1024 * 1024, 'chromedriver': 150 * 1024 * 1024}


@dataclass(frozen=True)
class ChromeRuntime:
    version: str
    chrome: Path
    driver: Path


def read_binary_version(binary):
    """Return a four-component binary version or raise, e.g. 157.0.8103.38."""
    output = subprocess.run(
        [str(binary), '--version'], text=True, capture_output=True,
        timeout=15, check=True,
    ).stdout
    match = re.search(r'\b(\d+\.\d+\.\d+\.\d+)\b', output)
    if not match:
        raise RuntimeError(f'Cannot extract version from {binary}: {output[:200]}')
    return match.group(1)


def runtime_for(cache_dir, version):
    root = Path(cache_dir) / 'versions' / version
    return ChromeRuntime(
        version=version,
        chrome=root / 'chrome-linux64' / 'chrome',
        driver=root / 'chromedriver-linux64' / 'chromedriver',
    )


def valid_runtime(runtime):
    try:
        return (runtime.chrome.is_file() and runtime.driver.is_file()
                and read_binary_version(runtime.chrome) == runtime.version
                and read_binary_version(runtime.driver) == runtime.version)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def _read_manifest(cache):
    path = cache / 'current.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not VERSION_RE.fullmatch(str(data.get('version', ''))):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _write_manifest(cache, version):
    payload = {'version': version, 'last_checked_utc': datetime.now(timezone.utc).isoformat()}
    with tempfile.NamedTemporaryFile(mode='w', prefix='.manifest-', suffix='.json',
                                     dir=cache, encoding='utf-8', delete=False) as temp:
        json.dump(payload, temp, indent=2)
        temp_path = Path(temp.name)
    try:
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, cache / 'current.json')
    finally:
        temp_path.unlink(missing_ok=True)


def _needs_refresh(manifest, update_days):
    if update_days <= 0:
        return False  # explicit manual updates only
    try:
        checked = datetime.fromisoformat(manifest['last_checked_utc'])
        if checked.tzinfo is None:
            return True
        return (datetime.now(timezone.utc) - checked).total_seconds() >= update_days * 86400
    except (ValueError, KeyError, TypeError):
        return True


def _fetch_release_metadata(session):
    response = session.get(METADATA_URL, timeout=(10, 30), allow_redirects=False)
    try:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Unexpected status from official Chrome metadata')
        data = response.json()['channels']['Stable']
        version = str(data['version'])
        if not VERSION_RE.fullmatch(version):
            raise ValueError('Invalid version in official Chrome for Testing metadata')
        urls = {}
        for name in ('chrome', 'chromedriver'):
            matches = [x['url'] for x in data['downloads'][name]
                       if x.get('platform') == 'linux64']
            if len(matches) != 1:
                raise ValueError(f'No unique official Linux x64 download: {name}')
            url = matches[0]
            if not url.startswith(f'{DOWNLOAD_PREFIX}{version}/linux64/'):
                raise ValueError('Unexpected Chrome download origin/version')
            if not url.endswith(f'/{name}-linux64.zip'):
                raise ValueError('Unexpected Chrome archive filename')
            urls[name] = url
        return version, urls
    finally:
        response.close()


def _download_zip(session, url, destination, max_bytes):
    response = session.get(url, timeout=(15, 180), stream=True, allow_redirects=False)
    try:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('Redirect refused for binary download')
        total = 0
        with open(destination, 'wb') as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError('Chrome for Testing archive exceeds download size cap')
                output.write(chunk)
        if total < 1024:
            raise ValueError('Downloaded Chrome for Testing archive is unexpectedly small')
    finally:
        response.close()


def _extract_zip_safe(zip_path, output_dir, folder, max_expanded_bytes):
    """Extract a Chrome archive, rejecting paths outside the expected folder."""
    with zipfile.ZipFile(zip_path) as archive:
        total = 0
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] != folder):
                raise ValueError(f'Unsafe Chrome archive entry: {entry.filename}')
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise ValueError(f'Archive symlink not supported: {entry.filename}')
            total += entry.file_size
            if total > max_expanded_bytes:
                raise ValueError('Uncompressed Chrome for Testing files exceed size cap')
            dest = output_dir.joinpath(*path.parts)
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, open(dest, 'wb') as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                # Chrome needs executable helpers and libraries, not just chrome itself.
                os.chmod(dest, 0o755 if mode & 0o111 else 0o644)


def _install(cache, session, version, urls):
    if os.name != 'posix':
        raise RuntimeError('Managed browser in this package currently supports Linux x64 only')
    versions = cache / 'versions'
    versions.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.chrome-download-', dir=cache) as temporary:
        tmp = Path(temporary)
        stage = tmp / 'payload'
        stage.mkdir()
        for name, folder in [('chrome', 'chrome-linux64'),
                             ('chromedriver', 'chromedriver-linux64')]:
            zip_path = tmp / f'{name}.zip'
            _download_zip(session, urls[name], zip_path, DOWNLOAD_BYTE_CAPS[name])
            _extract_zip_safe(zip_path, stage, folder, EXPANDED_BYTE_CAPS[name])
            zip_path.unlink()
        runtime = ChromeRuntime(version, stage / 'chrome-linux64' / 'chrome',
                                stage / 'chromedriver-linux64' / 'chromedriver')
        for binary in (runtime.chrome, runtime.driver):
            if not binary.is_file():
                raise RuntimeError(f'Missing binary from official Chrome zip: {binary}')
            os.chmod(binary, 0o755)
        if not valid_runtime(runtime):
            raise RuntimeError('Downloaded Chrome and ChromeDriver do not match release metadata')
        target = versions / version
        backup = tmp / 'backup'
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup.exists():
                os.replace(backup, target)
            raise
    return runtime_for(cache, version)


def ensure_browser(cache_dir, update_days=7, force_refresh=False, force_reinstall=False,
                   session=None, logger=None):
    """Download matching Chrome/Driver once, reuse; update only on schedule.

    On an update-network failure, a locally verified pair is reused unless a
    corrupt/mismatched pair is being repaired. Never silently replace a working
    browser with a half-downloaded new version.
    """
    cache = Path(cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = _read_manifest(cache)
    previous = runtime_for(cache, manifest['version']) if manifest else None
    previous_valid = bool(previous and valid_runtime(previous))
    if previous_valid and not (force_refresh or force_reinstall or
                               _needs_refresh(manifest, update_days)):
        return previous
    client = session or requests.Session()
    should_close = session is None
    try:
        version, urls = _fetch_release_metadata(client)
        new_runtime = runtime_for(cache, version)
        if force_reinstall or not valid_runtime(new_runtime):
            new_runtime = _install(cache, client, version, urls)
            if logger:
                logger(f'Chrome for Testing {version} installed and validated')
        _write_manifest(cache, version)
        return new_runtime
    except Exception as exc:
        if previous_valid and not force_reinstall:
            if logger:
                logger(f'Chrome update check failed, using verified cached {previous.version}: {exc}')
            return previous
        raise RuntimeError(f'Chrome for Testing provisioning failed: {exc}') from exc
    finally:
        if should_close:
            client.close()


def prepare_uc_driver(runtime, cache_dir, force_rebuild=False):
    """Make a UC-patchable copy of the exact matching official ChromeDriver."""
    patched_dir = Path(cache_dir).expanduser() / 'uc-patched' / runtime.version
    patched_dir.mkdir(parents=True, exist_ok=True)
    patched = patched_dir / 'chromedriver'
    if not force_rebuild and patched.exists():
        try:
            if read_binary_version(patched) == runtime.version:
                return patched
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
    with tempfile.NamedTemporaryFile(prefix='.chrome-driver-', dir=patched_dir,
                                     delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        shutil.copy2(runtime.driver, temp_path)
        os.chmod(temp_path, 0o755)
        if read_binary_version(temp_path) != runtime.version:
            raise RuntimeError('Copied ChromeDriver does not match Chrome version')
        os.replace(temp_path, patched)
    finally:
        temp_path.unlink(missing_ok=True)
    return patched
