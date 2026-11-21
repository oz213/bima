"""Offline operator CLI for BIMA's human-supervised review pipeline.

Run: PYTHONPATH=multi python3 -m learning.cli --help
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .core import (
    attach_evidence_text, export_dataset, list_cases, review_case, review_stats,
    seed_review_queue,
)


def make_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m learning.cli",
        description="BIMA offline review and candidate learning",
    )
    parser.add_argument(
        "--learning-db", default="multi/state/bima_learning_v450.sqlite3",
        help="Separate learning SQLite database (never production crawler DB)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("seed", help="Import review candidates from production read-only")
    sync.add_argument("--production-db", default="multi/state/crawler_production.sqlite3")
    sync.add_argument("--limit", type=int, default=5000, help="Recent alerted jobs to scan (1..50000)")

    queue = sub.add_parser("list", help="List review case metadata without page text")
    queue.add_argument("--limit", type=int, default=30)
    queue.add_argument("--all", action="store_true", help="Include already reviewed")

    attach = sub.add_parser("attach-text", help="Attach analyst-approved UTF-8 text evidence")
    attach.add_argument("--case", type=int, required=True)
    attach.add_argument("--file", type=Path, required=True, help="Existing local UTF-8 text file")
    attach.add_argument("--origin", choices=(
        "manual_capture", "approved_archive", "screenshot_transcript",
    ), required=True)

    review = sub.add_parser("review", help="Human label; final labels require attached text")
    review.add_argument("--case", type=int, required=True)
    review.add_argument("--label", choices=(
        "true_positive", "false_positive", "needs_review",
    ), required=True)
    review.add_argument("--text-label", choices=(
        "gambling", "non_gambling", "not_for_training",
    ), help="Explicit text-specific label, separate from incident verdict; required for final review")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", required=True)
    review.add_argument("--domain-group", help="Explicit registrable .id domain for split leakage prevention")

    sub.add_parser("stats", help="Count review labels and candidate types")

    export = sub.add_parser("export", help="Export VERIFIED evidence only (no model update)")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--seed", default="bima-v450")
    export.add_argument("--min-total", type=int, default=100)
    export.add_argument("--min-per-class", type=int, default=25)
    export.add_argument("--min-groups", type=int, default=20)
    learn = sub.add_parser("learn", help="Fit and evaluate a candidate from verified labels")
    learn.add_argument("--production-db", help="Optionally seed review cases read-only before learning")
    learn.add_argument("--runs", type=Path, default=Path("multi/state/learning-runs"))
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if args.command == "seed":
            result = seed_review_queue(args.production_db, args.learning_db, args.limit)
        elif args.command == "list":
            result = list_cases(args.learning_db, args.limit, args.all)
        elif args.command == "attach-text":
            if args.file.is_symlink() or not args.file.is_file():
                raise ValueError("Evidence must be a regular local file, not a symlink")
            if args.file.stat().st_size > 480000:
                raise ValueError("Evidence file too large")
            text = args.file.read_text(encoding="utf-8")
            result = attach_evidence_text(args.learning_db, args.case, text, args.origin)
        elif args.command == "review":
            result = review_case(
                args.learning_db, args.case, args.label,
                args.reviewer, args.note, args.domain_group, args.text_label,
            )
        elif args.command == "learn":
            from .candidate import learn
            seeded = None
            if args.production_db:
                seeded = seed_review_queue(args.production_db, args.learning_db)
            result = learn(args.learning_db, args.runs)
            if seeded is not None:
                result["seed"] = seeded
        elif args.command == "stats":
            result = review_stats(args.learning_db)
        else:
            result = export_dataset(
                args.learning_db, args.output, args.seed,
                args.min_total, args.min_per_class, args.min_groups,
            )
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        # Never leak evidence text in a traceback; report concise operator error.
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
