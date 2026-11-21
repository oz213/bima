# BIMA v4.1 - Mac upgrade from commit 3bdecf5

This kit fixes the previous Git patch failing at `multi/crawler_multi.py:944`.
It creates four new Python files and this README with `git apply`, then uses
`tools/upgrade_bima_retry_v41.py` to update `crawler_multi.py` using seven
validated, unique source anchors. It does not modify Registry `urls.json`,
IndoBERT, config.json, production database, or systemd files.

## On Mac

Use only `bima-v4.1-compatible.patch`. Do not use the earlier v4.1 patch.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git rev-parse --short HEAD
# Ensure clean status and HEAD 3bdecf5.
git apply --check ~/Downloads/bima-v4.1-compatible.patch && git apply ~/Downloads/bima-v4.1-compatible.patch
python3 -B tools/upgrade_bima_retry_v41.py --check && python3 -B tools/upgrade_bima_retry_v41.py --apply
python3 -B -m unittest discover -s multi/tests -p 'test_network_retry_*.py' -v
```

Optionally run all v4 suites too. Inspect `git diff --check`, `git diff --stat`,
`git status --short`, and review the new `multi/crawler_multi.py` diff.

```bash
git add -- README_NETWORK_RETRY_V41.md tools/upgrade_bima_retry_v41.py \
    multi/crawler_multi.py multi/network_retry_queue.py \
    multi/tests/test_network_retry_queue.py multi/tests/test_network_retry_upgrade_v41.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: add bounded automatic network retries to BIMA"
git push origin main
```

## In server (after push)

Fetch only after checking local state, then run the read-only tests first.
Neither `git pull` nor this patch activates automatic retry. Existing
`--retry-network` should NOT be used together with `--auto-network-retry`.
Production activation requires planned systemd restart and explicit opt-in:
`--auto-network-retry --auto-retry-every 5 --auto-retry-max-per-minute 10`.

## Safety

- Retry selection is only for status `retry_http` with known DNS/timeout error,
  with non-null due time and attempts below the configured maximum.
- At most one retry per five routine dispatches, up to ten per rolling minute.
- Telegram priority is preserved; no bulk status mutation or new database schema.
- The updater refuses unknown, duplicated, and partially upgraded source anchors.
- No automated alert transmission is triggered by installing this patch.
