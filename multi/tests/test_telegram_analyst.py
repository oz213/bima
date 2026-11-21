"""Private analyst conversations with real worker threads and temporary SQLite."""
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import telegram_monitor as bot


class EvidenceAnswerer:
    """Use saved fixture rows; Events control only the slow answer boundary."""

    def __init__(self, blocked=False):
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, production_db, targets, question='', history=(), **kwargs):
        with self.lock:
            self.calls.append((tuple(targets), question, tuple(history)))
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError('fixture answer was not released')
        key = int(str(targets[0]).lstrip('#')) if str(targets[0]).lstrip('#').isdigit() else 1
        with sqlite3.connect(Path(production_db).as_uri() + '?mode=ro', uri=True) as conn:
            url = conn.execute('SELECT url FROM jobs WHERE id=?', (key,)).fetchone()[0]
        text = f'{url} [job#{key}] {question}'
        return {'text': text, 'context': {'targets': list(targets),
                'history': [*history, {'question': question, 'answer': text}][-2:]}}


class TelegramAnalystTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'crawler.sqlite3'
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,url TEXT,status TEXT,'
                         'attempts INTEGER,updated_at TEXT)')
            conn.executemany('INSERT INTO jobs VALUES(?,?,?,1,?)',
                             [(1, 'https://one.id/', 'complete', 't'),
                              (2, 'https://two.id/', 'inconclusive', 't')])
        self.allowed = {str(number) for number in range(42, 62)}
        environment = patch.dict(os.environ, {'BIMA_ANALYST_ENABLED': '1',
            'TELEGRAM_REVIEW_USER_IDS': ','.join(sorted(self.allowed))})
        environment.start()
        self.addCleanup(environment.stop)
        self.services = []
        self.answerers = []
        self.addCleanup(self.close_services)

    def close_services(self):
        for answerer in self.answerers:
            answerer.release.set()
        for service in self.services:
            service.close()

    def service(self, answerer=None, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('telegram_analyst'),
                             'The bounded Telegram analyst service is missing')
        module = importlib.import_module('telegram_analyst')
        answerer = answerer or EvidenceAnswerer()
        if isinstance(answerer, EvidenceAnswerer):
            self.answerers.append(answerer)
        service = module.AnalystService(self.path, answerer=answerer, **kwargs)
        self.services.append(service)
        return service, answerer

    def command(self, service, text, update=1, chat='42', sender=None, kind='private'):
        return bot.handle_command(text, chat, self.allowed, self.path, update,
                                  chat if sender is None else sender,
                                  analyst_service=service, chat_type=kind)

    def receive(self, service, count=1, allowed=None):
        messages = []
        deadline = time.monotonic() + 3
        tick = threading.Event()
        while len(messages) < count and time.monotonic() < deadline:
            service.deliver(lambda chat, text: messages.append((chat, text)),
                            self.allowed if allowed is None else allowed)
            if len(messages) < count:
                tick.wait(0.005)
        self.assertEqual(len(messages), count, 'Worker did not deliver its accepted answer')
        return messages

    def test_feature_is_explicitly_disabled_without_opt_in(self):
        with patch.dict(os.environ, {'BIMA_ANALYST_ENABLED': '0'}):
            response = bot.handle_command('/analisis #1', '42', self.allowed,
                                          self.path, 1, '42')
        self.assertIn('belum diaktifkan', response)

    def test_private_reviewer_authorization_precedes_answer_or_context(self):
        service, answerer = self.service()
        for chat, sender, kind in [('-7', '42', 'group'), ('42', '42', 'group'),
                                    ('42', '43', 'private'), ('42', '', 'private'),
                                    ('42', '42', None), ('٤٢', '٤٢', 'private')]:
            with self.subTest(chat=chat, sender=sender, kind=kind):
                response = service.handle('/analisis #1', chat, sender,
                    self.allowed | {'-7', '٤٢'}, update_id=1, chat_type=kind)
                self.assertIn('reviewer', response)
                self.assertIsNone(service.handle('lanjut', chat, sender,
                    self.allowed, update_id=2, chat_type=kind, plain=True))
        with patch.dict(os.environ, {'TELEGRAM_REVIEW_USER_IDS': ''}):
            self.assertIn('reviewer', self.command(service, '/analisis #1'))
        self.assertEqual(answerer.calls, [])

    def test_analysis_comparison_and_followup_use_only_that_users_context(self):
        service, answerer = self.service()
        self.assertIn('diterima', self.command(service, '/analisis #1 mengapa?', update=1))
        self.assertIn('one.id', self.receive(service)[0][1])
        self.assertIn('diterima', self.command(service, '/bandingkan #2 #1', update=2, chat='43'))
        self.receive(service)
        self.command(service, '/tanya bukti apa?', update=3)
        self.receive(service)
        self.assertEqual(answerer.calls[0][:2], (('#1',), 'mengapa?'))
        self.assertEqual(answerer.calls[1][:2], (('#2', '#1'), ''))
        self.assertEqual(answerer.calls[2][0], ('#1',))
        self.assertEqual(answerer.calls[2][2][0]['question'], 'mengapa?')
        self.assertIn('/analisis', self.command(service, '/tanya konteks?', update=4, chat='44'))

    def test_invalid_commands_do_not_admit_work(self):
        service, answerer = self.service()
        for number, command in enumerate(('/analisis', '/analisis evil.com',
                '/analisis https://name:password@one.id/', '/analisis #0',
                '/analisis #1 ' + 'a' * 1001, '/tanya', '/bandingkan #1',
                '/bandingkan #1 two.id', '/bandingkan #1 #2 extra', '/lupakan extra'), 1):
            with self.subTest(command=command[:50]):
                self.assertIn('Gunakan', self.command(service, command, update=number))
        self.assertEqual(answerer.calls, [])

    def test_slow_answer_does_not_hold_existing_status_or_admission(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True))
        self.command(service, '/analisis #1')
        self.assertTrue(answerer.started.wait(2))
        response = bot.handle_command('/status', '42', self.allowed, self.path, 2, '42',
                                      analyst_service=service, chat_type='private')
        self.assertIn('Total URL', response)
        self.assertIn('masih', self.command(service, '/tanya masih sibuk?', update=3))
        self.assertFalse(answerer.release.is_set())
        answerer.release.set()
        self.receive(service)

    def test_duplicate_updates_survive_forgetting_without_reexecuting(self):
        service, answerer = self.service()
        first = self.command(service, '/analisis #1', update=12)
        self.assertEqual(first, self.command(service, '/analisis #1', update=12))
        self.receive(service)
        self.assertIn('dihapus', self.command(service, '/lupakan', update=13))
        self.assertEqual(first, self.command(service, '/analisis #1', update=12))
        self.assertIn('/analisis', self.command(service, '/tanya lanjut', update=14))
        self.assertEqual(len(answerer.calls), 1)

    def test_replay_survives_conversation_expiry_within_acknowledgment_ttl(self):
        now = [100.0]
        service, answerer = self.service(clock=lambda: now[0])
        self.command(service, '/analisis #1', update=10)
        self.receive(service)
        now[0] += 1801
        self.command(service, '/analisis #1', update=10)
        self.assertIn('diterima', self.command(service, '/analisis #2', update=11))
        self.assertIn('two.id', self.receive(service)[0][1])
        self.assertEqual([call[0] for call in answerer.calls], [('#1',), ('#2',)])

    def test_new_update_sequence_after_week_of_inactivity_is_accepted(self):
        now = [100.0]
        service, answerer = self.service(clock=lambda: now[0])
        self.command(service, '/analisis #1', update=9000)
        self.receive(service)
        for update in range(9001, 10030):
            self.command(service, '/lupakan', update=update)
        now[0] += 7 * 86400 + 1
        self.assertIn('diterima', self.command(service, '/analisis #2', update=10))
        self.assertIn('two.id', self.receive(service)[0][1])
        self.assertEqual([call[0] for call in answerer.calls], [('#1',), ('#2',)])

    def test_forgetting_drops_inflight_answer_and_allows_a_new_session(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True), workers=1)
        self.command(service, '/analisis #1', update=1)
        self.assertTrue(answerer.started.wait(2))
        self.assertIn('dihapus', self.command(service, '/lupakan', update=2))
        self.command(service, '/analisis #2', update=3)
        answerer.release.set()
        messages = self.receive(service)
        self.assertEqual(len(messages), 1)
        self.assertIn('two.id', messages[0][1])
        self.assertNotIn('one.id', messages[0][1])

    def test_expired_inflight_answer_cannot_attach_to_recreated_session(self):
        now = [100.0]
        service, answerer = self.service(EvidenceAnswerer(blocked=True), workers=1,
                                        clock=lambda: now[0], session_ttl=30)
        self.command(service, '/analisis #1', update=1)
        self.assertTrue(answerer.started.wait(2))
        now[0] = 131.0
        self.assertIn('/analisis', self.command(service, '/tanya sudah kedaluwarsa?', update=2))
        self.command(service, '/analisis #2', update=3)
        answerer.release.set()
        self.assertIn('two.id', self.receive(service)[0][1])

    def test_undelivered_answers_count_toward_outstanding_limit(self):
        service, answerer = self.service(max_outstanding=2)
        self.command(service, '/analisis #1', update=1)
        self.command(service, '/analisis #2', update=2, chat='43')
        self.assertTrue(answerer.started.wait(2))
        self.assertIn('penuh', self.command(service, '/analisis #1', update=3, chat='44'))
        self.receive(service, 2)
        self.assertIn('diterima', self.command(service, '/analisis #1', update=4, chat='44'))
        self.receive(service)

    def test_transient_delivery_failure_retains_answer_and_definite_failure_releases_it(self):
        service, answerer = self.service(max_outstanding=1)
        self.command(service, '/analisis #1', update=1)
        failed = threading.Event()
        def unavailable(chat, text):
            failed.set()
            raise URLError('https://token.example/private')
        deadline = time.monotonic() + 3
        while not failed.is_set() and time.monotonic() < deadline:
            service.deliver(unavailable, self.allowed)
            failed.wait(0.005)
        self.assertTrue(failed.is_set())
        self.assertIn('one.id', self.receive(service)[0][1])
        self.command(service, '/analisis #2', update=2)
        refused = threading.Event()
        def forbidden(chat, text):
            refused.set()
            raise HTTPError('https://token.example/private', 403, 'forbidden', {}, None)
        deadline = time.monotonic() + 3
        while not refused.is_set() and time.monotonic() < deadline:
            service.deliver(forbidden, self.allowed)
            refused.wait(0.005)
        self.assertTrue(refused.is_set())
        self.assertIn('diterima', self.command(service, '/analisis #1', update=3))
        self.receive(service)

    def test_transient_failures_rotate_so_third_recipient_is_delivered(self):
        service, answerer = self.service(workers=1, max_outstanding=3)
        for update, chat in enumerate(('42', '43', '44'), 1):
            self.assertIn('diterima', self.command(service, '/analisis #1', update=update, chat=chat))
        attempts = []
        received = []
        delivered = threading.Event()
        def send(chat, text):
            attempts.append(chat)
            if chat in ('42', '43'):
                raise URLError('synthetic temporary delivery failure')
            received.append((chat, text))
            delivered.set()
        deadline = time.monotonic() + 3
        with self.assertLogs('pandi.telegram.analyst', level='WARNING'):
            while not delivered.is_set() and time.monotonic() < deadline:
                before = len(attempts)
                service.deliver(send, self.allowed, limit=2)
                self.assertLessEqual(len(attempts) - before, 2)
                delivered.wait(0.005)
        self.assertTrue(delivered.is_set(), 'Transient failures starved the third recipient')
        self.assertEqual([chat for chat, _ in received], ['44'])
        self.assertIn('[job#1]', received[0][1])
        retried = self.receive(service, 2)
        self.assertEqual({chat for chat, _ in retried}, {'42', '43'})
        self.assertEqual(len(answerer.calls), 3)

    def test_revoked_authorization_discards_ready_answer(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True), max_outstanding=1)
        self.command(service, '/analisis #1', update=1)
        self.assertTrue(answerer.started.wait(2))
        answerer.release.set()
        messages = []
        with patch.dict(os.environ, {'TELEGRAM_REVIEW_USER_IDS': ''}):
            service.deliver(lambda *args: messages.append(args), self.allowed)
        self.assertEqual(messages, [])
        self.assertIn('/analisis', self.command(service, '/tanya lanjut', update=2))

    def test_empty_evidence_does_not_create_followup_context(self):
        def empty(*args, **kwargs):
            return {'text': 'Tidak ada bukti tersimpan.',
                    'context': {'targets': [], 'history': []}}
        service, _ = self.service(empty)
        self.command(service, '/analisis missing.id', update=1)
        self.receive(service)
        self.assertIn('/analisis', self.command(service, '/tanya lanjut', update=2))

    def test_failed_replacement_does_not_reuse_the_previous_site(self):
        saved = EvidenceAnswerer()
        def answer(*args, **kwargs):
            if args[1] == ('#2',):
                raise ValueError('secret-token-in-untrusted-error')
            return saved(*args, **kwargs)
        service, _ = self.service(answer)
        self.command(service, '/analisis #1', update=1)
        self.receive(service)
        with self.assertLogs('pandi.telegram.analyst', level='WARNING') as logged:
            self.command(service, '/analisis #2', update=2)
            self.assertNotIn('secret-token', self.receive(service)[0][1])
        self.assertNotIn('secret-token', '\n'.join(logged.output))
        self.assertIn('/analisis', self.command(service, '/tanya situs mana?', update=3))
        self.command(service, '/analisis #1', update=4)
        self.assertIn('one.id', self.receive(service)[0][1])

    def test_plain_followups_only_reply_with_delivered_active_context(self):
        service, answerer = self.service()
        self.assertIsNone(service.handle('halo', '42', '42', self.allowed,
                          update_id=1, chat_type='private', plain=True))
        self.command(service, '/analisis #1', update=2)
        self.receive(service)
        self.assertIn('diterima', service.handle('jelaskan lagi', '42', '42', self.allowed,
                      update_id=3, chat_type='private', plain=True))
        self.receive(service)
        self.command(service, '/lupakan', update=4)
        self.assertIsNone(service.handle('jelaskan lagi', '42', '42', self.allowed,
                          update_id=3, chat_type='private', plain=True))
        self.assertEqual(len(answerer.calls), 2)

    def test_followup_command_accepts_telegram_suffix_and_whitespace(self):
        service, answerer = self.service()
        self.command(service, '/analisis #1', update=1)
        self.receive(service)
        self.assertIn('diterima', self.command(service, '/TANYA@bima_bot\tbukti apa?', update=2))
        self.receive(service)
        self.assertEqual(answerer.calls[-1][1], 'bukti apa?')

    def test_forgetting_queued_work_prevents_answerer_call(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True), workers=1)
        self.command(service, '/analisis #1', update=1)
        self.assertTrue(answerer.started.wait(2))
        self.command(service, '/analisis #2', update=2, chat='43')
        self.command(service, '/lupakan', update=3, chat='43')
        answerer.release.set()
        self.receive(service)
        self.command(service, '/analisis #1', update=4, chat='44')
        self.receive(service)
        self.assertEqual([call[0] for call in answerer.calls], [('#1',), ('#1',)])

    def test_session_limit_evicts_idle_context_without_evicting_pending_work(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True), max_sessions=1)
        self.command(service, '/analisis #1', update=1)
        self.assertTrue(answerer.started.wait(2))
        self.assertIn('penuh', self.command(service, '/analisis #2', update=2, chat='43'))
        answerer.release.set()
        self.receive(service)
        self.command(service, '/analisis #2', update=3, chat='43')
        self.receive(service)
        self.assertIn('/analisis', self.command(service, '/tanya konteks lama?', update=4))

    def test_main_loop_delivers_worker_answers_without_blocking_status(self):
        service, answerer = self.service(EvidenceAnswerer(blocked=True))
        module = importlib.import_module('telegram_analyst')
        messages = []
        polls = [0]
        deadline = time.monotonic() + 3
        def update(number, text):
            return {'update_id': number, 'message': {'chat': {'id': 42, 'type': 'private'},
                    'from': {'id': 42}, 'text': text}}
        def get_updates(*args, **kwargs):
            polls[0] += 1
            if polls[0] == 1:
                return [update(1, '/analisis #1'), update(2, '/status')]
            if polls[0] == 2:
                self.assertTrue(answerer.started.wait(1), 'main did not create/admit the analyst service')
                self.assertTrue(any('Total URL' in text for _, text in messages))
                self.assertFalse(answerer.release.is_set())
                answerer.release.set()
            if any('[job#1]' in text for _, text in messages):
                raise KeyboardInterrupt()
            self.assertLess(time.monotonic(), deadline, 'main did not deliver the ready answer')
            threading.Event().wait(0.005)
            return []
        environment = {'TELEGRAM_BOT_TOKEN': 'synthetic-bot-token',
                       'TELEGRAM_ALLOWED_CHAT_IDS': '42', 'TELEGRAM_POLL_SECONDS': '5',
                       'TELEGRAM_OFFSET_PATH': str(self.path.parent / 'offset'),
                       'CRAWLER_DB_PATH': str(self.path), 'TELEGRAM_CHECKS_ENABLED': '0'}
        with patch.dict(os.environ, environment), \
             patch.object(module, 'AnalystService', return_value=service), \
             patch.object(bot, 'get_updates', side_effect=get_updates), \
             patch.object(bot, 'send_message', side_effect=lambda token, chat, text: messages.append((chat, text))):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertEqual(len(answerer.calls), 1)
        self.assertIn('berhenti', self.command(service, '/analisis #2', update=3))
        self.assertEqual((self.path.parent / 'offset').read_text(), '3')

    def test_main_only_routes_plain_text_after_a_private_analysis(self):
        service, answerer = self.service()
        module = importlib.import_module('telegram_analyst')
        messages = []
        phase = [0]
        deadline = time.monotonic() + 3
        def update(number, text, kind='private', chat=42):
            return {'update_id': number, 'message': {'chat': {'id': chat, 'type': kind},
                    'from': {'id': 42}, 'text': text}}
        def get_updates(*args, **kwargs):
            if phase[0] == 0:
                phase[0] = 1
                return [update(1, 'halo'), update(2, '/analisis #1')]
            answers = [text for _, text in messages if '[job#1]' in text]
            if phase[0] == 1 and answers:
                phase[0] = 2
                return [update(3, 'bukti apa?'), update(4, 'rahasia grup', 'group', -7)]
            if phase[0] == 2 and len(answers) == 2:
                raise KeyboardInterrupt()
            self.assertLess(time.monotonic(), deadline, 'plain followup was not answered')
            threading.Event().wait(0.005)
            return []
        environment = {'TELEGRAM_BOT_TOKEN': 'synthetic-bot-token',
                       'TELEGRAM_ALLOWED_CHAT_IDS': '42,-7', 'TELEGRAM_POLL_SECONDS': '5',
                       'TELEGRAM_OFFSET_PATH': str(self.path.parent / 'offset'),
                       'CRAWLER_DB_PATH': str(self.path), 'TELEGRAM_CHECKS_ENABLED': '0'}
        with patch.dict(os.environ, environment), \
             patch.object(module, 'AnalystService', return_value=service), \
             patch.object(bot, 'get_updates', side_effect=get_updates), \
             patch.object(bot, 'send_message', side_effect=lambda token, chat, text: messages.append((chat, text))):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
        self.assertEqual(len(answerer.calls), 2)
        self.assertEqual(answerer.calls[1][1], 'bukti apa?')
        self.assertEqual({chat for chat, _ in messages}, {'42'})


if __name__ == '__main__':
    unittest.main()
