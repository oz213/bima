#!/usr/bin/env python3
"""Upgrade the BIMA v4 crawler queue to bounded auto retry, without brittle source anchors.

- Reads and validates Python syntax/AST, and makes targeted line edits only.
- Refuses partial installations, modified crawler files, or a different base commit.
- Does not touch urls.json, config.json, IndoBERT, runtime SQLite or systemd.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import stat
import subprocess
import tempfile

BASE = Path(__file__).resolve().parents[1]
TARGET = BASE / 'multi' / 'crawler_multi.py'
EXPECTED_COMMIT = '3bdecf5'


def exact_one(items, label):
    if len(items) != 1:
        raise ValueError(f'{label}: expected exactly 1 match, found {len(items)}')
    return items[0]


def named_function(node, name):
    return exact_one([x for x in ast.walk(node)
                      if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and x.name == name], name)


def call_name(call, name):
    return isinstance(call, ast.Call) and (
        (isinstance(call.func, ast.Name) and call.func.id == name) or
        (isinstance(call.func, ast.Attribute) and call.func.attr == name))


def call_arg_text(stmt):
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    args = stmt.value.args
    if not args:
        return None
    try:
        return ast.literal_eval(args[0])
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def indentation(lines, node):
    line = lines[node.lineno - 1]
    return line[:len(line) - len(line.lstrip(' \t'))]


def upgrade_text(source):
    tree = ast.parse(source, filename='multi/crawler_multi.py')
    existing_markers = (
        'from network_retry_queue import RetryPacer, choose_next_job',
        "'--auto-network-retry'",
        'retry_pacer =',
        'row, kind = choose_next_job(',
        'retry_pacer.record(kind, now_mono)',
    )
    markers = [marker in source for marker in existing_markers]
    if all(markers):
        return 'already-upgraded', source
    if any(markers):
        raise ValueError('partial v4.1 modification detected; refusing to modify crawler_multi.py')

    lines = source.splitlines(keepends=True)
    if not lines or not lines[-1].endswith('\n'):
        raise ValueError('source must end with a newline')
    # Changes are (first line number, final line number inclusive, new text).
    changes = []

    def insert_after(stmt, addition, label):
        if stmt.end_lineno is None:
            raise ValueError(f'{label}: source range unavailable')
        changes.append((stmt.end_lineno + 1, stmt.end_lineno, addition))

    def replace(stmt, replacement, label):
        if stmt.end_lineno is None:
            raise ValueError(f'{label}: source range unavailable')
        changes.append((stmt.lineno, stmt.end_lineno, replacement))

    imports = [x for x in tree.body if isinstance(x, ast.ImportFrom)
               and x.module == 'routine_queue' and
               any(alias.name == 'find_next_pending_job' for alias in x.names)]
    existing_import = exact_one(imports, 'queue import')
    insert_after(existing_import, 'from network_retry_queue import RetryPacer, choose_next_job\n',
                 'retry import')

    parser = named_function(tree, 'arg_parser')
    cli_args = [x for x in ast.walk(parser) if isinstance(x, ast.Expr)
                and isinstance(x.value, ast.Call) and call_name(x.value, 'add_argument')
                and x.value.args and isinstance(x.value.args[0], ast.Constant)
                and x.value.args[0].value == '--retry-network']
    add_arg = exact_one(cli_args, '--retry-network option')
    ind = indentation(lines, add_arg)
    insert_after(add_arg, (
        f"{ind}p.add_argument('--auto-network-retry', action='store_true',\n"
        f"{ind}               help='Opt-in: dispatch due DNS/timeout retries while running')\n"
        f"{ind}p.add_argument('--auto-retry-every', type=int, default=5,\n"
        f"{ind}               help='One retry after N-1 normal jobs, if due')\n"
        f"{ind}p.add_argument('--auto-retry-max-per-minute', type=int, default=10,\n"
        f"{ind}               help='Rolling 60 second cap on automatic retries')\n"
    ), 'retry options')

    validator = named_function(tree, 'validate_options')
    max_attempt_checks = [x for x in ast.walk(validator) if isinstance(x, ast.If)
                          and isinstance(x.test, ast.Compare)
                          and isinstance(x.test.left, ast.Attribute)
                          and isinstance(x.test.left.value, ast.Name)
                          and x.test.left.value.id == 'args'
                          and x.test.left.attr == 'max_network_attempts'
                          and len(x.test.ops) == 1 and isinstance(x.test.ops[0], ast.Lt)
                          and len(x.test.comparators) == 1
                          and isinstance(x.test.comparators[0], ast.Constant)
                          and x.test.comparators[0].value == 1]
    max_check = exact_one(max_attempt_checks, 'max network attempts validation')
    ind = indentation(lines, max_check)
    insert_after(max_check, (
        f"{ind}if getattr(args, 'auto_retry_every', 5) < 2:\n"
        f"{ind}    raise ValueError('auto-retry-every must be >= 2')\n"
        f"{ind}if getattr(args, 'auto_retry_max_per_minute', 10) < 1:\n"
        f"{ind}    raise ValueError('auto-retry-max-per-minute must be >= 1')\n"
        f"{ind}if getattr(args, 'auto_network_retry', False) and args.retry_network:\n"
        f"{ind}    raise ValueError('Use --auto-network-retry OR --retry-network, not both')\n"
    ), 'validation')

    runner = named_function(tree, 'run')
    telegram_guards = [x for x in ast.walk(runner) if isinstance(x, ast.If)
                       and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                               and '--telegram-checks requires' in n.value
                               for n in ast.walk(x))]
    telegram_guard = exact_one(telegram_guards, 'telegram mode guard')
    ind = indentation(lines, telegram_guard)
    insert_after(telegram_guard, (
        f"{ind}if getattr(args, 'auto_network_retry', False) and not interactive:\n"
        f"{ind}    raise ValueError('--auto-network-retry requires --telegram-checks --limit 0')\n"
    ), 'auto retry mode guard')

    futures = [x for x in ast.walk(runner) if isinstance(x, ast.Assign)
               and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
               and x.targets[0].id == 'futures' and isinstance(x.value, ast.Dict)
               and not x.value.keys]
    futures_stmt = exact_one(futures, 'executor futures dict')
    ind = indentation(lines, futures_stmt)
    insert_after(futures_stmt, (
        f"{ind}retry_pacer = (RetryPacer(getattr(args, 'auto_retry_every', 5),\n"
        f"{ind}                          getattr(args, 'auto_retry_max_per_minute', 10))\n"
        f"{ind}               if getattr(args, 'auto_network_retry', False) else None)\n"
        f"{ind}if retry_pacer is not None:\n"
        f"{ind}    log(f'Auto network retry enabled: every={{retry_pacer.every}}, '\n"
        f"{ind}        f'max_per_minute={{retry_pacer.max_per_minute}}')\n"
    ), 'retry pacer init')

    submit = named_function(runner, 'submit_one')
    rows = [x for x in ast.walk(submit) if isinstance(x, ast.Assign)
            and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
            and x.targets[0].id == 'row'
            and isinstance(x.value, ast.BoolOp) and isinstance(x.value.op, ast.Or)
            and len(x.value.values) == 2
            and isinstance(x.value.values[0], ast.Name)
            and x.value.values[0].id == 'priority'
            and call_name(x.value.values[1], 'find_next_pending_job')]
    row_stmt = exact_one(rows, 'queue row selection')
    ind = indentation(lines, row_stmt)
    replace(row_stmt, (
        f"{ind}now_mono = time.monotonic()\n"
        f"{ind}row, kind = choose_next_job(\n"
        f"{ind}    conn, last_id, interactive=interactive, priority=priority,\n"
        f"{ind}    pacer=retry_pacer, now_utc=utcnow(), now_monotonic=now_mono,\n"
        f"{ind}    max_attempts=args.max_network_attempts)\n"
    ), 'queue chooser')

    cursor_ifs = [x for x in ast.walk(submit) if isinstance(x, ast.If)
                  and isinstance(x.test, ast.Compare)
                  and isinstance(x.test.left, ast.Name) and x.test.left.id == 'priority'
                  and len(x.test.ops) == 1 and isinstance(x.test.ops[0], ast.Is)
                  and len(x.test.comparators) == 1
                  and isinstance(x.test.comparators[0], ast.Constant)
                  and x.test.comparators[0].value is None
                  and len(x.body) == 1 and isinstance(x.body[0], ast.Assign)
                  and len(x.body[0].targets) == 1
                  and isinstance(x.body[0].targets[0], ast.Name)
                  and x.body[0].targets[0].id == 'last_id'
                  and isinstance(x.body[0].value, ast.Name)
                  and x.body[0].value.id == 'id_']
    cursor = exact_one(cursor_ifs, 'routine cursor update')
    ind = indentation(lines, cursor)
    replace(cursor, (
        f"{ind}if kind == 'routine':\n"
        f"{ind}    last_id = id_\n"
    ), 'routine cursor')

    claims = [x for x in ast.walk(submit) if isinstance(x, ast.Expr)
              and isinstance(x.value, ast.Call)
              and isinstance(x.value.func, ast.Attribute)
              and isinstance(x.value.func.value, ast.Name)
              and x.value.func.value.id == 'conn' and x.value.func.attr == 'execute'
              and isinstance(call_arg_text(x), str)
              and "UPDATE jobs SET status='http_inflight'" in call_arg_text(x)
              and 'attempts=attempts+1' in call_arg_text(x)]
    claim = exact_one(claims, 'atomic claim statement')
    ind = indentation(lines, claim)
    insert_after(claim, (
        f"{ind}if retry_pacer is not None:\n"
        f"{ind}    retry_pacer.record(kind, now_mono)\n"
    ), 'retry pacing record')

    progress_logs = [x for x in ast.walk(runner) if isinstance(x, ast.Expr)
                     and isinstance(x.value, ast.Call) and call_name(x.value, 'log')
                     and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                             and 'HTTP attempted=' in n.value for n in ast.walk(x.value))]
    progress = exact_one(progress_logs, 'crawler periodic progress log')
    ind = indentation(lines, progress)
    insert_after(progress, (
        f"{ind}if retry_pacer is not None:\n"
        f"{ind}    log(f'auto_retry_dispatched={{retry_pacer.retries_dispatched}}')\n"
    ), 'retry metric')

    # Refuse edits on overlapping source ranges, and preserve all other lines verbatim.
    changes.sort(key=lambda e: (e[0], e[1]))
    for (_, e0, _), (s1, e1, _) in zip(changes, changes[1:]):
        if s1 <= e0:
            raise ValueError(f'overlapping edit ranges ({e0}, {s1})')
    output = []
    cursor_line = 1
    for start, end, content in changes:
        output.extend(lines[cursor_line - 1:start - 1])
        output.append(content)
        cursor_line = end + 1
    output.extend(lines[cursor_line - 1:])
    updated = ''.join(output)
    ast.parse(updated, filename='multi/crawler_multi.py')
    # Verify critical symbols were inserted in the resulting program.
    assert all(marker in updated for marker in existing_markers)
    return 'ready', updated


def git(*args):
    return subprocess.run(['git', '-C', str(BASE), *args],
                          text=True, capture_output=True, check=False)


def preflight():
    revision = git('rev-parse', '--verify', 'HEAD')
    if revision.returncode or not revision.stdout.startswith(EXPECTED_COMMIT):
        raise ValueError(f'expecting commit {EXPECTED_COMMIT}; got {revision.stdout.strip()[:12]}')
    if TARGET.is_symlink() or not TARGET.is_file():
        raise ValueError('crawler_multi.py must be a regular file, not a symlink')
    for args in [('diff', '--quiet', '--', 'multi/crawler_multi.py'),
                 ('diff', '--cached', '--quiet', '--', 'multi/crawler_multi.py')]:
        result = git(*args)
        if result.returncode != 0:
            raise ValueError('crawler_multi.py has local modifications; refusing to overwrite')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        preflight()
        original = TARGET.read_bytes()
        if b'\r\n' in original and b'\n' in original.replace(b'\r\n', b''):
            raise ValueError('mixed line endings in crawler_multi.py')
        eol = b'\r\n' if b'\r\n' in original else b'\n'
        source = original.decode('utf-8').replace('\r\n', '\n')
        status, new_source = upgrade_text(source)
        if status == 'already-upgraded':
            print('OK: crawler_multi.py is already upgraded, no changes')
            return 0
        if args.check:
            print('OK: Python AST checks passed; targeted changes valid (preview only)')
            return 0
        target_bytes = new_source.encode('utf-8').replace(b'\n', eol)
        with tempfile.NamedTemporaryFile(mode='wb', dir=str(TARGET.parent),
                                         prefix='.bima-retry-v41-', delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                tmp.write(target_bytes)
                tmp.flush()
                os.fsync(tmp.fileno())
                os.chmod(temporary, stat.S_IMODE(TARGET.stat().st_mode))
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            # Recheck for unexpected concurrent local edits before replacement.
            if TARGET.read_bytes() != original:
                raise ValueError('crawler_multi.py changed during upgrade; aborting')
            os.replace(temporary, TARGET)
        finally:
            temporary.unlink(missing_ok=True)
        print('OK: crawler_multi.py upgraded; run unit tests and inspect git diff')
        return 0
    except (ValueError, OSError, SyntaxError, UnicodeError) as exc:
        print(f'ERROR: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
