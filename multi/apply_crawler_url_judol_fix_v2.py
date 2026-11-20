#!/usr/bin/env python3
"""Safe, whitespace-tolerant repair of the original crawler_url.py false negative.

Does not import or execute crawler_url.py. Checks AST structure before changing
anything. Default is read-only; --apply creates backup and atomically replaces.
Use only after stopping both legacy and multi-worker crawler processes.
"""
import argparse
import ast
import difflib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone

HELPER = '''def has_strong_operator_gambling_promo(page_text, page_title=""):
    """Check multiple independent signals for explicit operator-style promotion."""
    title = normalize_text_for_match(page_title)
    body = normalize_text_for_match(page_text)
    title_promo = (re.search(r"\\bspin\\b", title) is not None
                   and re.search(r"\\bmenang\\b", title) is not None)
    account_calls = (re.search(r"\\blogin\\b", body) is not None
                     and re.search(r"\\bdaftar\\b", body) is not None)
    terms = ("deposit", "jackpot", "rtp", "link alternatif")
    term_count = sum(bool(has_context_terms(body, [term])) for term in terms)
    return bool(title_promo and account_calls and term_count >= 3)


'''

RULE = '''{indent}# Bukti promosi operator judi yang kuat mengalahkan konteks produk umum.
{indent}# Hanya berlaku jika IndoBERT sangat yakin dan ada beberapa sinyal eksplisit.
{indent}if (ml_source == "indobert"
{indent}        and ml_pred == "gambling"
{indent}        and ml_conf >= strong_threshold
{indent}        and has_strong_operator_gambling_promo(page_text, page_title)):
{indent}    return "high_risk", "strong_operator_gambling_promotion", text_hits

'''

SAFE_TEST = ('safe_product_context and not real_gambling_context '
             'and not contextual_phrase_hit')


def fail(message):
    raise ValueError(message)


def locate_def(tree, name):
    matches = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(matches) != 1:
        fail(f'Expected exactly one top-level function {name}(); found {len(matches)}')
    return matches[0]


