"""Cross-case triage on bounded review samples. No fetches or label writes."""
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

LIMIT = 500
MIN_GROUPS = 3
METHOD = {"http": "Redirect HTTP", "meta_refresh": "Petunjuk meta refresh",
          "js_static": "Petunjuk JS statis"}


def observations(production, jobs):
    signals = defaultdict(set)
    source = Path(production).resolve()
    if not source.is_file() or not jobs:
        return signals
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='proactive_redirect_observations'").fetchone():
            return signals
        # Only attribute an observation to the exact source URL of the case.
        for job in jobs:
            rows = conn.execute(
                "SELECT o.method,o.destination_url,o.http_status FROM jobs j "
                "JOIN proactive_redirect_observations o ON o.source_url=j.url "
                "WHERE j.id=? AND o.external=1 ORDER BY o.last_seen DESC LIMIT 3", (job,))
            for method, url, status in rows:
                if method.startswith("js_"):
                    method = "js_static"
                if method not in METHOD or (method == "http" and status not in (301,302,303,307,308)):
                    continue
                try:
                    host = urlsplit(url).hostname
                except ValueError:
                    continue
                if host:
                    signals[job].add(f"{METHOD[method]} ke {host}")
    return signals


def analyze(conn, production):
    rows = conn.execute(
        "SELECT c.id,c.source_job_id,c.domain_group,c.hostname,c.final_hostname,"
        "COALESCE(t.verdict,c.label),substr(e.text_value,1,600) "
        "FROM review_cases c LEFT JOIN telegram_incidents t ON t.case_id=c.id "
        "LEFT JOIN case_evidence e ON e.case_id=c.id ORDER BY c.id DESC LIMIT ?", (LIMIT,)
    ).fetchall()
    redirect = observations(production, [r[1] for r in rows])
    groups = defaultdict(list)
    for key, job, domain, host, final, verdict, excerpt in rows:
        destinations = set(redirect[job])
        if final and final != host:
            destinations.add("Host akhir crawl " + final)
        words = re.findall(r"\w+", (excerpt or "").casefold())
        phrases = {" ".join(words[i:i+3]) for i in range(len(words)-2)
                   if all(w.isalpha() and 2 <= len(w) <= 30 for w in words[i:i+3])}
        for dest in destinations or {""}:
            if dest:
                groups[dest, ""].append((key, domain, verdict))
            for phrase in phrases:
                groups[dest, phrase].append((key, domain, verdict))
        if destinations:
            for phrase in phrases:
                groups["", phrase].append((key, domain, verdict))

    ranked = []
    for (dest, phrase), members in groups.items():
        domains = {d for _, d, _ in members}
        if len(domains) < MIN_GROUPS:
            continue
        positive = {d for _, d, v in members if v == "true_positive"}
        negative = {d for _, d, v in members if v == "false_positive"}
        signals = [s for s in (dest, f'Frasa “{phrase}”' if phrase else "") if s]
        # Human confirmation raises review priority only. Prefer corroborating signals.
        rank = (not bool(positive), -len(signals), not bool(dest), -len(domains), signals)
        ranked.append((rank, signals, len(domains), len(positive), len(negative), members))
    ranked.sort(key=lambda item: item[0])
    summaries = {}
    first = None
    for _, signals, size, yes, no, members in ranked:
        examples = ", ".join(sorted(domains)[:3])
        for key, _, verdict in reversed(members):
            if key not in summaries:
                summaries[key] = ("Dugaan pola lintas situs · %d kelompok domain\n%s\n"
                                  "Contoh kelompok: %s\nKelompok dengan keputusan Judol: %d · Bukan judol: %d"
                                  % (size, " + ".join(signals), examples, yes, no))
            if first is None and verdict is None:
                first = key
    return first, summaries
