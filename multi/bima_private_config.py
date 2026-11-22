"""Load BIMA's single private JSON config; no credential logging or file copies.

The JSON is a backward-compatible superset of legacy config.json with an
additional __bima_runtime__ object.  Startup remains backward-compatible until
migration has been completed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

RUNTIME_KEY = '__bima_runtime__'
ROLES = {'crawler', 'telegram', 'discovery'}


def private_path():
    return Path(os.environ.get('BIMA_CONFIG_FILE') or
                Path.home() / '.config' / 'bima' / 'bima.json')


def read_private_config(path=None):
    path = Path(path) if path is not None else private_path()
    if path.is_symlink():
        raise ValueError('BIMA private config must not be a symlink')
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise PermissionError('BIMA private config must be a private 0600 regular file')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get(RUNTIME_KEY), dict):
        raise ValueError('BIMA private config schema missing')
    if not data.get('ENDPOINT_URL') or not data.get('X_API_KEY'):
        raise ValueError('BIMA private endpoint credentials missing')
    return data


def apply_private_config(role):
    """Populate selected role's process environment from one private file.

    Return False only for installations that have not migrated yet.  If the
    unified config exists but is malformed/insecure, fail closed on startup.
    """
    if role not in ROLES:
        raise ValueError('unknown BIMA role')
    path = private_path()
    if not path.exists() and not path.is_symlink():
        return False
    data = read_private_config(path)
    runtime = data[RUNTIME_KEY]
    roles = runtime.get('roles')
    if not isinstance(roles, dict):
        raise ValueError('BIMA private roles missing')
    env = roles.get(role)
    if not isinstance(env, dict):
        raise ValueError('BIMA private role missing')
    for key, value in env.items():
        if (not isinstance(key, str) or not key.isidentifier() or
                not isinstance(value, str) or '\0' in value):
            raise ValueError('BIMA private environment entry invalid')
        os.environ[key] = value
    os.environ['BIMA_CONFIG_FILE'] = str(path)
    os.environ['BIMA_PREALERT_CONFIG_PATH'] = str(path)
    os.environ['BIMA_PRIVATE_LOADED'] = '1'
    return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Run BIMA with one private configuration')
    parser.add_argument('--role', choices=sorted(ROLES), required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.command
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]
    if not cmd:
        parser.error('command required after --')
    if not apply_private_config(args.role):
        parser.error('unified BIMA configuration has not been migrated')
    os.execvpe(cmd[0], cmd, os.environ)


if __name__ == '__main__':
    sys.exit(main())
