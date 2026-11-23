#!/usr/bin/env bash
# One-time Phase 1B-1D rollout after git pull, non-destructive to crawler.
# Run as root. Optional --enable-timers only after manual baseline succeeds.
set -Eeuo pipefail
REPO=/home/fauzi/crawler-rebuild
PY="$REPO/venv/bin/python3"
TOOL="$REPO/tools/bima_resilience_v9.py"
NFS=/mnt/registrar-sandbox
BACKUPS="$NFS/bima/backups/verified-sqlite"

[ "$(id -u)" -eq 0 ] || { echo 'STOP: run as root'; exit 1; }
[ -f "$TOOL" ] || { echo 'STOP: v9 missing, git pull first'; exit 1; }
[ -f "$REPO/tools/bima_production_health_v8.py" ] || {
  echo 'STOP: v8 health checker missing; apply combined v8+v9 patch on Mac first'; exit 1;
}
[ "$(findmnt -rn -T "$NFS" -o FSTYPE)" = nfs4 ] || {
  echo 'STOP: NFS not mounted as nfs4'; exit 1;
}
systemctl is-active --quiet pandi-crawler-multi.service || {
  echo 'STOP: crawler not active'; exit 1;
}
systemctl is-active --quiet pandi-telegram-monitor.service || {
  echo 'STOP: Telegram monitor not active'; exit 1;
}

# Never change ownership of the whole repository or NFS mount.
runuser -u fauzi -- mkdir -p "$BACKUPS"
runuser -u fauzi -- chmod 700 "$BACKUPS"
install -d -o fauzi -g fauzi -m 0700 /var/lib/pandi-bima-health

echo '=== 1C STORAGE AUDIT (READ ONLY) ==='
runuser -u fauzi -- "$PY" -B "$TOOL" storage-audit

echo '=== 1B VERIFIED ONLINE SQLITE BACKUP ==='
runuser -u fauzi -- "$PY" -B "$TOOL" backup

echo '=== 1D FULL LOCAL RESTORE REHEARSAL ==='
runuser -u fauzi -- "$PY" -B "$TOOL" rehearse

echo '=== 1A HEALTH CHECK ==='
runuser -u fauzi -- "$PY" -B "$REPO/tools/bima_production_health_v8.py" --json

echo '=== 1D READINESS ==='
runuser -u fauzi -- "$PY" -B "$TOOL" readiness

if [ "${1:-}" = '--enable-timers' ]; then
  echo '=== INSTALL OPT-IN SCHEDULED MONITORS ==='
  for name in health backup rehearsal readiness; do
    install -o root -g root -m 0644 \
      "$REPO/deploy/pandi-bima-$name.service" /etc/systemd/system/
    install -o root -g root -m 0644 \
      "$REPO/deploy/pandi-bima-$name.timer" /etc/systemd/system/
  done
  systemctl daemon-reload
  systemd-analyze verify /etc/systemd/system/pandi-bima-{health,backup,rehearsal,readiness}.{service,timer}
  systemctl enable --now pandi-bima-health.timer pandi-bima-backup.timer \
    pandi-bima-rehearsal.timer pandi-bima-readiness.timer
  systemctl list-timers 'pandi-bima-*'
fi

echo '=== SERVICES (NO RESTART PERFORMED) ==='
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
echo '=== PHASE 1B-1D VALIDATED: NFS risk still requires future local DB cutover ==='
