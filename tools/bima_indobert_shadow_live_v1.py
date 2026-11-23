#!/usr/bin/env python3
"""Independent, read-only-to-BIMA IndoBERT shadow scorer.

Reads private sampled evidence from a queue, compares a candidate model with
recorded production IndoBERT, writes metadata-only audit, removes processed
queue items. Never accesses BIMA's database or alert API.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

# Script location: <repo>/tools/
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'multi'))
from shadow_capture_v1 import VERSION, MAX_EVENT_BYTES  # noqa: E402

SHADOW_VERSION = 'bima_indobert_shadow_live_v1'


def chunk_v27(text, min_chunk_chars=32, max_chars=1800, max_chunks=8):
    """Replicate legacy sentence splitting with a repaired short final chunk."""
    normalized = re.sub(r'\s+', ' ', text or '').strip()
    remaining = normalized
    chunks = []
    while remaining and len(chunks) < max_chunks:
        chunk = remaining[:max_chars]
        boundary = max(chunk.rfind('.'), chunk.rfind('!'),
                       chunk.rfind('?'), chunk.rfind('\n'))
        if boundary > 500:
            chunk = chunk[:boundary+1]
        chunk = chunk.strip()
        if not chunk:
            break
        chunks.append(chunk)
        remaining = remaining[len(chunk):].strip()
    if len(chunks) >= 2 and len(chunks[-1]) < min_chunk_chars:
        short_tail = chunks.pop()
        previous = chunks[-1]
        # Retain the original separator; some sentence punctuation is adjacent.
        separator = (' ' if (previous + ' ' + short_tail) in normalized else
                     '' if (previous + short_tail) in normalized else ' ')
        chunks[-1] = previous + separator + short_tail
    return chunks, bool(remaining)


def aggregate(probabilities, threshold=0.80, strong_threshold=0.92):
    if not probabilities:
        raise ValueError('No scores')
    if any(not 0.0 <= x <= 1.0 for x in probabilities):
        raise ValueError('Out-of-range probability')
    scores = sorted(probabilities, reverse=True)
    peak = scores[0]
    mean_top2 = sum(scores[:2]) / min(2, len(scores))
    count = sum(p >= threshold for p in probabilities)
    strong = sum(p >= strong_threshold for p in probabilities)
    confidence = peak if len(scores) == 1 else mean_top2
    hit = (confidence >= strong_threshold if len(scores) == 1 else
           confidence >= threshold and (count >= 2 or strong >= 1))
    return {'hit': bool(hit), 'confidence': confidence, 'max_chunk_probability': peak,
            'above_threshold': count, 'above_strong': strong,
            'masked_strong_chunk': bool(not hit and strong > 0)}


def _private_dir(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'Private directory missing or symlink: {path}')
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f'Private directory must be owned by worker with mode 0700: {path}')


def _load_event(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError('Invalid/symlink/oversized event')
    with path.open('r', encoding='utf-8') as handle:
        event = json.load(handle)
    if event.get('version') != VERSION or not isinstance(event.get('text'), str):
        raise ValueError('Unsupported or incomplete event')
    if not re.fullmatch(r'[a-f0-9]{32}', event.get('event_id', '')):
        raise ValueError('Invalid event id')
    if path.name != event['event_id'] + '.json':
        raise ValueError('Event ID does not match filename')
    if len(event['text']) > 16000:
        raise ValueError('Event text exceeds limit')
    captured_digest = hashlib.sha256(event['text'].encode('utf-8', 'replace')).hexdigest()
    if event.get('captured_text_sha256') != captured_digest:
        raise ValueError('Captured text digest mismatch')
    if not isinstance(event.get('original_indobert_used'), bool) or not event['original_indobert_used']:
        raise ValueError('Missing original IndoBERT inference')
    return event


def _append_json(path, value):
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, 'ab') as out:
        out.write((json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
        out.flush()
        os.fsync(out.fileno())


class CandidateScorer:
    def __init__(self, model_path, device='cpu', batch_size=2, max_length=256,
                 gambling_index=1, torch_threads=1):
        # Model weights never reside inside the crawler process.
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        torch.set_num_threads(torch_threads)
        self.torch = torch
        self.device = device
        self.batch_size = batch_size
        self.max_length = max_length
        self.gambling_index = gambling_index
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path, local_files_only=True).to(device)
        self.model.eval()
        if not (0 <= gambling_index < int(self.model.config.num_labels)):
            raise ValueError('Invalid gambling label index for checkpoint')
        # Record the exact local model weights/config once, not merely its path.
        digest = hashlib.sha256()
        selected = [p for p in sorted(Path(model_path).iterdir())
                    if p.is_file() and (p.name == 'config.json'
                                        or p.suffix in ('.safetensors', '.bin'))]
        if not any(p.suffix in ('.safetensors', '.bin') for p in selected):
            raise ValueError('No local model weights found in checkpoint')
        for path in selected:
            digest.update(path.name.encode('utf-8'))
            with path.open('rb') as f:
                for piece in iter(lambda: f.read(1024 * 1024), b''):
                    digest.update(piece)
        self.checkpoint_sha256 = digest.hexdigest()

    def score(self, text):
        chunks, tail = chunk_v27(text)
        if not chunks:
            raise ValueError('Empty text')
        # Audit token-level truncation separately from the character chunk repair.
        original_token_lengths = [len(self.tokenizer(
            chunk, truncation=False, add_special_tokens=True)['input_ids'])
            for chunk in chunks]
        values = []
        with self.torch.inference_mode():
            for i in range(0, len(chunks), self.batch_size):
                inputs = self.tokenizer(chunks[i:i+self.batch_size], truncation=True,
                                        padding=True, max_length=self.max_length,
                                        return_tensors='pt')
                inputs = {k: v.to(self.device) for k, v in inputs.items()}
                logits = self.model(**inputs).logits
                values += self.torch.softmax(logits, dim=-1)[:, self.gambling_index].cpu().tolist()
        result = aggregate([float(x) for x in values])
        result.update(n_chunks=len(chunks), chunk_lengths=[len(x) for x in chunks],
                      chunk_probabilities=[round(float(x), 6) for x in values],
                      token_lengths=original_token_lengths,
                      truncated_token_chunks=sum(n > self.max_length for n in original_token_lengths),
                      unscored_tail=tail)
        return result


def process_event(event, scorer):
    score = scorer.score(event['text'])
    old_conf = event.get('original_indobert_conf')
    old_pred = event.get('original_indobert_pred')
    old_hit = (old_pred == 'gambling') if old_pred in ('gambling', 'non-gambling', 'non_gambling') else None
    return {
        'version': SHADOW_VERSION,
        'event_id': event['event_id'], 'captured_utc': event.get('captured_utc'),
        'scored_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'hostname': event['hostname'], 'source': event.get('source'),
        'original_text_sha256': event.get('original_text_sha256'),
        'candidate_checkpoint_sha256': getattr(scorer, 'checkpoint_sha256', None),
        'input_capped': bool(event.get('text_truncated')),
        'original_classifier_status': event.get('original_classifier_status'),
        'original_indobert_conf': old_conf,
        'original_indobert_pred': old_pred,
        'original_indobert_hit': old_hit,
        'candidate': score,
        'model_disagreement': (old_hit != score['hit'] if old_hit is not None else None),
        'label': None, 'evaluation_approved': False, 'training_approved': False,
        'alert_sent': False,
    }


def _memory_available_mib():
    try:
        with open('/proc/meminfo', 'r', encoding='ascii') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def run_worker(args, scorer=None):
    spool = Path(args.spool).expanduser()
    output = Path(args.out).expanduser()
    _private_dir(spool)
    _private_dir(output)
    if spool.resolve() == output.resolve():
        raise ValueError('Audit output must not be inside the pending spool directory')
    lock_path = output / '.shadow_worker.lock'
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(str(lock_path), flags, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if scorer is None:
            available = _memory_available_mib()
            if available is not None and available < args.min_available_mib:
                raise RuntimeError(f'Memory preflight: {available} MiB free < {args.min_available_mib} MiB')
            scorer = CandidateScorer(args.model, args.device, args.batch_size,
                                     args.max_length, args.gambling_index, args.torch_threads)
        processed = 0
        while True:
            files = sorted(spool.glob('*.json'))
            if not files:
                if args.once:
                    break
                time.sleep(args.poll_seconds)
                continue
            for path in files:
                if args.max_events and processed >= args.max_events:
                    return processed
                try:
                    event = _load_event(path)
                    record = process_event(event, scorer)
                    _append_json(output / 'shadow_comparisons_v1.jsonl', record)
                    path.unlink()  # Input raw text retained only until successful audit write.
                    processed += 1
                except Exception as exc:
                    # Keep failed evidence for operator review, prevent infinite hot loop.
                    print(f'[shadow] skipped {path.name}: {type(exc).__name__}: {exc}', file=sys.stderr)
            if args.once or (args.max_events and processed >= args.max_events):
                break
            time.sleep(args.poll_seconds)
        return processed
    finally:
        os.close(lock_fd)


def main(argv=None):
    p = argparse.ArgumentParser(description='BIMA candidate IndoBERT shadow scorer: never sends alerts')
    p.add_argument('--spool', required=True, help='Private directory of pending sample JSON files')
    p.add_argument('--out', required=True, help='Existing separate private output directory')
    p.add_argument('--model', required=True, help='Local candidate checkpoint path')
    p.add_argument('--once', action='store_true', help='Process available events then exit')
    p.add_argument('--max-events', type=int, default=100,
                   help='Max events in this run (0=unlimited; use 0 for service loop)')
    p.add_argument('--poll-seconds', type=float, default=10)
    p.add_argument('--device', choices=['cpu', 'cuda', 'mps'], default='cpu')
    p.add_argument('--batch-size', type=int, default=2)
    p.add_argument('--max-length', type=int, default=256)
    p.add_argument('--gambling-index', type=int, default=1)
    p.add_argument('--torch-threads', type=int, default=1)
    p.add_argument('--min-available-mib', type=int, default=1500,
                   help='Linux memory safety preflight; 1500 MiB by default')
    a = p.parse_args(argv)
    if not (0 <= a.max_events <= 10000 and 1 <= a.batch_size <= 8 and
            64 <= a.max_length <= 512 and 1 <= a.torch_threads <= 8 and
            1 <= a.poll_seconds <= 600 and 0 <= a.gambling_index <= 100):
        p.error('Invalid bounds for inference or queue')
    processed = run_worker(a)
    print(json.dumps({'status': 'complete', 'processed': processed, 'output': a.out}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
