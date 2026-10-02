"""End-to-end behavioral tests for Validator v1/v2 batch compatibility."""

import copy
import json
from pathlib import Path

import pytest

from validator.apply_patch import apply_patches
from validator.finalize import finalize_batches
from validator.select_batch import load_open_batch_entries, select_open_batch
from validator.sharding import build_sharded_batch


def _job(
    index,
    *,
    status=None,
    reason=None,
    validated=None,
):
    return {
        "company": f"Company {index}",
        "title": f"Title {index}",
        "location": "Remote",
        "url": f"https://www.linkedin.com/jobs/view/{1000 + index}",
        "description": f"Description {index}",
        "source": "LinkedIn",
        "job_id": str(1000 + index),
        "status": status,
        "reason": reason,
        "validated": validated,
        "custom": {"preserve": index},
    }


def _write_state(root: Path):
    path = root / "validator/state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen_indeed": [],
        "seen_linkedin": [],
        "reported": [],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_v1(root: Path, batch_id: str, jobs, *, finalized=False):
    day, number = batch_id.split("-")
    path = root / f"validator/batches/{day}/{number}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "created_at": "2026-10-02T05:27:03+03:00",
            "job_count": len(jobs),
            "finalized": finalized,
            "jobs": jobs,
        }, indent=2),
        encoding="utf-8",
    )
    return path


def _write_v2(root: Path, batch_id: str, jobs):
    manifest, shards = build_sharded_batch(
        batch_id=batch_id,
        created_at="2026-10-02T05:27:03+03:00",
        jobs=jobs,
        max_jobs=2,
        hard_max_bytes=1024 * 1024,
    )
    day, number = batch_id.split("-")
    directory = root / f"validator/batches/{day}/{number}"
    directory.mkdir(parents=True, exist_ok=True)

    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    for shard in shards:
        (directory / f"{shard['shard_id']}.json").write_text(
            json.dumps(shard, indent=2),
            encoding="utf-8",
        )
    return manifest_path, shards


def _write_index_v2(root: Path, entries):
    path = root / "validator/open_batches.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 2,
            "batches": entries,
        }, indent=2),
        encoding="utf-8",
    )
    return path


def _write_patch_v2(root: Path, batch_id: str, shard_id: str, results):
    first = results[0]["index"]
    path = root / "validator/patches" / f"{batch_id}-{first:03d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 2,
            "batch_id": batch_id,
            "shard_id": shard_id,
            "results": results,
        }, indent=2),
        encoding="utf-8",
    )
    return path


def _result(job, index):
    return {
        "index": index,
        "source": job["source"],
        "job_id": job["job_id"],
        "url": job["url"],
        "status": "REJECTED",
        "reason": "TECHNOLOGY_ROLE: test rejection.",
        "validated": None,
    }


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema2_selector_reads_only_indexed_shard_and_returns_global_indexes(tmp_path):
    jobs = [_job(i, status="REJECTED", reason="existing") for i in range(2)]
    jobs.extend([_job(2), _job(3), _job(4)])
    _write_v2(tmp_path, "20261002-003", jobs)

    _write_index_v2(
        tmp_path,
        [{
            "batch_id": "20261002-003",
            "storage": "sharded",
            "shard_id": "001",
        }],
    )

    selected = select_open_batch(tmp_path)

    assert selected["batch_id"] == "20261002-003"
    assert selected["storage"] == "sharded"
    assert selected["shard_id"] == "001"
    assert selected["batch_path"] == "validator/batches/20261002/003/001.json"
    assert selected["logical_batch_path"] == (
        "validator/batches/20261002/003/manifest.json"
    )
    assert selected["pending_indexes"] == [2, 3]


