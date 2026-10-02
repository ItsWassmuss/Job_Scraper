"""Deterministic read-only selection for Validator open batches."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
OPEN_BATCH_INDEX = Path("validator/open_batches.json")
PATCH_ROOT = Path("validator/patches")

FINAL_STATUSES = frozenset({"REJECTED", "QUALIFIED", "UNVALIDATED"})
_BATCH_ID_RE = re.compile(r"^\d{8}-\d{3}$")
_PATCH_NAME_RE = re.compile(r"^(\d{8})-(\d{3})-(\d{3})\.json$")


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_open_batch_ids(root: Path = REPO_ROOT) -> list[str]:
    path = root / OPEN_BATCH_INDEX
    payload = _load_json(path)

    if not isinstance(payload, dict) or set(payload) != {"schema_version", "batches"}:
        raise ValueError(f"{path} must contain exactly schema_version and batches")

    if payload["schema_version"] != 1:
        raise ValueError(f"{path} schema_version must equal 1")

    batches = payload["batches"]
    if not isinstance(batches, list):
        raise ValueError(f"{path} batches must be an array")

    if any(not isinstance(batch_id, str) or _BATCH_ID_RE.fullmatch(batch_id) is None for batch_id in batches):
        raise ValueError(f"{path} contains an invalid batch identifier")

    if len(set(batches)) != len(batches):
        raise ValueError(f"{path} contains duplicate batch identifiers")

    if batches != sorted(batches, reverse=True):
        raise ValueError(f"{path} batches must be sorted newest-first")

    return batches


def reserved_batch_ids(root: Path = REPO_ROOT) -> set[str]:
    patch_root = root / PATCH_ROOT

    if not patch_root.exists():
        return set()

    if not patch_root.is_dir():
        raise ValueError(f"{patch_root} must be a directory")

    reserved: set[str] = set()

    for path in sorted(patch_root.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue

        match = _PATCH_NAME_RE.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Invalid Validator patch filename: {path.name}")

        day, batch_number, _ = match.groups()
        reserved.add(f"{day}-{batch_number}")

    return reserved


def batch_path_for_id(root: Path, batch_id: str) -> Path:
    if _BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError(f"Invalid batch identifier: {batch_id!r}")

    day, batch_number = batch_id.split("-", 1)
    return root / "validator/batches" / day / f"{batch_number}.json"


def _load_candidate_batch(path: Path) -> tuple[dict[str, Any], list[int]]:
    if not path.is_file():
        raise FileNotFoundError(path)

    payload = _load_json(path)

    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")

    finalized = payload.get("finalized")
    if finalized not in {True, False}:
        raise ValueError(f"{path} must contain finalized=true or finalized=false")

    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError(f"{path} must contain a jobs array of objects")

    invalid_statuses = [
        job.get("status")
        for job in jobs
        if job.get("status") is not None and job.get("status") not in FINAL_STATUSES
    ]
    if invalid_statuses:
        raise ValueError(f"{path} contains invalid job status {invalid_statuses[0]!r}")

    pending_indexes = [
        index
        for index, job in enumerate(jobs)
        if job.get("status") is None
    ]

    return payload, pending_indexes


def select_open_batch(root: Path = REPO_ROOT) -> dict[str, Any] | None:
    batch_ids = load_open_batch_ids(root)
    reserved = reserved_batch_ids(root)
    stale_skipped: list[str] = []

    for batch_id in batch_ids:
        if batch_id in reserved:
            continue

        path = batch_path_for_id(root, batch_id)
        payload, pending_indexes = _load_candidate_batch(path)

        if payload["finalized"] is True or not pending_indexes:
            stale_skipped.append(batch_id)
            continue

        return {
            "batch_id": batch_id,
            "batch_path": str(path.relative_to(root)),
            "pending_indexes": pending_indexes,
            "reserved_batches": sorted(reserved, reverse=True),
            "stale_skipped": stale_skipped,
        }

    return None


def main() -> int:
    selection = select_open_batch()

    if selection is None:
        print("No eligible unreserved batch.")
        return 0

    print(json.dumps(selection, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
