#!/usr/bin/env python3
"""One-shot BIMA private-config consolidation, activation and safe cleanup.

Run as root after deploying the code patch.  Keeps existing live services and
all source files unchanged until static validation passes.  Removes only
explicit legacy credential sources once BOTH services have restarted and stayed
active, and never touches the crawler database, its spool or model files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

APP = Path('/home/fauzi/crawler-rebuild')
USER = 'fauzi'
SERVICES = {
    'crawler': 'pandi-crawler-multi.service',
    'telegram': 'pandi-telegram-monitor.service',
}
DROPIN = Path('/etc/systemd/system/pandi-telegram-monitor.service.d/99-bima-unified.conf')
ENV_SOURCES = [
    APP / 'multi/telegram_monitor.env',
    Path('/home/fauzi/.config/bima/analyst.env'),
]
BRAVE_SOURCE = Path('/home/fauzi/.config/bima/brave-api-key')
LEGACY_CONFIG = APP / 'config.json'
RUNTIME_KEY = '__bima_runtime__'
KEY_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def run(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def parse_environment_file(path):
    """Parse simple systemd/sh KEY=VALUE assignments without eval or expansion."""
    out = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        if '=' not in line:
            raise ValueError(f'{path.name}: unsupported assignment on line {number}')
        name, value = line.split('=', 1)
        name = name.strip()
        if not KEY_PATTERN.fullmatch(name):
            raise ValueError(f'{path.name}: invalid variable on line {number}')
        try:
            parts = shlex.split('X=' + value, comments=False, posix=True)
        except ValueError:
            raise ValueError(f'{path.name}: invalid quoting on line {number}') from None
        if len(parts) != 1 or not parts[0].startswith('X='):
            raise ValueError(f'{path.name}: unsupported value on line {number}')
        out[name] = parts[0][2:]
    return out


def systemd_environment(service):
    """Read only effective inline Environment= assignments (no secret output)."""
    raw = run('systemctl', 'show', service, '-p', 'Environment', '--value').stdout
    variables = {}
    for part in shlex.split(raw.strip()):
        if '=' in part:
            name, value = part.split('=', 1)
            if KEY_PATTERN.fullmatch(name):
                variables[name] = value
    return variables


def ensure_private_directory(home, uid, gid):
    base = home / '.config'
    target = base / 'bima'
    if base.is_symlink() or target.is_symlink():
        raise ValueError('private config directory must not use symlinks')
    base.mkdir(mode=0o700, exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=True)
    os.chown(target, uid, gid)
    os.chmod(target, 0o700)
    return target


def atomic_private_json(path, payload, uid, gid):
    fd, name = tempfile.mkstemp(prefix='.bima-new-', suffix='.json', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ensure_regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'expected regular source: {path}')


def prepare_data(home, allow_prompt):
    ensure_regular(LEGACY_CONFIG)
    cfg = json.loads(LEGACY_CONFIG.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict) or not cfg.get('X_API_KEY') or not cfg.get('ENDPOINT_URL'):
        raise ValueError('legacy config missing BIMA API endpoint/key')
    if RUNTIME_KEY in cfg:
        raise ValueError('legacy config already contains reserved runtime key')

    telegram_env = systemd_environment(SERVICES['telegram'])
    crawler_env = systemd_environment(SERVICES['crawler'])
    sources_present = []
    # Later environment files override earlier ones, matching systemd order.
    for path in ENV_SOURCES:
        if path.exists():
            ensure_regular(path)
            telegram_env.update(parse_environment_file(path))
            sources_present.append(path)

    token = telegram_env.get('TELEGRAM_BOT_TOKEN', '').strip()
    reviewer = telegram_env.get('BIMA_PREALERT_REVIEWER_CHAT_ID', '').strip()
    allowed = {x.strip() for x in
               telegram_env.get('TELEGRAM_ALLOWED_CHAT_IDS', '').split(',') if x.strip()}
    if not token or not reviewer.isdecimal() or reviewer not in allowed:
        raise ValueError('Telegram token/reviewer authorization missing in existing sources')
    telegram_env['BIMA_PREALERT_ENABLED'] = '1'
    crawler_env['BIMA_PREALERT_ENABLED'] = '1'

    key = cfg.get('BRAVE_SEARCH_API_KEY', '')
    if isinstance(key, str) and key.startswith('${'):
        key = ''
    if BRAVE_SOURCE.exists():
        ensure_regular(BRAVE_SOURCE)
        key = BRAVE_SOURCE.read_text(encoding='utf-8').strip()
        sources_present.append(BRAVE_SOURCE)
    if not key:
        if not allow_prompt or not sys.stdin.isatty():
            raise ValueError('Brave key missing; interactive --apply requires one secure prompt')
        key = getpass.getpass('Brave API key (once; input hidden): ').strip()
    if not key or '\n' in key or '\r' in key:
        raise ValueError('invalid Brave API key')
    cfg['BRAVE_SEARCH_API_KEY'] = key
    discover_env = {
        'BIMA_DISCOVERY_AUTO_IMPORT': '1',
        'BIMA_ENABLE_BRAVE_DAILY': '1',
        'BIMA_DISCOVERY_INTERVAL_DAYS': '2',
        'BIMA_DISCOVERY_ANCHOR_UTC': '2026-11-22',
        'BIMA_BRAVE_REQUESTS_PER_RUN': '1',
        'BRAVE_SEARCH_API_KEY': key,
    }
    cfg[RUNTIME_KEY] = {
        'version': 1,
        'roles': {
            'crawler': crawler_env,
            'telegram': telegram_env,
            'discovery': discover_env,
        },
    }
    for role, env in cfg[RUNTIME_KEY]['roles'].items():
        if not all(KEY_PATTERN.fullmatch(k) and isinstance(v, str) and '\0' not in v
                   for k, v in env.items()):
            raise ValueError('invalid environment value in ' + role)
    return cfg, sources_present


def wait_active(name, before_pid):
    """Require sustained active state and a new main PID after controlled restart."""
    time.sleep(3)
    first = run('systemctl', 'show', name, '-p', 'MainPID', '--value').stdout.strip()
    if not first.isdecimal() or int(first) <= 0 or first == before_pid:
        raise RuntimeError(name + ' did not start a fresh process')
    if run('systemctl', 'is-active', name).stdout.strip() != 'active':
        raise RuntimeError(name + ' is not active after restart')
    time.sleep(3)
    second = run('systemctl', 'show', name, '-p', 'MainPID', '--value').stdout.strip()
    if second != first or run('systemctl', 'is-active', name).stdout.strip() != 'active':
        raise RuntimeError(name + ' did not remain active')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Migrate BIMA settings/keys into one private JSON')
    parser.add_argument('--plan', action='store_true', help='read-only inventory')
    parser.add_argument('--apply', action='store_true', help='migrate, restart, verify and clean')
    args = parser.parse_args(argv)
    if args.plan == args.apply:
        parser.error('choose exactly one of --plan or --apply')
    if os.geteuid() != 0:
        parser.error('run as root using sudo')

    account = pwd.getpwnam(USER)
    home = Path(account.pw_dir)
    private = home / '.config/bima/bima.json'
    if private.exists() or private.is_symlink():
        raise SystemExit('Unified BIMA config already exists; refusing to overwrite it')
    if args.plan:
        print('BIMA single-file migration plan (no values or credentials):')
        print('  Target:', private)
        for path in [LEGACY_CONFIG, *ENV_SOURCES, BRAVE_SOURCE]:
            print('  Source:', path, 'FOUND' if path.exists() else 'MISSING')
        for role, service in SERVICES.items():
            print('  Service:', service, run('systemctl', 'is-active', service, check=False).stdout.strip())
        print('  Discovery: two-day UTC cadence; Brave transient-only; auto-import limit unchanged')
        print('  --apply will restart both services and delete migrated legacy files after verification')
        return 0

    stage_dir = ensure_private_directory(home, account.pw_uid, account.pw_gid)
    cfg, cleanup = prepare_data(home, allow_prompt=True)
    saved = stage_dir / ('.legacy-config-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    if saved.exists():
        raise ValueError('migration staging path already exists')
    if DROPIN.exists() or DROPIN.is_symlink():
        raise ValueError('systemd migration override already exists; refusing overwrite')
    for service in SERVICES.values():
        if run('systemctl', 'is-active', service).stdout.strip() != 'active':
            raise RuntimeError(service + ' not active; aborting production migration')
    before = {role: run('systemctl', 'show', service, '-p', 'MainPID', '--value').stdout.strip()
              for role, service in SERVICES.items()}
    migrated = False
    activated = False
    created_dropin = False
    created_json = False
    try:
        atomic_private_json(private, cfg, account.pw_uid, account.pw_gid)
        created_json = True
        os.replace(LEGACY_CONFIG, saved)
        migrated = True
        os.symlink(str(private), str(LEGACY_CONFIG))
        DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        DROPIN.write_text('[Service]\nEnvironmentFile=\n', encoding='utf-8')
        DROPIN.chmod(0o644)
        created_dropin = True
        # Ensure new private path and role payloads can be read by service user.
        check = run('sudo', '-u', USER, '-H', str(APP / 'venv/bin/python3'), '-B',
                    '-c', 'import sys;sys.path.insert(0,sys.argv[1]);'
                          'from bima_private_config import read_private_config;'
                          'd=read_private_config();assert d["__bima_runtime__"]["roles"]["telegram"]["TELEGRAM_BOT_TOKEN"]',
                    str(APP / 'multi'))
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', SERVICES['telegram'])
        wait_active(SERVICES['telegram'], before['telegram'])
        run('systemctl', 'restart', SERVICES['crawler'])
        wait_active(SERVICES['crawler'], before['crawler'])
        files = run('systemctl', 'show', SERVICES['telegram'], '-p', 'EnvironmentFiles', '--value').stdout
        if 'analyst.env' in files or 'telegram_monitor.env' in files:
            raise RuntimeError('Telegram service still loading legacy EnvironmentFile')
        # Delete original legacy sources ONLY after both services pass health checks.
        # Cleanup failures must not roll back a healthy deployment after files
        # have been removed; report them for targeted inspection instead.
        activated = True
        for item in cleanup:
            ensure_regular(item)
        for item in cleanup:
            item.unlink()
        saved.unlink()
        print('SUCCESS: BIMA one-file config active; migrated old config and key files deleted.')
        print('  Private JSON:', private)
        print('  config.json: symlink to private JSON (not a separate credential copy)')
        print('  Discovery: 2-day UTC interval; Google + Brave + urlscan scheduled')
        print('  Tracked legacy credential file may still exist in Git history; rotate exposed tokens.')
        return 0
    except Exception:
        if activated:
            print('BIMA is active with the unified config, but legacy cleanup was incomplete.',
                  file=sys.stderr)
            print('Do not re-run --apply; inspect only the remaining old files.', file=sys.stderr)
            raise
        # Never erase legacy sources if activation did not succeed.
        if created_dropin and DROPIN.exists():
            DROPIN.unlink()
        if migrated:
            if LEGACY_CONFIG.is_symlink():
                LEGACY_CONFIG.unlink()
            if saved.exists():
                os.replace(saved, LEGACY_CONFIG)
        if created_json and private.exists():
            inactive = stage_dir / '.bima-migration-incomplete.json'
            if inactive.exists():
                inactive.unlink()
            os.replace(private, inactive)
        run('systemctl', 'daemon-reload', check=False)
        for service in SERVICES.values():
            run('systemctl', 'restart', service, check=False)
        print('FAILED: legacy configs preserved/restored; BIMA services restarted to old mode.', file=sys.stderr)
        raise


if __name__ == '__main__':
    sys.exit(main())