def make_patch(text):
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        fail(f'Source is not valid Python before modification: {exc}')
    original_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    required = {'normalize_text_for_match', 'has_context_terms', 'classify_risk',
                'classify_text_content'}
    if not required <= original_funcs:
        fail('Original classifier functions not found: ' + ', '.join(sorted(required - original_funcs)))
    old = locate_def(tree, 'classify_risk')
    caller = locate_def(tree, 'classify_text_content')
    if old.decorator_list:
        fail('classify_risk() has a decorator; refusing automatic rewrite')
    if 'has_strong_operator_gambling_promo' in original_funcs:
        if 'strong_operator_gambling_promotion' in text and any(a.arg == 'page_title' for c in ast.walk(caller) if isinstance(c, ast.Call)
                                             and isinstance(c.func, ast.Name) and c.func.id == 'classify_risk'
                                             for a in c.keywords):
            return None, 'ALREADY_APPLIED'
        fail('A similar helper exists, but expected routing is missing. Refusing partial overwrite.')
    if any(a.arg == 'page_title' for a in old.args.kwonlyargs):
        fail('Unexpected classify_risk signature; inspect manually')
    if 'page_title' in [arg.arg for arg in old.args.args]:
        fail('classify_risk already has page_title parameter but helper is absent; inspect manually')
    if 'ml_source' not in [arg.arg for arg in old.args.args]:
        fail('classify_risk signature differs from expected source')
    guards = []
    for node in ast.walk(old):
        if not isinstance(node, ast.If):
            continue
        if ast.dump(node.test, include_attributes=False) != ast.dump(ast.parse(SAFE_TEST, mode='eval').body, include_attributes=False):
            continue
        if not any(isinstance(n, ast.Constant) and n.value == 'safe_product_or_industrial_context'
                   for n in ast.walk(node)):
            continue
        guards.append(node)
    if len(guards) != 1:
        fail('Expected one original safe-product rule in classify_risk(); found ' + str(len(guards)))
    guard = guards[0]
    calls = [n for n in ast.walk(caller) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'classify_risk']
    if len(calls) != 1:
        fail('Expected one classify_risk() call in classify_text_content(); found ' + str(len(calls)))
    call = calls[0]
    if any(k.arg == 'page_title' for k in call.keywords):
        fail('classify_risk caller already passes page_title; inspect manually')
    ml_source_args = [k for k in call.keywords if k.arg == 'ml_source']
    if len(ml_source_args) != 1:
        fail('Could not identify unique ml_source= keyword at classify_risk call')
    if not hasattr(ml_source_args[0], 'end_lineno'):
        fail('Python AST location data unavailable')
    if call.end_lineno is None:
        fail('AST call end position unavailable')
    source_lines = text.splitlines(keepends=True)
    line_ending = '\r\n' if '\r\n' in text else '\n'
    replace = {}
    inserts = {}

    # Require a single-line signature, but tolerate whitespace and quote variations.
    signature_index = old.lineno - 1
    signature_line = source_lines[signature_index]
    if old.body[0].lineno <= old.lineno:
        fail('Unexpected function body line number')
    signature_re = re.compile(r'^(\s*def\s+classify_risk\s*\([^()\n]*)(\)\s*:\s*(?:#.*)?)(\r?\n)?$')
    m = signature_re.fullmatch(signature_line)
    if m is None:
        fail('classify_risk signature is not a single-line definition; need a tailored patch')
    replace[signature_index] = m.group(1) + ', page_title=""' + m.group(2) + (m.group(3) or '')

    guard_index = guard.lineno - 1
    guard_line = source_lines[guard_index]
    rule_indent = re.match(r'^[ \t]*', guard_line).group(0)
    if guard_line.lstrip().startswith('if ') is False:
        fail('safe-product rule is not at expected line')
    inserts[guard_index] = RULE.format(indent=rule_indent).replace('\n', line_ending)

    ml_keyword = ml_source_args[0]
    ml_idx = ml_keyword.end_lineno - 1
    ml_line = source_lines[ml_idx]
    indent = re.match(r'^[ \t]*', ml_line).group(0)
    if re.match(r'^\s*ml_source\s*=\s*ml_source\s*,\s*(?:#.*)?\r?\n?$', ml_line) is None:
        fail('ml_source argument is not an independent trailing-comma line')
    inserts[ml_idx + 1] = indent + 'page_title=page_title,' + line_ending

    helper_index = old.lineno - 1
    inserts[helper_index] = HELPER.replace('\n', line_ending)
    if len(inserts) != 3:
        fail('Unexpected insertion line collision')
    for index, new_line in replace.items():
        source_lines[index] = new_line
    for index in sorted(inserts, reverse=True):
        source_lines.insert(index, inserts[index])
    result = ''.join(source_lines)
    try:
        parsed = ast.parse(result)
        compile(parsed, '<patched crawler_url.py>', 'exec')
    except (SyntaxError, ValueError) as exc:
        fail(f'Patched file did not compile: {exc}')
    test_func = locate_def(parsed, 'classify_risk')
    if 'page_title' not in [a.arg for a in test_func.args.args]:
        fail('Post-check failed: classify_risk lacks page_title')
    if 'strong_operator_gambling_promotion' not in result:
        fail('Post-check failed: classifier reason missing')
    return result, 'READY'


def main():
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument('--file', required=True, help='Actual crawler_url.py path')
    arg.add_argument('--apply', action='store_true', help='Make backup then apply; default is read-only')
    opts = arg.parse_args()
    src = Path(opts.file)
    if not src.is_file():
        arg.error(f'Missing source file: {src}')
    current = src.read_text(encoding='utf-8')
    try:
        rewritten, status = make_patch(current)
    except ValueError as exc:
        arg.error(str(exc))
    if status == 'ALREADY_APPLIED':
        print('Already applied; no changes necessary.')
        return
    changes = list(difflib.unified_diff(current.splitlines(keepends=True),
                                        rewritten.splitlines(keepends=True),
                                        fromfile=str(src), tofile=str(src), n=2))
    print('Validated intended edits; original remains untouched in check mode.')
    print(''.join(changes[:95]), end='')
    if len(changes) > 95:
        print(f'... ({len(changes) - 95} additional diff lines)')
    if not opts.apply:
        print('\nCHECK OK. Stop crawler processes and rerun with --apply to save.')
        return
    original_stat = src.stat()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = src.with_name(f'{src.name}.backup_judol_fix_{stamp}_{os.getpid()}')
    shutil.copy2(src, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{src.name}.tmp.', dir=src.parent)
    tmp = Path(temp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
            out.write(rewritten)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(src, tmp)
        os.chmod(tmp, stat.S_IMODE(original_stat.st_mode))
        if hasattr(os, 'chown'):
            os.chown(tmp, original_stat.st_uid, original_stat.st_gid)
        # Avoid overwriting an unexpectedly changed source during the check.
        if src.read_text(encoding='utf-8') != current:
            fail('Source changed during patch operation; stopped before replacement')
        os.replace(tmp, src)
        print(f'\nAPPLIED. Backup: {backup}')
        print(f'Compile check: python3 -m py_compile {src}')
    finally:
        if tmp.exists():
            tmp.unlink()

if __name__ == '__main__':
    main()
