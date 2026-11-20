#!/usr/bin/env python3
"""Optional preflight: install/check matching Chrome for Testing and ChromeDriver.

The crawler also invokes the same logic automatically at first browser startup.
This command is useful to verify network/cache/disk before crawling.
"""
import argparse
import os
from pathlib import Path
import sys

from chrome_runtime import ensure_browser, prepare_uc_driver, read_binary_version


def main():
    parser = argparse.ArgumentParser(description='PANDI managed Chrome setup')
    parser.add_argument('--cache-dir', default=str(Path.home() / '.cache/pandi-crawler/chrome'))
    parser.add_argument('--update-days', type=int, default=7)
    parser.add_argument('--refresh', action='store_true', help='Check latest Stable release now')
    parser.add_argument('--repair', action='store_true',
                        help='Force re-download of a fresh matching Chrome/Driver pair')
    args = parser.parse_args()
    if args.update_days < 0:
        parser.error('--update-days must be >= 0')
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        parser.error('Run as an unprivileged crawler user; do not create a root-owned browser cache')
    try:
        runtime = ensure_browser(
            args.cache_dir, update_days=args.update_days,
            force_refresh=args.refresh or args.repair,
            force_reinstall=args.repair, logger=print,
        )
        uc_driver = prepare_uc_driver(runtime, args.cache_dir, force_rebuild=args.repair)
    except Exception as exc:
        print(f'Chrome setup failed: {exc}', file=sys.stderr)
        return 1
    print('Chrome for Testing: ' + read_binary_version(runtime.chrome))
    print('ChromeDriver:       ' + read_binary_version(runtime.driver))
    print('UC driver copy:     ' + str(uc_driver))
    print('Cache directory:    ' + str(Path(args.cache_dir).expanduser()))
    print('Ready: Chrome and ChromeDriver versions match (browser startup not yet tested).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
