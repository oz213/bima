import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import crawler_multi as cm
import telegram_monitor as bot


class TelegramChecks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.sqlite'
        self.db = cm.connect_db(self.path)
        self.addCleanup(self.db.close)
        env = patch.dict(os.environ, TELEGRAM_CHECKS_ENABLED='1', TELEGRAM_CHECK_USER_IDS='42')
        env.start()
        self.addCleanup(env.stop)

    def command(self, command, update=1, sender='42', chat='-7'):
        return bot.handle_command(command, chat, {'-7', '123'}, self.path, update, sender)

    def test_browser_priority_and_fifo(self):
        tasks = cm.TelegramBrowserQueue(5)
        for number, requested in enumerate([False, True, False, True]):
            tasks.put({'id': number, 'telegram_requested': requested})
        tasks.put(None)
        self.assertEqual([tasks.get()['id'] for _ in range(4)], [1, 3, 0, 2])
        self.assertIsNone(tasks.get())

    def test_access_validation_and_explicit_command(self):
        for cmd, sender in [('/cek https://example.com', '42'), ('/cek x.id', '99'),
                            ('/cek', '42'), ('https://x.id', '42')]:
            self.command(cmd, sender=sender)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_result_delivery_and_duplicate_updates(self):
        cmd = '/cek https://contoh.id/a?test=private'
        first = self.command(cmd)
        self.assertEqual(first, self.command(cmd))
        self.command(cmd, update=2, chat='123')
        job, url = self.db.execute('SELECT id,url FROM jobs').fetchone()
        self.assertEqual(url, cmd.split()[1])
        cm.set_status(self.db, job, 'retry_http', reason='timeout')
        with patch.object(bot, 'send_message') as send:
            bot.deliver_checks('unused', self.path, {'-7','123'})
            bot.deliver_checks('unused', self.path, {'-7','123'})
            self.assertEqual([c.args[1] for c in send.call_args_list], ['-7','123'])
            self.assertIn('Belum dapat disimpulkan', send.call_args.args[2])
            self.assertNotIn('private', send.call_args.args[2])
        self.command(cmd, update=3)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'pending')
        cm.set_status(self.db, job, 'alerted', reason='bima_alert_sent')
        self.command(cmd, update=4)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'alerted')

    def test_worker_takes_priority_before_routine(self):
        args = cm.arg_parser().parse_args(['--telegram-checks','--limit','0','--db',str(self.path)])
        args.input = str(self.path.parent/'urls.json')
        Path(args.input).write_text('["routine.id"]')
        cm.import_input_urls(self.db, args.input)
        self.command('/cek contoh.id/page')
        processed = []
        def fetch(engine, url, options):
            processed.append(url)
            return {'reason':'non_html_content','ok':False}
        args.http_workers = 1
        self.addCleanup(cm._STOP.clear)
        with patch.object(cm,'load_engine'), patch.object(cm,'fetch_http',side_effect=fetch), patch.object(cm._STOP,'wait',side_effect=lambda _: cm._STOP.set()):
            cm.run(args)
        self.assertEqual(processed, ['https://contoh.id/page','https://routine.id'])
        self.assertEqual(self.db.execute('SELECT result FROM telegram_checks').fetchone()[0], 'inconclusive')
