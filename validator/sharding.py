"""Pure deterministic helpers for Validator sharded-batch construction."""

from __future__ import annotations

import copy
import json
import re
from typing import Any


SCHEMA_VERSION = 2
SHARD_MAX_JOBS = 10
SHARD_TARGET_BYTES = 48 * 1024

_BATCH_ID_RE = re.compile(r"^\d{8}-\d{3}$")


def serialize_json(payload: Any) -> str:
    """Serialize exactly as sharded batch files are intended to be persisted."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def serialized_json_bytes(payload: Any) -> int:
    return len(serialize_json(payload).encode("utf-8"))


def _validate_inputs(
    batch_id: str,
    created_at: str,
    jobs: list[dict[str, Any]],
    max_jobs: int,
    target_bytes: int,
) -> None:
    if not isinstance(batch_id, str) or _BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("batch_id must match YYYYMMDD-NNN")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("created_at must be a non-empty string")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs must be a non-empty list")
    if any(not isinstance(job, dict) for job in jobs):
        raise ValueError("jobs must contain only objects")
    if type(max_jobs) is not int or max_jobs <= 0:
        raise ValueError("max_jobs must be a positive integer")
    if type(target_bytes) is not int or target_bytes <= 0:
        raise ValueError("target_bytes must be a positive integer")


def _shard_payload(
    *,
    batch_id: str,
    shard_number: int,
    start_index: int,
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "shard_id": f"{shard_number:03d}",
        "start_index": start_index,
        "job_count": len(jobs),
        "jobs": copy.deepcopy(jobs),
    }


def build_sharded_batch(
    *,
    batch_id: str,
    created_at: str,
    jobs: list[dict[str, Any]],
    max_jobs: int = SHARD_MAX_JOBS,
    target_bytes: int = SHARD_TARGET_BYTES,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split one logical batch into deterministic bounded shard payloads.

    The byte target is enforced for every multi-job shard. A single job is
    never split, so a single-job shard may exceed target_bytes.
    """
    _validate_inputs(batch_id, created_at, jobs, max_jobs, target_bytes)

    shards: list[dict[str, Any]] = []
    current_jobs: list[dict[str, Any]] = []
    current_start = 0

    def flush() -> None:
        nonlocal current_jobs, current_start
        if not current_jobs:
            return
        shards.append(
            _shard_payload(
                batch_id=batch_id,
                shard_number=len(shards),
                start_index=current_start,
                jobs=current_jobs,
            )
        )
        current_jobs = []

    for global_index, job in enumerate(jobs):
        if not current_jobs:
            current_start = global_index
            current_jobs = [copy.deepcopy(job)]
            continue

        candidate_jobs = current_jobs + [copy.deepcopy(job)]
        candidate = _shard_payload(
            batch_id=batch_id,
            shard_number=len(shards),
            start_index=current_start,
            jobs=candidate_jobs,
        )

        if (
            len(candidate_jobs) > max_jobs
            or serialized_json_bytes(candidate) > target_bytes
        ):
            flush()
            current_start = global_index
            current_jobs = [copy.deepcopy(job)]
        else:
            current_jobs = candidate_jobs

    flush()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at": created_at,
        "job_count": len(jobs),
        "finalized": False,
        "shards": [shard["shard_id"] for shard in shards],
    }

    return manifest, shards


def global_index_for(shard: dict[str, Any], local_index: int) -> int:
    if type(local_index) is not int or local_index < 0:
        raise ValueError("local_index must be a non-negative integer")
    start_index = shard.get("start_index")
    jobs = shard.get("jobs")
    if type(start_index) is not int or start_index < 0:
        raise ValueError("shard start_index must be a non-negative integer")
    if not isinstance(jobs, list):
        raise ValueError("shard jobs must be a list")
    if local_index >= len(jobs):
        raise IndexError("local_index is outside shard jobs")
    return start_index + local_index
