#!/usr/bin/env python3
"""Adjust BIMA's long-lived crawler queue to process requeued old job IDs.

This tool deliberately does not overwrite crawler_multi.py with a different
version. It inspects the real file, changes one recognized assignment inside
submit_one(), and adds one import. Preview (--check) is the default.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import stat
import tempfile

IMPORT = 'from routine_queue import find_next_pending_job'
CALL = 'find_next_pending_job'


def _join_literal_strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _join_literal_strings(node.left)
        right = _join_literal_strings(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _has_old_pending_sql(value):
    if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.Or):
        return False
    if len(value.values) != 2 or not isinstance(value.values[0], ast.Name):
        return False
    if value.values[0].id != 'priority':
        return False
    fetch = value.values[1]
    if not (isinstance(fetch, ast.Call) and isinstance(fetch.func, ast.Attribute)
            and fetch.func.attr == 'fetchone' and isinstance(fetch.func.value, ast.Call)):
        return False
    execute = fetch.func.value
    if not (isinstance(execute.func, ast.Attribute) and execute.func.attr == 'execute'
            and isinstance(execute.func.value, ast.Name) and execute.func.value.id == 'conn'
            and len(execute.args) >= 2):
        return False
    sql = _join_literal_strings(execute.args[0])
    if not sql:
        return False
    checks = (r'\bselect\s+id\s*,\s*url\s+from\s+jobs\b',
              r'\bstatus\s*=\s*[\'\"]pending[\'\"]',
              r'\bid\s*>\s*\?', r'\border\s+by\s+id\b', r'\blimit\s+1\b')
    if not all(re.search(pattern, sql, re.IGNORECASE) for pattern in checks):
        return False
    return any(isinstance(child, ast.Name) and child.id == 'last_id'
               for child in ast.walk(execute.args[1]))


def plan_change(contents):
    tree = ast.parse(contents, filename='multi/crawler_multi.py')
    funcs = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == 'submit_one']
    if len(funcs) != 1:
        raise ValueError(f'expected exactly one submit_one(), found {len(funcs)}; no change')
    row_assignments = [node for node in ast.walk(funcs[0])
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'row' for t in node.targets)]
    old = [node for node in row_assignments if _has_old_pending_sql(node.value)]
    updated = [node for node in row_assignments
               if isinstance(node.value, ast.BoolOp)
               and any(isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                       and v.func.id == CALL for v in node.value.values)]
    if len(old) == 0 and len(updated) == 1:
        state = 'already upgraded'
    elif len(old) == 1 and len(updated) == 0:
        state = 'old queue query found'
    else:
        raise ValueError('queue query differs from supported patterns; no change. '
                         'Inspect submit_one() before attempting a manual edit.')

    lines = contents.splitlines(keepends=True)
    if state == 'old queue query found':
        node = old[0]
        start, end = node.lineno - 1, node.end_lineno
        indent = re.match(r'[ \t]*', lines[start]).group(0)
        lines[start:end] = [indent + 'row = priority or find_next_pending_job(conn, last_id, wrap=interactive)\n']
    result = ''.join(lines)
    if IMPORT not in result:
        # Require a stable anchor, rather than inserting before __future__ imports.
        lines = result.splitlines(keepends=True)
        anchor = [i for i, line in enumerate(lines)
                  if line.startswith('from telegram_checks import ')]
        if len(anchor) != 1:
            raise ValueError('could not safely locate telegram_checks import; no change')
        lines.insert(anchor[0] + 1, IMPORT + '\n')
        result = ''.join(lines)
    compile(result, 'multi/crawler_multi.py', 'exec')
    return result, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=Path('multi/crawler_multi.py'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='read-only verification (default)')
    mode.add_argument('--apply', action='store_true', help='write the minimal compatible change')
    args = parser.parse_args()

    path = args.path
    if not path.is_file():
        parser.error(f'not found: {path}')
    if not (path.parent / 'routine_queue.py').is_file():
        parser.error('multi/routine_queue.py is missing; apply safe patch first')
    original = path.read_bytes()
    if b'\x00' in original:
        parser.error('unexpected binary file')
    line_ending = b'\r\n' if b'\r\n' in original else b'\n'
    contents = original.decode('utf-8')
    if line_ending == b'\r\n':
        contents = contents.replace('\r\n', '\n')
    proposed, state = plan_change(contents)
    changed = proposed != contents
    print(f'{path}: {state}; changed={changed}; syntax=OK')
    if not args.apply:
        print('PREVIEW ONLY. To write: python3 ~/Downloads/bima-crawler-queue-compat.py --apply')
        return
    if not changed:
        print('Already up to date; no file changed.')
        return

    # Atomic write in same directory. Preserve original permissions; no backup
    # is necessary because Git retains original content, and no data files touched.
    mode_bits = stat.S_IMODE(path.stat().st_mode)
    contents_out = proposed.replace('\n', '\r\n').encode('utf-8') if line_ending == b'\r\n' else proposed.encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.bima-queue-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(contents_out)
        os.chmod(tmp, mode_bits)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print('Applied queue cursor compatibility fix. No other files modified.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, SyntaxError) as exc:
        raise SystemExit(f'ERROR: {exc}') from None
