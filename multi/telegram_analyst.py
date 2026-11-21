"""Bounded, temporary private-reviewer conversations over saved evidence.

The service owns no Telegram credentials and writes no files or database rows.
Workers call the synchronous analyst; the polling thread supplies delivery I/O.
"""
from collections import OrderedDict, deque
from dataclasses import dataclass
import itertools
import logging
import os
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from telegram_checks import normalize_input_url
from telegram_review import authorized


LOGGER = logging.getLogger('pandi.telegram.analyst')
COMMANDS = frozenset(('/analisis', '/tanya', '/bandingkan', '/lupakan'))
HELP = ('\n/analisis <domain .id|URL|#job> [pertanyaan] - analisis bukti tersimpan'
        '\n/tanya <pertanyaan> - lanjutkan konteks pribadi'
        '\n/bandingkan <#job> <#job> - bandingkan dua hasil tersimpan'
        '\n/lupakan - hapus konteks percakapan sementara')
USAGE = ('Gunakan /analisis <domain .id|URL|#job> [pertanyaan], '
         '/tanya <pertanyaan>, /bandingkan <#job> <#job>, atau /lupakan. '
         'Pertanyaan maksimal 1000 karakter.')
DENIED = 'Analisis hanya tersedia di chat pribadi reviewer terdaftar.'
ACCEPTED = 'Permintaan analisis diterima. Jawaban dari bukti tersimpan akan dikirim ke chat pribadi ini.'
NO_CONTEXT = 'Belum ada konteks analisis aktif. Mulai dengan /analisis <domain .id|URL|#job>.'
_TOKENS = itertools.count(1)
_JOB = re.compile(r'#?([1-9][0-9]{0,17})\Z')


@dataclass
class _Session:
    token: int
    touched: float
    context: dict | None = None
    pending: int | None = None


@dataclass
class _Work:
    number: int
    key: tuple
    token: int
    targets: tuple
    question: str
    history: tuple
    allowed: object
    state: str = 'queued'
    cancelled: bool = False
    answer: dict | None = None


def _private(chat, sender, allowed, chat_type='private'):
    return (chat_type == 'private' and isinstance(chat, str)
            and isinstance(sender, str) and authorized(chat, sender, allowed))


def _target(value):
    match = _JOB.fullmatch(value)
    if match:
        return '#' + match[1]
    if len(value) > 2048 or '\\' in value or any(char.isspace() for char in value):
        return None
    normalized = normalize_input_url(value)
    if normalized and '@' not in urlsplit(normalized).netloc:
        return value
    return None


def _parse(text, plain):
    if not isinstance(text, str) or len(text) > 4096:
        return None
    text = text.strip()
    if plain:
        return ('/tanya', (), text) if text and not text.startswith('/') and len(text) <= 1000 else None
    parts = text.split(maxsplit=2)
    command = parts[0].split('@')[0].lower() if parts else ''
    if command == '/lupakan' and len(parts) == 1:
        return (command, (), '')
    if command == '/analisis' and len(parts) >= 2:
        target = _target(parts[1])
        question = parts[2].strip() if len(parts) == 3 else ''
        if target and len(question) <= 1000:
            return (command, (target,), question)
    if command == '/bandingkan':
        arguments = text.split()[1:]
        if len(arguments) == 2 and all(_JOB.fullmatch(value) for value in arguments):
            return (command, tuple('#' + value.lstrip('#') for value in arguments), '')
    if command == '/tanya':
        arguments = text.split(maxsplit=1)
        question = arguments[1].strip() if len(arguments) == 2 else ''
        if question and len(question) <= 1000:
            return (command, (), question)
    return None


def _context(value):
    """Copy only the bounded answerer's public conversation contract."""
    value = value if isinstance(value, dict) else {}
    targets = value.get('targets')
    if not isinstance(targets, (tuple, list)) or not targets:
        return None
    targets = [target for target in targets[:5]
               if isinstance(target, str) and len(target) <= 2048]
    history = value.get('history', ())
    if not isinstance(history, (tuple, list)):
        history = ()
    history = [{'question': turn.get('question', '')[:1000],
                'answer': turn.get('answer', '')[:1200]}
               for turn in history[-2:] if isinstance(turn, dict)
               and isinstance(turn.get('question', ''), str)
               and isinstance(turn.get('answer', ''), str)]
    return {'targets': targets, 'history': history} if targets else None


