"""Offline tests for BIMA unified private JSON startup behavior."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bima_private_config import apply_private_config, read_private_config


class UnifiedConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bima.json'
        self.data = {
            'ENDPOINT_URL': 'https://example.org/api',
            'X_API_KEY': 'fake-secret-value',
            '__bima_runtime__': {
                'version': 1,
                'roles': {
                    'crawler': {'BIMA_PREALERT_ENABLED': '1'},
                    'telegram': {'TELEGRAM_BOT_TOKEN': 'test-only-token',
                                 'BIMA_PREALERT_ENABLED': '1'},
                    'discovery': {'BIMA_DISCOVERY_INTERVAL_DAYS': '2'},
                },
            },
        }

    def write(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def test_reads_private_json(self):
        self.write()
        self.assertEqual(read_private_config(self.path)['X_API_KEY'], 'fake-secret-value')

    def test_rejects_world_readable_file(self):
        self.write()
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            read_private_config(self.path)

    def test_rejects_symlink(self):
        self.write()
        link = Path(self.temp.name) / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            read_private_config(link)

    def test_runtime_overrides_service_environment(self):
        self.write()
        with patch.dict(os.environ, {'BIMA_CONFIG_FILE': str(self.path),
                                    'BIMA_PREALERT_ENABLED': '0'}):
            self.assertTrue(apply_private_config('crawler'))
            self.assertEqual(os.environ['BIMA_PREALERT_ENABLED'], '1')

    def test_role_does_not_export_other_role_token(self):
        self.write()
        with patch.dict(os.environ, {'BIMA_CONFIG_FILE': str(self.path)}, clear=True):
            self.assertTrue(apply_private_config('crawler'))
            self.assertNotIn('TELEGRAM_BOT_TOKEN', os.environ)

    def test_legacy_fallback_without_unified_file(self):
        with patch.dict(os.environ, {'BIMA_CONFIG_FILE': str(self.path)}):
            self.assertFalse(apply_private_config('crawler'))

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            apply_private_config('invalid')

    def test_invalid_runtime_fails_closed(self):
        self.write()
        self.data['__bima_runtime__']['roles']['crawler'] = ['invalid']
        self.write()
        with patch.dict(os.environ, {'BIMA_CONFIG_FILE': str(self.path)}):
            with self.assertRaises(ValueError):
                apply_private_config('crawler')


if __name__ == '__main__':
    unittest.main()
