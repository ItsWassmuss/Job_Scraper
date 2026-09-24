#!/usr/bin/env python3
"""Build and optionally send the hourly Validator status report.

This reporter is read-only. It reads today's Validator batch files, computes
status counts deterministically, and sends the approved compact report through
the existing Pushover transport in notify.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
BATCH_ROOT = REPO_ROOT / "validator" / "batches"
TIMEZONE = ZoneInfo("Europe/Helsinki")
FINAL_STATUSES = {"QUALIFIED", "REJECTED", "UNVALIDATED"}

# Reuse only the existing transport function; reporting logic stays isolated.
sys.path.insert(0, str(REPO_ROOT))
from notify import send_pushover  # noqa: E402


def load_today_stats(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(TIMEZONE)
    day = now.strftime("%Y%m%d")
    day_dir = BATCH_ROOT / day

    stats = {
        "jobs": 0,
        "qualified": 0,
        "rejected": 0,
        "unvalidated": 0,
        "processed": 0,
        "pending_jobs": 0,
        "batches": 0,
        "finalized_batches": 0,
        "pending_batches": 0,
        "not_finalized_batches": 0,
    }

    if not day_dir.exists():
        return stats
    if not day_dir.is_dir():
        raise RuntimeError(f"Today's batch path is not a directory: {day_dir}")

    batch_paths = sorted(day_dir.glob("[0-9][0-9][0-9].json"))
    stats["batches"] = len(batch_paths)

    for path in batch_paths:
        with path.open(encoding="utf-8") as f:
            batch = json.load(f)

        jobs = batch.get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError(f"{path}: top-level jobs must be a list")

        declared_count = batch.get("job_count")
        if declared_count != len(jobs):
            raise RuntimeError(
                f"{path}: job_count={declared_count!r} does not match jobs={len(jobs)}"
            )

        finalized = batch.get("finalized")
        if not isinstance(finalized, bool):
            raise RuntimeError(f"{path}: finalized must be boolean")

        batch_pending = 0
        for index, job in enumerate(jobs):
            if not isinstance(job, dict):
                raise RuntimeError(f"{path}: jobs[{index}] must be an object")

            status = job.get("status")
            if status is None:
                stats["pending_jobs"] += 1
                batch_pending += 1
                continue

            if status not in FINAL_STATUSES:
                raise RuntimeError(
                    f"{path}: jobs[{index}] has unexpected status {status!r}"
                )

            stats["processed"] += 1
            if status == "QUALIFIED":
                stats["qualified"] += 1
            elif status == "REJECTED":
                stats["rejected"] += 1
            else:
                stats["unvalidated"] += 1

        stats["jobs"] += len(jobs)

        if finalized:
            if batch_pending:
                raise RuntimeError(
                    f"{path}: finalized=true but {batch_pending} pending job(s) remain"
                )
            stats["finalized_batches"] += 1
        elif batch_pending:
            stats["pending_batches"] += 1
        else:
            stats["not_finalized_batches"] += 1

    if stats["processed"] + stats["pending_jobs"] != stats["jobs"]:
        raise RuntimeError("Internal count mismatch: processed + pending != jobs")

    return stats


def build_report(stats: dict[str, int], now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(TIMEZONE)
    title = f"Job Scraper Status — {now:%H:%M}"
    message = "\n".join(
        [
            "Today",
            f"Jobs: {stats['jobs']}",
            (
                f"Qualified: {stats['qualified']} | "
                f"Rejected: {stats['rejected']} | "
                f"Unvalidated: {stats['unvalidated']}"
            ),
            f"Processed: {stats['processed']} | Pending: {stats['pending_jobs']}",
            "",
            "Batches",
            f"Today: {stats['batches']}",
            (
                f"Finalized: {stats['finalized_batches']} | "
                f"Pending: {stats['pending_batches']} | "
                f"Not finalized: {stats['not_finalized_batches']}"
            ),
        ]
    )
    return title, message


def main() -> int:
    parser = argparse.ArgumentParser(description="Validator status reporter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report without sending Pushover",
    )
    args = parser.parse_args()

    now = datetime.now(TIMEZONE)
    stats = load_today_stats(now)
    title, message = build_report(stats, now)

    if args.dry_run:
        print(title)
        print()
        print(message)
        return 0

    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        print("PUSHOVER_TOKEN and PUSHOVER_USER are required.", file=sys.stderr)
        return 2

    ok = send_pushover(
        token,
        user,
        title=title,
        message=message,
        priority=0,
    )
    if not ok:
        print("Pushover status report send failed.", file=sys.stderr)
        return 1

    print(title)
    print(message)
    print("Pushover status report sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
