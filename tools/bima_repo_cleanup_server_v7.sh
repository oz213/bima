#!/usr/bin/env bash
# One-time safe deployment of repo cleanup commit that UNTRACKS the four
# legacy models/indobert-judol files. Executed as root ON registrar-sandbox.
# Never deletes model data; temporarily moves only the models symlink.
set -Eeuo pipefail

REPO=/home/fauzi/crawler-rebuild
MODEL_NFS=/mnt/registrar-sandbox/bima/models/legacy
UNIT_CRAWLER=pandi-crawler-multi.service
UNIT_BOT=pandi-telegram-monitor.service

fail() { echo "STOP: $*" >&2; exit 1; }
git_fauzi() { runuser -u fauzi -- git -C "$REPO" "$@"; }

[ "$(id -u)" = 0 ] || fail "jalankan sebagai root"
[ -d "$REPO/.git" ] && [ ! -L "$REPO/.git" ] || fail "repository .git tidak valid"
[ "$(git_fauzi branch --show-current)" = main ] || fail "server harus pada branch main"
[ -L "$REPO/models" ] || fail "models bukan symlink; jangan lanjut otomatis"
[ "$(readlink -f "$REPO/models")" = "$MODEL_NFS" ] || fail "target models berbeda dari NFS yang diharapkan"
[ "$(findmnt -rn -T "$MODEL_NFS" -o FSTYPE)" = nfs4 ] || fail "mount NFS model tidak tersedia"
[ -d "$MODEL_NFS" ] || fail "direktori model NFS tidak tersedia"
[ -f "$REPO/.gitignore" ] || fail ".gitignore tidak ditemukan"

systemctl is-active --quiet "$UNIT_CRAWLER" || fail "crawler tidak aktif"
systemctl is-active --quiet "$UNIT_BOT" || fail "Telegram Monitor tidak aktif"
git_fauzi diff --cached --quiet || fail "ada perubahan staged di server"
git_fauzi diff --quiet -- .gitignore || fail ".gitignore di server dimodifikasi lokal"

# Fetch and validate before stopping a running crawler.
echo '=== FETCH / PREFLIGHT (crawler tetap aktif) ==='
git_fauzi fetch origin main
git_fauzi merge-base --is-ancestor HEAD origin/main || fail "branch diverged, bukan fast-forward"
[ "$(git_fauzi rev-parse HEAD)" != "$(git_fauzi rev-parse origin/main)" ] || fail "server sudah mengikuti origin/main"

EXPECTED="$(printf '%s\n' \
  '.gitignore' \
  'docs/OPERASIONAL_NFS_BIMA.md' \
  'tools/bima_repo_cleanup_server_v7.sh' \
  'models/indobert-judol/config.json' \
  'models/indobert-judol/special_tokens_map.json' \
  'models/indobert-judol/tokenizer_config.json' \
  'models/indobert-judol/vocab.txt' | LC_ALL=C sort)"
ACTUAL="$(git_fauzi diff --name-only HEAD origin/main | LC_ALL=C sort)"
[ "$ACTUAL" = "$EXPECTED" ] || {
  echo 'Remote berubah di luar 7 path yang diharapkan:' >&2
  git_fauzi diff --name-status HEAD origin/main >&2
  fail "review perubahan sebelum deploy; services belum dihentikan"
}

[ -z "$(git_fauzi ls-tree -r --name-only origin/main -- models)" ] || fail "model masih tracked pada remote"
[ "$(git_fauzi ls-files -- models | wc -l | tr -d ' ')" = 4 ] || fail "jumlah model tracked sebelum cleanup bukan 4"
[ ! -e "$REPO/docs/OPERASIONAL_NFS_BIMA.md" ] || fail "docs cleanup sudah ada lokal"

echo '=== PREVIEW CHANGES ==='
git_fauzi diff --name-status HEAD origin/main
echo '=== STOP SERVICES ==='

HOLD="$(mktemp -d /home/fauzi/.bima-repo-cleanup-v7.XXXXXXXX)"
LINK_HOLD="$HOLD/models-link"
RESTORED=0
STOPPED=0
DEPLOY_VALIDATED=0

