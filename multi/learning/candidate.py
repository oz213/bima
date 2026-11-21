"""Offline multinomial Naive Bayes text candidate trained on verified reviews."""
import fcntl
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .core import InsufficientExamples, export_dataset

VERSION = "hashed-word-nb-v1"
BINS = 65536


def features(text):
    words = re.findall(r"\w+", text.casefold())
    return Counter(int.from_bytes(hashlib.blake2s(w.encode(), digest_size=2).digest(), "big")
                   for w in words)


def fit(rows):
    documents = Counter()
    counts = [Counter(), Counter()]
    for row in rows:
        label = row["label"]
        documents[label] += 1
        counts[label].update(features(row["text"]))
    if not all(counts):
        raise InsufficientExamples("Training needs text tokens in both classes")
    totals = [sum(c.values()) + BINS for c in counts]
    return {
        "version": VERSION,
        "prior": math.log(documents[1] / documents[0]),
        "length_weight": math.log(totals[0] / totals[1]),
        "weights": {str(k): math.log((counts[1][k] + 1) / (counts[0][k] + 1))
                    for k in counts[0].keys() | counts[1].keys()},
    }


def predict(model, text):
    tokens = features(text)
    score = model["prior"] + sum(tokens.values()) * model["length_weight"]
    score += sum(n * model["weights"].get(str(k), 0) for k, n in tokens.items())
    return int(score > 0)


def evaluate(model, rows):
    tp = tn = fp = fn = 0
    for row in rows:
        if predict(model, row["text"]):
            if row["label"]:
                tp += 1
            else:
                fp += 1
        elif row["label"]:
            fn += 1
        else:
            tn += 1
    return dict(tp=tp, tn=tn, fp=fp, fn=fn, precision=tp / max(1, tp + fp),
                recall=tp / (tp + fn), f1=2 * tp / (2 * tp + fp + fn))


def learn(learning_db, runs):
    runs = Path(runs)
    runs.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (runs / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with TemporaryDirectory(dir=runs) as tmp:
            dataset = Path(tmp) / "dataset"
            try:
                manifest = export_dataset(learning_db, dataset)
                splits = manifest["splits"]
                if any(len(part["labels"]) != 2 for part in splits.values()):
                    raise InsufficientExamples("Each split needs both verified text labels")
                hashes = [splits[k]["sha256"] for k in ("train", "validation", "test")]
                run_id = hashlib.sha256("|".join([VERSION] + hashes).encode()).hexdigest()
                target = runs / run_id
                if target.is_dir():
                    return {"status": "up_to_date", "run": str(target)}

                def rows(part):
                    with (dataset / (part + ".jsonl")).open(encoding="utf-8") as source:
                        for line in source:
                            yield json.loads(line)

                model = fit(rows("train"))
            except InsufficientExamples as exc:
                return {"status": "waiting_for_labels", "reason": str(exc)}

            report = {"algorithm": VERSION, "splits": splits,
                      "validation": evaluate(model, rows("validation")),
                      "test": evaluate(model, rows("test"))}
            output = Path(tmp) / "candidate"
            output.mkdir(mode=0o700)
            for name, payload in (("model", model), ("report", report)):
                (output / (name + ".json")).write_text(json.dumps(payload, sort_keys=True),
                                                      encoding="utf-8")
            output.rename(target)
            return {"status": "candidate_ready", "run": str(target), **report}
