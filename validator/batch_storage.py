"""Shared storage helpers for Validator logical batches (v1 monolithic and v2 sharded)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FINAL_STATUSES = frozenset({"REJECTED", "QUALIFIED", "UNVALIDATED"})
_BATCH_ID_RE = re.compile(r"^(\d{8})-(\d{3})$")
_DAY_RE = re.compile(r"^\d{8}$")
_NUMBER_RE = re.compile(r"^\d{3}$")
_MONOLITHIC_RE = re.compile(r"^(\d{3})\.json$")
_SHARD_RE = re.compile(r"^\d{3}$")

MANIFEST_KEYS = frozenset({
    "schema_version", "batch_id", "created_at", "job_count", "finalized", "shards",
})
SHARD_KEYS = frozenset({
    "schema_version", "batch_id", "shard_id", "start_index", "job_count", "jobs",
})


@dataclass(frozen=True)
class BatchRef:
    batch_id: str
    storage: str
    metadata_path: Path


def _load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} must contain valid UTF-8 JSON") from exc


def validate_batch_id(batch_id: str) -> tuple[str, str]:
    if not isinstance(batch_id, str):
        raise ValueError("batch_id must be a string matching YYYYMMDD-NNN")
    match = _BATCH_ID_RE.fullmatch(batch_id)
    if match is None:
        raise ValueError(f"Invalid batch identifier: {batch_id!r}")
    return match.groups()


def monolithic_path(root: Path, batch_id: str) -> Path:
    day, number = validate_batch_id(batch_id)
    return root / "validator/batches" / day / f"{number}.json"


def batch_dir(root: Path, batch_id: str) -> Path:
    day, number = validate_batch_id(batch_id)
    return root / "validator/batches" / day / number


def manifest_path(root: Path, batch_id: str) -> Path:
    return batch_dir(root, batch_id) / "manifest.json"


def shard_path(root: Path, batch_id: str, shard_id: str) -> Path:
    if not isinstance(shard_id, str) or _SHARD_RE.fullmatch(shard_id) is None:
        raise ValueError(f"Invalid shard identifier: {shard_id!r}")
    return batch_dir(root, batch_id) / f"{shard_id}.json"


def relative_monolithic_path(batch_id: str) -> str:
    day, number = validate_batch_id(batch_id)
    return f"validator/batches/{day}/{number}.json"


def relative_manifest_path(batch_id: str) -> str:
    day, number = validate_batch_id(batch_id)
    return f"validator/batches/{day}/{number}/manifest.json"


def relative_shard_path(batch_id: str, shard_id: str) -> str:
    day, number = validate_batch_id(batch_id)
    if not isinstance(shard_id, str) or _SHARD_RE.fullmatch(shard_id) is None:
        raise ValueError(f"Invalid shard identifier: {shard_id!r}")
    return f"validator/batches/{day}/{number}/{shard_id}.json"


def resolve_batch_ref(
    root: Path,
    batch_id: str,
    *,
    storage: str | None = None,
) -> BatchRef:
    validate_batch_id(batch_id)
    v1 = monolithic_path(root, batch_id)
    v2_dir = batch_dir(root, batch_id)
    v2_manifest = manifest_path(root, batch_id)

    has_v1 = v1.is_file()
    has_v2_dir = v2_dir.is_dir()
    has_v2 = v2_manifest.is_file()

    if has_v1 and has_v2_dir:
        raise ValueError(f"{batch_id} has both monolithic and sharded storage")
    if has_v2_dir and not has_v2:
        raise FileNotFoundError(v2_manifest)

    detected = "monolithic" if has_v1 else "sharded" if has_v2 else None
    if detected is None:
        if storage == "monolithic":
            raise FileNotFoundError(v1)
        if storage == "sharded":
            raise FileNotFoundError(v2_manifest)
        raise FileNotFoundError(f"No Validator batch storage found for {batch_id}")

    if storage is not None and storage not in {"monolithic", "sharded"}:
        raise ValueError(f"Unsupported batch storage: {storage!r}")
    if storage is not None and detected != storage:
        raise ValueError(
            f"{batch_id} index storage={storage!r} conflicts with repository storage={detected!r}"
        )

    path = v1 if detected == "monolithic" else v2_manifest
    return BatchRef(batch_id=batch_id, storage=detected, metadata_path=path)


def discover_batch_refs(root: Path) -> list[BatchRef]:
    batches_root = root / "validator/batches"
    if not batches_root.is_dir():
        return []

    refs: list[BatchRef] = []
    for day_dir in sorted(batches_root.iterdir()):
        if not day_dir.is_dir() or _DAY_RE.fullmatch(day_dir.name) is None:
            continue

        seen_numbers: set[str] = set()
        for entry in sorted(day_dir.iterdir()):
            number: str | None = None
            storage: str | None = None

            if entry.is_file():
                match = _MONOLITHIC_RE.fullmatch(entry.name)
                if match is not None:
                    number = match.group(1)
                    storage = "monolithic"
            elif entry.is_dir() and _NUMBER_RE.fullmatch(entry.name) is not None:
                number = entry.name
                storage = "sharded"

            if number is None or storage is None:
                continue
            if number in seen_numbers:
                raise ValueError(
                    f"validator/batches/{day_dir.name}/{number} has both monolithic and sharded storage"
                )
            seen_numbers.add(number)
            batch_id = f"{day_dir.name}-{number}"
            refs.append(resolve_batch_ref(root, batch_id, storage=storage))

    return refs


def load_monolithic(ref: BatchRef) -> dict[str, Any]:
    if ref.storage != "monolithic":
        raise ValueError(f"{ref.batch_id} is not monolithic")
    payload = _load_json(ref.metadata_path)
    if not isinstance(payload, dict):
        raise ValueError(f"{ref.metadata_path} must contain a JSON object")

    finalized = payload.get("finalized")
    if finalized not in {True, False}:
        raise ValueError(
            f"{ref.metadata_path} must contain finalized=true or finalized=false"
        )

    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError(f"{ref.metadata_path} must contain a jobs array of objects")

    return payload


def load_manifest(ref: BatchRef) -> dict[str, Any]:
    if ref.storage != "sharded":
        raise ValueError(f"{ref.batch_id} is not sharded")
    payload = _load_json(ref.metadata_path)
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise ValueError(f"{ref.metadata_path} has invalid manifest keys")
    if payload["schema_version"] != 2:
        raise ValueError(f"{ref.metadata_path} schema_version must equal 2")
    if payload["batch_id"] != ref.batch_id:
        raise ValueError(f"{ref.metadata_path} batch_id does not match its path")
    if not isinstance(payload["created_at"], str) or not payload["created_at"]:
        raise ValueError(f"{ref.metadata_path} created_at must be a non-empty string")
    if type(payload["job_count"]) is not int or payload["job_count"] <= 0:
        raise ValueError(f"{ref.metadata_path} job_count must be a positive integer")
    if payload["finalized"] not in {True, False}:
        raise ValueError(f"{ref.metadata_path} finalized must be true or false")

    shards = payload["shards"]
    if (
        not isinstance(shards, list)
        or not shards
        or any(not isinstance(value, str) or _SHARD_RE.fullmatch(value) is None for value in shards)
    ):
        raise ValueError(f"{ref.metadata_path} shards must be a non-empty array of NNN strings")
    expected = [f"{index:03d}" for index in range(len(shards))]
    if shards != expected:
        raise ValueError(f"{ref.metadata_path} shards must be unique contiguous IDs from 000")

    return payload


def load_shard(
    root: Path,
    batch_id: str,
    shard_id: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ref = resolve_batch_ref(root, batch_id, storage="sharded")
    manifest_payload = load_manifest(ref) if manifest is None else manifest
    if shard_id not in manifest_payload["shards"]:
        raise ValueError(f"{batch_id} manifest does not declare shard {shard_id}")

    path = shard_path(root, batch_id, shard_id)
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = _load_json(path)
    if not isinstance(payload, dict) or set(payload) != SHARD_KEYS:
        raise ValueError(f"{path} has invalid shard keys")
    if payload["schema_version"] != 2:
        raise ValueError(f"{path} schema_version must equal 2")
    if payload["batch_id"] != batch_id:
        raise ValueError(f"{path} batch_id does not match its path")
    if payload["shard_id"] != shard_id:
        raise ValueError(f"{path} shard_id does not match its path")
    if type(payload["start_index"]) is not int or payload["start_index"] < 0:
        raise ValueError(f"{path} start_index must be a non-negative integer")

    jobs = payload["jobs"]
    if not isinstance(jobs, list) or not jobs or any(not isinstance(job, dict) for job in jobs):
        raise ValueError(f"{path} jobs must be a non-empty array of objects")
    if type(payload["job_count"]) is not int or payload["job_count"] != len(jobs):
        raise ValueError(f"{path} job_count must equal the jobs array length")

    return payload


def validate_job_statuses(jobs: list[dict[str, Any]], context: str) -> None:
    invalid = [
        job.get("status")
        for job in jobs
        if job.get("status") is not None and job.get("status") not in FINAL_STATUSES
    ]
    if invalid:
        raise ValueError(f"{context} contains invalid job status {invalid[0]!r}")


def pending_indexes_for_shard(shard: dict[str, Any]) -> list[int]:
    jobs = shard["jobs"]
    validate_job_statuses(jobs, f"shard {shard['batch_id']}/{shard['shard_id']}")
    start = shard["start_index"]
    return [
        start + local_index
        for local_index, job in enumerate(jobs)
        if job.get("status") is None
    ]


def load_all_shards(
    root: Path,
    ref: BatchRef,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    manifest_payload = load_manifest(ref) if manifest is None else manifest
    shards: list[dict[str, Any]] = []
    expected_start = 0

    for shard_id in manifest_payload["shards"]:
        shard = load_shard(
            root,
            ref.batch_id,
            shard_id,
            manifest=manifest_payload,
        )
        if shard["start_index"] != expected_start:
            raise ValueError(
                f"{ref.batch_id} shard {shard_id} start_index={shard['start_index']} "
                f"does not equal expected contiguous index {expected_start}"
            )
        validate_job_statuses(
            shard["jobs"],
            f"{ref.batch_id} shard {shard_id}",
        )
        expected_start += shard["job_count"]
        shards.append(shard)

    if expected_start != manifest_payload["job_count"]:
        raise ValueError(
            f"{ref.metadata_path} job_count does not equal total shard jobs"
        )

    return shards


def first_pending_shard(shards: list[dict[str, Any]]) -> str | None:
    for shard in shards:
        if any(job.get("status") is None for job in shard["jobs"]):
            return shard["shard_id"]
    return None


def flatten_shards(shards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for shard in shards for job in shard["jobs"]]
