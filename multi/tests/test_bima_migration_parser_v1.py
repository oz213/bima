"""Verify legacy credentials are parsed safely with no shell execution."""
import tempfile
from pathlib import Path
import unittest
from tools.migrate_bima_single_config import parse_environment_file


class EnvMigrationTests(unittest.TestCase):
    def test_parses_simple_and_quoted_environment(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'env'
            p.write_text('''# example
TELEGRAM_BOT_TOKEN=abc:123
export BIMA_ANALYST_ENABLED=1
BIMA_MODEL="some model with spaces"
BRAVE_SEARCH_API_KEY=abc#def
''')
            parsed = parse_environment_file(p)
            self.assertEqual(parsed['TELEGRAM_BOT_TOKEN'], 'abc:123')
            self.assertEqual(parsed['BIMA_MODEL'], 'some model with spaces')
            self.assertEqual(parsed['BRAVE_SEARCH_API_KEY'], 'abc#def')

    def test_never_executes_shell_statement(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'env'
            p.write_text('DANGEROUS=$(touch /tmp/bima-must-not-execute)\n')
            with self.assertRaises(ValueError):
                parse_environment_file(p)

    def test_unsupported_line_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'env'
            p.write_text('source /another/file\n')
            with self.assertRaises(ValueError):
                parse_environment_file(p)


if __name__ == '__main__':
    unittest.main()