cleanup() {
    local rc=$?
    trap - EXIT
    if [ "$STOPPED" = 1 ]; then
        # Preserve all file content if the Git operation created a local
        # models directory. This is NOT the NFS target directory.
        if [ -L "$LINK_HOLD" ]; then
            if [ -e "$REPO/models" ] || [ -L "$REPO/models" ]; then
                if [ -d "$REPO/models" ] && [ ! -L "$REPO/models" ]; then
                    if ! mv -T "$REPO/models" "$HOLD/models-metadata-local"; then
                        echo 'ERROR: cannot move temporary local model metadata' >&2
                        rc=1
                    fi
                else
                    echo 'ERROR: unexpected models path; symlink preserved in hold' >&2
                    rc=1
                fi
            fi
            if [ ! -e "$REPO/models" ] && [ ! -L "$REPO/models" ]; then
                if mv -T "$LINK_HOLD" "$REPO/models"; then
                    RESTORED=1
                else
                    echo "ERROR: restore symlink manually from $LINK_HOLD" >&2
                    rc=1
                fi
            fi
        elif [ -L "$REPO/models" ]; then
            RESTORED=1
        fi
        if [ "$RESTORED" = 1 ] && [ "$(readlink -f "$REPO/models")" != "$MODEL_NFS" ]; then
            echo 'ERROR: model symlink no longer resolves to original NFS target' >&2
            rc=1
        fi
        systemctl start "$UNIT_CRAWLER" || rc=1
        systemctl start "$UNIT_BOT" || rc=1
        if ! systemctl is-active --quiet "$UNIT_CRAWLER" || \
           ! systemctl is-active --quiet "$UNIT_BOT"; then
            echo 'ERROR: service check failed; inspect systemctl status immediately' >&2
            rc=1
        fi
    fi
    if [ -d "$HOLD" ]; then
        rmdir "$HOLD" 2>/dev/null || \
            echo "INFO: retained local metadata / diagnostic at $HOLD"
    fi
    [ "$RESTORED" = 1 ] || {
      [ "$STOPPED" = 0 ] || echo 'WARNING: symlink restoration not verified' >&2
    }
    if [ "$rc" -eq 0 ] && [ "$RESTORED" = 1 ] && [ "$DEPLOY_VALIDATED" = 1 ]; then
        echo '=== POST-DEPLOY STATUS ==='
        git_fauzi status --short || rc=1
        echo '=== ACTIVE SERVICES ==='
        systemctl is-active "$UNIT_CRAWLER" "$UNIT_BOT" || rc=1
        echo '=== MODELS NFS SYMLINK ==='
        readlink -f "$REPO/models" || rc=1
        [ "$rc" -eq 0 ] && echo '=== CLEANUP V7 DEPLOY SUCCESS ==='
    fi
    exit "$rc"
}
trap cleanup EXIT

STOPPED=1
systemctl stop "$UNIT_BOT" "$UNIT_CRAWLER"

# Move only the symlink inode. This does NOT move NFS model data.
mv -T "$REPO/models" "$LINK_HOLD"
[ -L "$LINK_HOLD" ] || fail "model symlink not preserved"

# Restore the four small tracked files from the *old* Git commit into the
# local worktree, so the upcoming deletion commit does not see local D files.
# This temporary directory is removed by Git during fast-forward.
git_fauzi restore --worktree -- models/indobert-judol

echo '=== FAST-FORWARD PULL ==='
git_fauzi pull --ff-only origin main
[ -z "$(git_fauzi ls-files -- models)" ] || fail "model metadata still tracked after pull"
[ -f "$REPO/docs/OPERASIONAL_NFS_BIMA.md" ] || fail "docs cleanup not present"
[ -f "$REPO/tools/bima_repo_cleanup_server_v7.sh" ] || fail "server helper not present"

# Exit trap restores link and services, including on error.
echo '=== SOURCE STATUS AFTER PULL ==='
git_fauzi log -1 --oneline
git_fauzi status --short

DEPLOY_VALIDATED=1
echo '=== FINISH / RESTORE SYMLINK & SERVICES ==='
