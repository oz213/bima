"""Priority metadata for BIMA Discovery jobs; Telegram /cek always remains first.

Metadata is in a separate small table: never rewrite existing jobs' verdicts,
attempts, evidence, queue status, or the Registry's urls.json baseline.
Only explicit discovery_queue --apply creates/writes this table.  The live
crawler's read helper gracefully handles databases not upgraded yet.
"""
from __future__ import annotations

from routine_queue import find_next_pending_job
from registry_snapshot_v2 import active_cycle, find_pending_registry


PRIORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_job_priority (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    provider TEXT NOT NULL CHECK(provider IN ('google','urlscan','brave')),
    discovered_at TEXT NOT NULL
)
"""


def has_priority_table(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_job_priority'"
    ).fetchone() is not None


def find_next_discovery_job(conn):
    """Return the earliest pending tagged job; read-only. No duplicate claims."""
    if not has_priority_table(conn):
        return None
    return conn.execute(
        "SELECT j.id,j.url FROM discovery_job_priority AS p "
        "JOIN jobs AS j ON j.id=p.job_id "
        "WHERE j.status='pending' "
        "ORDER BY p.discovered_at,p.job_id LIMIT 1"
    ).fetchone()



def find_next_registry_job(conn, last_id, *, wrap):
    """Find a normal Registry job, excluding the tagged Discovery lane.

    The separate query matters: otherwise a Discovery job can also be selected
    as `routine`, defeating the 4-to-1 fairness guarantee. The cursor remains
    scoped only to Registry jobs and preserves the existing wrap policy.
    """
    if active_cycle(conn) is not None:
        return find_pending_registry(conn, last_id, wrap=wrap)
    if not has_priority_table(conn):
        return find_next_pending_job(conn, last_id, wrap=wrap)
    statement = (
        "SELECT j.id,j.url FROM jobs j WHERE j.status='pending' "
        "AND j.id>? AND NOT EXISTS "
        "(SELECT 1 FROM discovery_job_priority p WHERE p.job_id=j.id) "
        "ORDER BY j.id LIMIT 1"
    )
    result = conn.execute(statement, (last_id,)).fetchone()
    if result is None and wrap:
        result = conn.execute(
            "SELECT j.id,j.url FROM jobs j WHERE j.status='pending' "
            "AND j.id<=? AND NOT EXISTS "
            "(SELECT 1 FROM discovery_job_priority p WHERE p.job_id=j.id) "
            "ORDER BY j.id LIMIT 1", (last_id,),
        ).fetchone()
    return result

def pending_priority_count(conn):
    if not has_priority_table(conn):
        return 0
    return int(conn.execute(
        "SELECT COUNT(*) FROM discovery_job_priority p "
        "JOIN jobs j ON j.id=p.job_id WHERE j.status='pending'"
    ).fetchone()[0])


def cleanup_finished_priorities(conn):
    """Maintenance called only during explicit queue --apply transaction.

    Keep in-progress and retryable jobs tagged so a crash/requeue can preserve
    their discovery lane; discard terminal verdict metadata from previous runs.
    """
    if not has_priority_table(conn):
        return 0
    return conn.execute(
        "DELETE FROM discovery_job_priority "
        "WHERE job_id IN (SELECT p.job_id FROM discovery_job_priority p "
        "LEFT JOIN jobs j ON j.id=p.job_id "
        "WHERE j.id IS NULL OR j.status NOT IN "
        "('pending','http_inflight','browser_queued','retry_http'))"
    ).rowcount


class DiscoveryPacer:
    """Prefer Discovery, but guarantee a routine job after max_burst.

    With max_burst=4 and Registry work available, dispatch up to four Discovery
    jobs then one Registry job. Telegram and bounded network retries do not
    reset the fairness counter. No per-provider remote requests are made.
    """

    def __init__(self, max_burst=4):
        if not 1 <= max_burst <= 20:
            raise ValueError('discovery max_burst must be 1..20')
        self.max_burst = max_burst
        self.streak = 0

    def allow(self, routine_available):
        return not routine_available or self.streak < self.max_burst

    def record(self, kind):
        if kind == 'discovery':
            self.streak = min(self.streak + 1, self.max_burst)
        elif kind == 'routine':
            self.streak = 0
        elif kind in ('telegram', 'retry', 'none'):
            return
        else:
            raise ValueError('invalid discovery queue kind')
