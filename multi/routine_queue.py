"""Routine job cursor helpers for long-lived interactive BIMA crawler.

A routine job may be requeued after the main loop already moved past its ID.
Interactive mode must wrap around when no pending ID above the cursor exists.
"""
from __future__ import annotations


def find_next_pending_job(conn, last_id: int, *, wrap: bool):
    """Return (id,url) of the next pending job, or None; no writes."""
    row = conn.execute(
        "SELECT id,url FROM jobs WHERE status='pending' AND id>? ORDER BY id LIMIT 1",
        (last_id,),
    ).fetchone()
    if row is None and wrap:
        row = conn.execute(
            "SELECT id,url FROM jobs WHERE status='pending' AND id<=? ORDER BY id LIMIT 1",
            (last_id,),
        ).fetchone()
    return row
