"""Static safety checks for BIMA backup/recovery systemd units."""
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'


class UnitPolicies(unittest.TestCase):
    def test_services_never_restart_or_modify_crawler(self):
        for name in ('backup', 'rehearsal', 'readiness'):
            text = (DEPLOY / f'pandi-bima-{name}.service').read_text()
            self.assertIn('User=fauzi', text)
            self.assertIn('ProtectSystem=strict', text)
            self.assertIn('NoNewPrivileges=true', text)
            self.assertNotIn('systemctl restart', text)
            self.assertNotIn('ExecStartPre=systemctl stop', text)
            self.assertNotIn('BIMA_HEALTH_NOTIFY=1', text)

    def test_only_backup_has_nfs_write_permission(self):
        backup = (DEPLOY / 'pandi-bima-backup.service').read_text()
        self.assertIn('ReadWritePaths=/mnt/registrar-sandbox/bima/backups/verified-sqlite', backup)
        for name in ('rehearsal', 'readiness'):
            text = (DEPLOY / f'pandi-bima-{name}.service').read_text()
            self.assertNotIn('ReadWritePaths=/mnt/registrar-sandbox', text)

    def test_timers_not_automatically_catch_up_after_outage(self):
        for name in ('backup', 'rehearsal', 'readiness'):
            text = (DEPLOY / f'pandi-bima-{name}.timer').read_text()
            self.assertIn('Persistent=false', text)
            self.assertIn('Unit=pandi-bima-', text)


if __name__ == '__main__':
    unittest.main()
