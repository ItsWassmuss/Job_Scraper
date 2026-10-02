"""Behavioral tests for pure Validator sharded-batch construction."""

import copy
import json

import pytest

from validator.sharding import (
    SCHEMA_VERSION,
    SHARD_MAX_JOBS,
    SHARD_TARGET_BYTES,
    build_sharded_batch,
    global_index_for,
    serialized_json_bytes,
)


def _job(index, *, description=None, status=None, reason=None, validated=None):
    return {
        "company": f"Company {index}",
        "title": f"Title {index}",
        "location": "Remote",
        "url": f"https://example.com/jobs/{index}",
        "description": description if description is not None else f"Description {index}",
        "source": "LinkedIn",
        "job_id": str(1000 + index),
        "status": status,
        "reason": reason,
        "validated": validated,
    }


def _flatten(shards):
    return [job for shard in shards for job in shard["jobs"]]


def test_frozen_default_contract():
    assert SCHEMA_VERSION == 2
    assert SHARD_MAX_JOBS == 10
    assert SHARD_TARGET_BYTES == 48 * 1024


def test_preserves_jobs_order_and_recoverable_global_indexes():
    jobs = [_job(i) for i in range(23)]

    manifest, shards = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
        target_bytes=1024 * 1024,
    )

    assert manifest == {
        "schema_version": 2,
        "batch_id": "20261002-003",
        "created_at": "2026-10-02T05:27:03+03:00",
        "job_count": 23,
        "finalized": False,
        "shards": ["000", "001", "002"],
    }
    assert [shard["start_index"] for shard in shards] == [0, 10, 20]
    assert [shard["job_count"] for shard in shards] == [10, 10, 3]
    assert _flatten(shards) == jobs

    recovered = [
        global_index_for(shard, local_index)
        for shard in shards
        for local_index in range(len(shard["jobs"]))
    ]
    assert recovered == list(range(len(jobs)))


def test_byte_target_splits_before_multi_job_shard_exceeds_target():
    jobs = [_job(i, description="x" * 14_000) for i in range(10)]

    _, shards = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
    )

    assert len(shards) > 1
    assert _flatten(shards) == jobs
    assert all(shard["job_count"] <= SHARD_MAX_JOBS for shard in shards)
    assert all(
        shard["job_count"] == 1 or serialized_json_bytes(shard) <= SHARD_TARGET_BYTES
        for shard in shards
    )


def test_single_oversized_job_is_kept_whole_in_its_own_shard():
    jobs = [
        _job(0, description="x" * 10_000),
        _job(1, description="small"),
    ]

    _, shards = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
        target_bytes=1024,
    )

    assert [shard["job_count"] for shard in shards] == [1, 1]
    assert serialized_json_bytes(shards[0]) > 1024
    assert _flatten(shards) == jobs


def test_preserves_existing_result_fields_exactly():
    jobs = [
        _job(
            0,
            status="QUALIFIED",
            reason="QUALIFIED: no rejection rule applied.",
            validated={"custom": ["preserve", 1]},
        ),
        _job(
            1,
            status="REJECTED",
            reason="TECHNOLOGY_ROLE: example.",
            validated=None,
        ),
    ]

    _, shards = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
    )

    assert _flatten(shards) == jobs


def test_builder_is_deterministic_and_does_not_mutate_input():
    jobs = [_job(i, description="é" * (i + 1) * 1000) for i in range(15)]
    original = copy.deepcopy(jobs)

    first = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
    )
    second = build_sharded_batch(
        batch_id="20261002-003",
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
    )

    assert first == second
    assert jobs == original


def test_utf8_byte_measurement_matches_actual_serialized_bytes():
    payload = {"text": "é漢字"}

    actual = (
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    assert serialized_json_bytes(payload) == len(actual)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"batch_id": "20261002-3"}, "batch_id"),
        ({"created_at": ""}, "created_at"),
        ({"jobs": []}, "jobs"),
        ({"jobs": ["not-an-object"]}, "jobs"),
        ({"max_jobs": 0}, "max_jobs"),
        ({"target_bytes": 0}, "target_bytes"),
    ],
)
def test_invalid_builder_inputs_fail(kwargs, message):
    params = {
        "batch_id": "20261002-003",
        "created_at": "2026-10-02T05:27:03+03:00",
        "jobs": [_job(0)],
    }
    params.update(kwargs)

    with pytest.raises(ValueError, match=message):
        build_sharded_batch(**params)
