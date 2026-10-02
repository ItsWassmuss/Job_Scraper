"""Deterministic read-only selection for Validator open batches."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from validator.batch_storage import (
        FINAL_STATUSES,
        load_manifest,
        load_monolithic,
        load_shard,
        monolithic_path,
        pending_indexes_for_shard,
        relative_manifest_path,
        relative_shard_path,
        resolve_batch_ref,
        validate_batch_id,
        validate_job_statuses,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from batch_storage import (
        FINAL_STATUSES,
        load_manifest,
        load_monolithic,
        load_shard,
        monolithic_path,
        pending_indexes_for_shard,
        relative_manifest_path,
        relative_shard_path,
        resolve_batch_ref,
        validate_batch_id,
        validate_job_statuses,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
OPEN_BATCH_INDEX = Path("validator/open_batches.json")
PATCH_ROOT = Path("validator/patches")

_PATCH_NAME_RE = re.compile(r"^(\d{8})-(\d{3})-(\d{3})\.json$")
_SHARD_ID_RE = re.compile(r"^\d{3}$")


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_open_batch_entries(root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    path = root / OPEN_BATCH_INDEX
    payload = _load_json(path)

    if not isinstance(payload, dict) or set(payload) != {"schema_version", "batches"}:
        raise ValueError(f"{path} must contain exactly schema_version and batches")

    schema_version = payload["schema_version"]
    batches = payload["batches"]
    if not isinstance(batches, list):
        raise ValueError(f"{path} batches must be an array")

    entries: list[dict[str, Any]] = []

    if schema_version == 1:
        for batch_id in batches:
            validate_batch_id(batch_id)
            entries.append({
                "batch_id": batch_id,
                "storage": "monolithic",
                "shard_id": None,
            })
    elif schema_version == 2:
        for position, entry in enumerate(batches):
            if not isinstance(entry, dict) or set(entry) != {
                "batch_id", "storage", "shard_id",
            }:
                raise ValueError(
                    f"{path} batches[{position}] must contain exactly "
                    "batch_id, storage, shard_id"
                )

            batch_id = entry["batch_id"]
            validate_batch_id(batch_id)
            storage = entry["storage"]
            shard_id = entry["shard_id"]

            if storage == "monolithic":
                if shard_id is not None:
                    raise ValueError(
                        f"{path} monolithic entry {batch_id} must have shard_id=null"
                    )
            elif storage == "sharded":
                if not isinstance(shard_id, str) or _SHARD_ID_RE.fullmatch(shard_id) is None:
                    raise ValueError(
                        f"{path} sharded entry {batch_id} must have a NNN shard_id"
                    )
            else:
                raise ValueError(
                    f"{path} entry {batch_id} has invalid storage {storage!r}"
                )

            entries.append({
                "batch_id": batch_id,
                "storage": storage,
                "shard_id": shard_id,
            })
    else:
        raise ValueError(f"{path} schema_version must equal 1 or 2")

    batch_ids = [entry["batch_id"] for entry in entries]
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError(f"{path} contains duplicate batch identifiers")
    if batch_ids != sorted(batch_ids, reverse=True):
        raise ValueError(f"{path} batches must be sorted newest-first")

    return entries


def load_open_batch_ids(root: Path = REPO_ROOT) -> list[str]:
    return [entry["batch_id"] for entry in load_open_batch_entries(root)]


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
    return monolithic_path(root, batch_id)


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
    entries = load_open_batch_entries(root)
    reserved = reserved_batch_ids(root)
    stale_skipped: list[str] = []

    for entry in entries:
        batch_id = entry["batch_id"]
        if batch_id in reserved:
            continue

        ref = resolve_batch_ref(root, batch_id, storage=entry["storage"])

        if ref.storage == "monolithic":
            payload = load_monolithic(ref)
            jobs = payload["jobs"]
            validate_job_statuses(jobs, str(ref.metadata_path))
            pending_indexes = [
                index
                for index, job in enumerate(jobs)
                if job.get("status") is None
            ]

            if payload["finalized"] is True or not pending_indexes:
                stale_skipped.append(batch_id)
                continue

            return {
                "batch_id": batch_id,
                "storage": "monolithic",
                "shard_id": None,
                "batch_path": str(ref.metadata_path.relative_to(root)),
                "logical_batch_path": str(ref.metadata_path.relative_to(root)),
                "pending_indexes": pending_indexes,
                "reserved_batches": sorted(reserved, reverse=True),
                "stale_skipped": stale_skipped,
            }

        manifest = load_manifest(ref)
        if manifest["finalized"] is True:
            stale_skipped.append(batch_id)
            continue

        shard_id = entry["shard_id"]
        if shard_id not in manifest["shards"]:
            raise ValueError(
                f"{root / OPEN_BATCH_INDEX} references undeclared shard "
                f"{batch_id}/{shard_id}"
            )

        shard = load_shard(
            root,
            batch_id,
            shard_id,
            manifest=manifest,
        )
        pending_indexes = pending_indexes_for_shard(shard)
        if not pending_indexes:
            stale_skipped.append(batch_id)
            continue

        return {
            "batch_id": batch_id,
            "storage": "sharded",
            "shard_id": shard_id,
            "batch_path": relative_shard_path(batch_id, shard_id),
            "logical_batch_path": relative_manifest_path(batch_id),
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