def test_schema2_index_rejects_duplicate_or_invalid_entries(tmp_path):
    _write_index_v2(
        tmp_path,
        [
            {
                "batch_id": "20261002-003",
                "storage": "sharded",
                "shard_id": "000",
            },
            {
                "batch_id": "20261002-003",
                "storage": "sharded",
                "shard_id": "001",
            },
        ],
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_open_batch_entries(tmp_path)


def test_v2_patch_mutates_only_target_shard_and_preserves_global_indexes(tmp_path):
    jobs = [_job(i) for i in range(5)]
    _, shards = _write_v2(tmp_path, "20261002-003", jobs)

    shard0 = tmp_path / "validator/batches/20261002/003/000.json"
    shard1 = tmp_path / "validator/batches/20261002/003/001.json"
    shard0_before = shard0.read_bytes()

    patch = _write_patch_v2(
        tmp_path,
        "20261002-003",
        "001",
        [
            _result(shards[1]["jobs"][0], 2),
            _result(shards[1]["jobs"][1], 3),
        ],
    )

    summary = apply_patches(tmp_path)

    assert summary["results_applied"] == 2
    assert summary["consumed_patches"] == 1
    assert not patch.exists()
    assert shard0.read_bytes() == shard0_before

    updated = _read(shard1)
    assert [job["status"] for job in updated["jobs"]] == [
        "REJECTED",
        "REJECTED",
    ]
    assert updated["start_index"] == 2


def test_v2_patch_outside_declared_shard_range_fails_without_mutation(tmp_path):
    jobs = [_job(i) for i in range(5)]
    _, shards = _write_v2(tmp_path, "20261002-003", jobs)
    shard1 = tmp_path / "validator/batches/20261002/003/001.json"
    original = shard1.read_bytes()

    patch = _write_patch_v2(
        tmp_path,
        "20261002-003",
        "001",
        [_result(shards[0]["jobs"][0], 0)],
    )
    patch_bytes = patch.read_bytes()

    with pytest.raises(ValueError, match="outside shard"):
        apply_patches(tmp_path)

    assert shard1.read_bytes() == original
    assert patch.read_bytes() == patch_bytes


def test_finalizer_mixed_storage_builds_schema2_index_with_next_pending_shard(tmp_path):
    _write_state(tmp_path)
    _write_v1(tmp_path, "20261001-013", [_job(0)])

    jobs = [
        _job(10, status="REJECTED", reason="existing"),
        _job(11, status="REJECTED", reason="existing"),
        _job(12),
        _job(13),
        _job(14),
    ]
    manifest_path, _ = _write_v2(tmp_path, "20261002-003", jobs)

    summary = finalize_batches(tmp_path)
    index = _read(tmp_path / "validator/open_batches.json")

    assert summary["pending_batches"] == 2
    assert summary["open_batch_index_schema_version"] == 2
    assert index == {
        "schema_version": 2,
        "batches": [
            {
                "batch_id": "20261002-003",
                "storage": "sharded",
                "shard_id": "001",
            },
            {
                "batch_id": "20261001-013",
                "storage": "monolithic",
                "shard_id": None,
            },
        ],
    }
    assert _read(manifest_path)["finalized"] is False


def test_completed_v2_finalizes_manifest_only_and_records_seen(tmp_path):
    state_path = _write_state(tmp_path)
    jobs = [
        _job(0, status="REJECTED", reason="TECHNOLOGY_ROLE: test."),
        _job(1, status="REJECTED", reason="LANGUAGE: test."),
        _job(2, status="UNVALIDATED", reason="unavailable"),
    ]
    manifest_path, shards = _write_v2(tmp_path, "20261002-003", jobs)
    shard_paths = [
        tmp_path / f"validator/batches/20261002/003/{shard['shard_id']}.json"
        for shard in shards
    ]
    shard_bytes = {path: path.read_bytes() for path in shard_paths}

    summary = finalize_batches(tmp_path)

    assert summary["finalized_files"] == [manifest_path]
    assert _read(manifest_path)["finalized"] is True
    for path, before in shard_bytes.items():
        assert path.read_bytes() == before

    state = _read(state_path)
    assert len(state["seen_linkedin"]) == 2
    assert _read(tmp_path / "validator/open_batches.json") == {
        "schema_version": 2,
        "batches": [],
    }


def test_noncontiguous_v2_shards_fail_before_state_or_manifest_mutation(tmp_path):
    state_path = _write_state(tmp_path)
    jobs = [_job(i, status="REJECTED", reason="existing") for i in range(4)]
    manifest_path, _ = _write_v2(tmp_path, "20261002-003", jobs)
    shard1 = tmp_path / "validator/batches/20261002/003/001.json"
    payload = _read(shard1)
    payload["start_index"] = 3
    shard1.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    state_before = state_path.read_bytes()
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="expected contiguous index"):
        finalize_batches(tmp_path)

    assert state_path.read_bytes() == state_before
    assert manifest_path.read_bytes() == manifest_before


def test_dual_source_of_truth_for_same_logical_batch_is_rejected(tmp_path):
    _write_state(tmp_path)
    jobs = [_job(0)]
    _write_v1(tmp_path, "20261002-003", jobs)
    _write_v2(tmp_path, "20261002-003", jobs)

    with pytest.raises(ValueError, match="both monolithic and sharded"):
        finalize_batches(tmp_path)


def test_shard_result_fields_remain_unmodified_except_apply_fields(tmp_path):
    jobs = [_job(i) for i in range(3)]
    _, shards = _write_v2(tmp_path, "20261002-003", jobs)
    target = shards[1]["jobs"][0]
    original = copy.deepcopy(target)

    _write_patch_v2(
        tmp_path,
        "20261002-003",
        "001",
        [_result(target, 2)],
    )
    apply_patches(tmp_path)

    updated = _read(
        tmp_path / "validator/batches/20261002/003/001.json"
    )["jobs"][0]
    for key, value in original.items():
        if key not in {"status", "reason", "validated"}:
            assert updated[key] == value