class AnalystService:
    def __init__(self, production_db, *, answerer=None, learning_db=None, environ=None,
                 workers=2, max_outstanding=8, max_sessions=100, session_ttl=1800,
                 clock=time.monotonic):
        if (not 1 <= workers <= 2 or not 1 <= max_outstanding <= 8
                or not 1 <= max_sessions <= 100 or not 0 < session_ttl <= 1800):
            raise ValueError('Invalid bounded analyst service limits')
        self.production_db = production_db
        self.answerer = answerer
        self.learning_db = learning_db
        self.environ = os.environ if environ is None else environ
        self.max_outstanding = max_outstanding
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self.clock = clock
        self._condition = threading.Condition()
        self._sessions = OrderedDict()
        self._seen = OrderedDict()
        self._requests = {}
        self._queue = deque()
        self._ready = deque()
        self._closed = False
        self._threads = [threading.Thread(target=self._worker, daemon=True,
                          name=f'bima-analyst-{number}') for number in range(workers)]
        for thread in self._threads:
            thread.start()

    def _discard(self, work):
        self._requests.pop(work.number, None)
        for queue in (self._queue, self._ready):
            try:
                queue.remove(work.number)
            except ValueError:
                pass
        session = self._sessions.get(work.key)
        if session and session.token == work.token and session.pending == work.number:
            session.pending = None

    def _forget(self, key):
        session = self._sessions.pop(key, None)
        if session and session.pending is not None:
            work = self._requests.get(session.pending)
            if work:
                work.cancelled = True
                work.history = ()
                if work.state not in ('running', 'delivering'):
                    self._discard(work)

    def _prune(self, now, allowed=None):
        for key, session in list(self._sessions.items()):
            if (now - session.touched >= self.session_ttl
                    or allowed is not None and not _private(*key, allowed)):
                self._forget(key)
        # Independent, bounded replay retention survives conversation expiry
        # and forgetting. Never retain a permanent update-ID floor: Telegram
        # may choose a new random sequence after a week without updates.
        for key, (when, _) in list(self._seen.items()):
            if now - when >= 86400:
                del self._seen[key]

    def _remember(self, key, when, response):
        if key is None:
            return
        self._seen[key] = (when, response)
        while len(self._seen) > 1024:
            self._seen.popitem(last=False)

    def _valid(self, work):
        session = self._sessions.get(work.key)
        return (not work.cancelled and session is not None
                and session.token == work.token and session.pending == work.number)

    def handle(self, text, chat_id, sender_id, allowed, *, update_id=None,
               chat_type=None, plain=False):
        # Authorization precedes parsing and every access to conversation state.
        if not _private(chat_id, sender_id, allowed, chat_type):
            return None if plain else DENIED
        parsed = _parse(text, plain)
        key = (chat_id, sender_id)
        dedup = (chat_id, sender_id, update_id) if isinstance(update_id, int) else None
        with self._condition:
            now = self.clock()
            self._prune(now, allowed)
            if self._closed:
                return None if plain else 'Layanan analisis sedang berhenti. Coba lagi nanti.'
            session = self._sessions.get(key)
            if plain and (session is None or session.context is None):
                return None
            if dedup in self._seen:
                return self._seen[dedup][1]
            if parsed is None:
                return USAGE
            command, targets, question = parsed
            if command == '/lupakan':
                self._forget(key)
                response = 'Konteks percakapan sementara dihapus; jawaban yang masih menunggu dibatalkan.'
                self._remember(dedup, now, response)
                return response
            if session and session.pending is not None:
                return 'Permintaan sebelumnya masih diproses atau menunggu pengiriman. Tunggu jawabannya atau /lupakan.'
            if command == '/tanya':
                if session is None or session.context is None:
                    return NO_CONTEXT
                targets = tuple(session.context['targets'])
                history = tuple(dict(turn) for turn in session.context['history'])
            else:
                history = ()
            if len(self._requests) >= self.max_outstanding:
                return 'Antrean analisis penuh. Coba lagi setelah jawaban sebelumnya terkirim.'
            if session is None:
                if len(self._sessions) >= self.max_sessions:
                    idle = next((key for key, item in self._sessions.items()
                                 if item.pending is None), None)
                    if idle is None:
                        return 'Antrean sesi analisis penuh. Coba lagi nanti.'
                    self._forget(idle)
                session = _Session(next(_TOKENS), now)
                self._sessions[key] = session
            session.touched = now
            if command != '/tanya':
                session.context = None
            self._sessions.move_to_end(key)
            number = next(_TOKENS)
            work = _Work(number, key, session.token, tuple(targets), question, history, allowed)
            session.pending = number
            self._requests[number] = work
            self._queue.append(number)
            self._remember(dedup, now, ACCEPTED)
            self._condition.notify()
            return ACCEPTED

    def _worker(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed:
                    return
                number = self._queue.popleft()
                work = self._requests.get(number)
                self._prune(self.clock())
                if work is None:
                    continue
                if not self._valid(work) or not _private(*work.key, work.allowed):
                    self._discard(work)
                    continue
                work.state = 'running'
            try:
                answerer = self.answerer
                if answerer is None:
                    from domain_analyst import answer_question
                    answerer = answer_question
                result = answerer(self.production_db, work.targets, work.question, work.history,
                                  learning_db=self.learning_db, environ=self.environ)
                if not isinstance(result, dict) or not isinstance(result.get('text'), str) or not result['text'].strip():
                    raise ValueError('Invalid analyst result')
                answer = {'text': result['text'][:3800],
                          'context': _context(result.get('context'))}
            except Exception as error:
                LOGGER.warning('Analyst answer failed (%s)', type(error).__name__)
                answer = {'text': 'Analisis belum dapat diselesaikan. Coba /analisis lagi setelah sumber data tersedia.',
                          'context': None}
            with self._condition:
                self._prune(self.clock())
                if self._closed or not self._valid(work) or not _private(*work.key, work.allowed):
                    self._discard(work)
                    continue
                work.answer = answer
                work.state = 'ready'
                self._ready.append(number)

    def deliver(self, send, allowed, *, limit=2):
        """Try a bounded outbox batch; never hold state locks during transport."""
        with self._condition:
            self._prune(self.clock(), allowed)
            numbers = list(self._ready)[:max(0, min(limit, 8))]
        delivered = 0
        for number in numbers:
            with self._condition:
                self._prune(self.clock(), allowed)
                work = self._requests.get(number)
                if work is None or work.state != 'ready':
                    continue
                if not self._valid(work) or not _private(*work.key, allowed):
                    self._discard(work)
                    continue
                work.state = 'delivering'
                chat, text = work.key[0], work.answer['text']
            permanent = False
            success = False
            try:
                send(chat, text)
                success = True
            except HTTPError as error:
                permanent = error.code in (400, 403)
                LOGGER.warning('Analyst delivery failed (HTTP %s)', error.code)
            except Exception as error:
                LOGGER.warning('Analyst delivery failed (%s)', type(error).__name__)
            with self._condition:
                self._prune(self.clock(), allowed)
                if not self._valid(work):
                    self._discard(work)
                elif success:
                    session = self._sessions[work.key]
                    session.context = work.answer['context']
                    session.touched = self.clock()
                    self._discard(work)
                    delivered += 1
                elif permanent:
                    self._discard(work)
                else:
                    work.state = 'ready'
                    # Retry later without starving other accepted recipients.
                    self._ready.remove(number)
                    self._ready.append(number)
        return delivered

    def close(self, timeout=1):
        """Invalidate pending work; bounded shutdown does not await a provider."""
        with self._condition:
            self._closed = True
            for key in list(self._sessions):
                self._forget(key)
            self._condition.notify_all()
        deadline = time.monotonic() + max(0, timeout)
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
