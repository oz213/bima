#!/usr/bin/env bash
# Apply BIMA Phase 1A (if absent) + 1B-1D patches in the Mac source checkout.
# Does not commit/push automatically and never touches NFS or production.
set -Eeuo pipefail
REPO="${1:-/Applications/XAMPP/xamppfiles/htdocs/BIMA/bima}"
PATCH_DIR="${2:-$HOME/Downloads}"
cd "$REPO"
test "$(git branch --show-current)" = main || { echo 'STOP: expected branch main'; exit 1; }
if ! test -f tools/bima_production_health_v8.py; then
  echo '=== APPLY PHASE 1A V8 ==='
  git apply --check "$PATCH_DIR/bima-phase1a-health-v8.patch"
  git apply "$PATCH_DIR/bima-phase1a-health-v8.patch"
fi
if test -e tools/bima_resilience_v9.py; then
  echo 'STOP: BIMA v9 file exists; inspect current working tree (no overwrite).'
  exit 1
fi
echo '=== APPLY PHASE 1B-1D V9 ==='
git apply --check "$PATCH_DIR/bima-phase1bcd-v9.patch"
git apply "$PATCH_DIR/bima-phase1bcd-v9.patch"
echo '=== TESTS ==='
python3 -m unittest discover -s tests -p 'test_bima_production_health_v8.py' -v
python3 -m unittest discover -s tests -p 'test_bima_resilience*_v9.py' -v
python3 -m compileall -q tools/bima_production_health_v8.py tools/bima_resilience_v9.py

git add -- \
  tools/bima_production_health_v8.py \
  tests/test_bima_production_health_v8.py \
  deploy/pandi-bima-health.service deploy/pandi-bima-health.timer \
  docs/OPERASIONAL_HEALTH_BIMA_V8.md \
  tools/bima_resilience_v9.py \
  tools/bima_phase1_apply_mac_v9.sh tools/bima_phase1_run_server_v9.sh \
  tests/test_bima_resilience_v9.py tests/test_bima_resilience_units_v9.py \
  deploy/pandi-bima-backup.service deploy/pandi-bima-backup.timer \
  deploy/pandi-bima-rehearsal.service deploy/pandi-bima-rehearsal.timer \
  deploy/pandi-bima-readiness.service deploy/pandi-bima-readiness.timer \
  docs/OPERASIONAL_BACKUP_RECOVERY_BIMA_V9.md

git diff --cached --check
echo '=== STAGED FILES (review before commit) ==='
git diff --cached --name-status
echo '=== READY: run git commit, then git push origin main ==='
