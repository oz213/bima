"""Read-only counters scoped to the current published Registry snapshot.

Old jobs and alert/review history remain in SQLite. When a Registry snapshot is
active, report *only* URLs belonging to that snapshot, not historical jobs.
"""


def active_cycle(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='registry_snapshot_active_v2'"
    ).fetchone()
    if not exists:
        return None
    row = conn.execute(
        'SELECT cycle_id,ready FROM registry_snapshot_active_v2 WHERE singleton=1'
    ).fetchone()
    if not row:
        return None
    if int(row[1]) != 1:
        raise RuntimeError('Registry snapshot is not ready; counts withheld')
    return str(row[0])


def jobs_source(conn):
    """SQL FROM fragment with jobs aliased as j; returns active-cycle ID."""
    cycle = active_cycle(conn)
    if cycle is None:
        return 'jobs j', None
    return ('registry_snapshot_queue_v2 q '
            'JOIN jobs j ON j.id=q.job_id AND q.blocked=0'), cycle


def current_counts(conn):
    source, cycle = jobs_source(conn)
    if cycle is None:
        rows = conn.execute(
            'SELECT j.status,COUNT(*),'
            'COALESCE(SUM(CASE WHEN j.attempts>0 THEN 1 ELSE 0 END),0) '
            'FROM jobs j GROUP BY j.status'
        ).fetchall()
    else:
        # Protected historical alerts/reviews remain stored, but do NOT count
        # as alerts, completed work, or attempts in the NEW dataset.
        rows = conn.execute(
            "SELECT CASE WHEN q.blocked=1 THEN 'protected_previous' ELSE j.status END AS visible_status, "
            'COUNT(*),COALESCE(SUM(CASE WHEN q.blocked=0 AND j.attempts>0 THEN 1 ELSE 0 END),0) '
            'FROM registry_snapshot_queue_v2 q JOIN jobs j ON j.id=q.job_id '
            'GROUP BY visible_status'
        ).fetchall()
    counts = {str(row[0]): int(row[1]) for row in rows}
    total = sum(counts.values())
    attempted = sum(int(row[2]) for row in rows)
    if cycle is not None:
        expected_row = conn.execute(
            'SELECT r.url_count FROM registry_snapshot_active_v2 a '
            'JOIN registry_snapshot_runs_v2 r ON r.cycle_id=a.cycle_id '
            'WHERE a.singleton=1 AND a.ready=1'
        ).fetchone()
        if not expected_row or total != int(expected_row[0]):
            raise RuntimeError('Snapshot totals inconsistent; refusing stale progress')
    return counts, total, attempted, cycle
