"""Behavioral tests for deterministic Validator patch application."""

import copy
import json
from pathlib import Path

import pytest

import validator.apply_patch as patcher
import validator.finalize as finalizer
from validator.apply_patch import apply_patches


def _validated(**overrides):
    value = {
        "work_mode": "Remote",
        "required_experience": "5+ years",
        "date_posted": "3 hours ago",
        "date_posted_at": "2026-09-21T10:00:00+03:00",
        "date_posted_date": None,
        "date_posted_precision": "relative",
        "work_authorization": "Not specified",
        "residence_requirement": "Not specified",
        "short_description": "Backend .NET role",
        "direct_application_link": "https://example.com/apply",
    }
    value.update(overrides)
    return value


def _job(
    index,
    *,
    source="LinkedIn",
    job_id=None,
    status=None,
    reason=None,
    validated=None,
):
    if job_id is None:
        job_id = str(1000 + index)

    return {
        "company": f"Company {index}",
        "title": f"Title {index}",
        "location": "Remote",
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "description": f"Description {index}",
        "source": source,
        "job_id": job_id,
        "status": status,
        "reason": reason,
        "validated": validated,
        "custom": {"keep": index},
    }


def _write_batch(
    root: Path,
    jobs,
    *,
    day="20260921",
    number="003",
    finalized=False,
):
    path = root / f"validator/batches/{day}/{number}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "created_at": "2026-09-21T04:54:24+03:00",
        "job_count": len(jobs),
        "finalized": finalized,
        "jobs": jobs,
        "custom_top": {"preserve": True},
    }

    path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return path


def _result(
    job,
    index,
    *,
    status="REJECTED",
    reason="TECHNOLOGY_ROLE: not backend .NET.",
    validated=None,
):
    return {
        "index": index,
        "source": job["source"],
        "job_id": job["job_id"],
        "url": job["url"],
        "status": status,
        "reason": reason,
        "validated": validated,
    }


def _write_patch(
    root: Path,
    name: str,
    batch: str,
    results,
    *,
    extra=None,
):
    path = root / "validator/patches" / name
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "batch": batch,
        "results": results,
    }
    if extra:
        payload.update(extra)

    path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return path


def _read(path):
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def test_applies_patch_preserves_unrelated_content_and_consumes_patch(tmp_path):
    jobs = [_job(i) for i in range(3)]
    original = copy.deepcopy(jobs)
    batch_path = _write_batch(tmp_path, jobs)
    patch_path = _write_patch(
        tmp_path,
        "20260921-003-001.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[1], 1)],
    )

    summary = apply_patches(tmp_path)
    batch = _read(batch_path)

    assert summary == {
        "patches_discovered": 1,
        "patches_applied": 1,
        "patches_idempotent": 0,
        "results_applied": 1,
        "results_already_applied": 0,
        "consumed_patches": 1,
    }
    assert not patch_path.exists()
    assert batch["custom_top"] == {"preserve": True}
    assert batch["jobs"][0] == original[0]
    assert batch["jobs"][2] == original[2]
    assert batch["jobs"][1]["status"] == "REJECTED"
    assert batch["jobs"][1]["reason"] == "TECHNOLOGY_ROLE: not backend .NET."
    assert batch["jobs"][1]["validated"] is None
    assert batch["jobs"][1]["custom"] == original[1]["custom"]


def test_sparse_indexes_are_allowed_when_sorted_and_filename_matches_first(tmp_path):
    jobs = [_job(i) for i in range(6)]
    batch_path = _write_batch(tmp_path, jobs)

    _write_patch(
        tmp_path,
        "20260921-003-001.json",
        "validator/batches/20260921/003.json",
        [
            _result(jobs[1], 1),
            _result(jobs[4], 4),
        ],
    )

    summary = apply_patches(tmp_path)
    batch = _read(batch_path)

    assert summary["results_applied"] == 2
    assert [
        batch["jobs"][i]["status"]
        for i in range(6)
    ] == [
        None,
        "REJECTED",
        None,
        None,
        "REJECTED",
        None,
    ]


def test_qualified_and_unvalidated_contracts_apply(tmp_path):
    jobs = [_job(0), _job(1)]
    batch_path = _write_batch(tmp_path, jobs)

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [
            _result(
                jobs[0],
                0,
                status="QUALIFIED",
                reason="QUALIFIED: no rejection rule applied.",
                validated=_validated(),
            ),
            _result(
                jobs[1],
                1,
                status="UNVALIDATED",
                reason="SUBSTANTIVE_EVIDENCE_UNAVAILABLE",
            ),
        ],
    )

    apply_patches(tmp_path)
    batch = _read(batch_path)

    assert batch["jobs"][0]["validated"] == _validated()
    assert batch["jobs"][1]["validated"] is None


def test_idempotent_replay_consumes_patch_without_rewriting_batch(tmp_path):
    validated = _validated()
    jobs = [
        _job(
            0,
            status="QUALIFIED",
            reason="QUALIFIED: no rejection rule applied.",
            validated=validated,
        )
    ]
    batch_path = _write_batch(tmp_path, jobs)
    original_bytes = batch_path.read_bytes()

    patch_path = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [
            _result(
                jobs[0],
                0,
                status="QUALIFIED",
                reason="QUALIFIED: no rejection rule applied.",
                validated=validated,
            )
        ],
    )

    summary = apply_patches(tmp_path)

    assert summary["patches_idempotent"] == 1
    assert summary["results_already_applied"] == 1
    assert batch_path.read_bytes() == original_bytes
    assert not patch_path.exists()


def test_entire_queue_validates_before_any_repository_mutation(tmp_path):
    jobs_a = [_job(0)]
    jobs_b = [
        _job(
            0,
            status="REJECTED",
            reason="existing",
            validated=None,
        )
    ]

    batch_a = _write_batch(
        tmp_path,
        jobs_a,
        number="003",
    )
    batch_b = _write_batch(
        tmp_path,
        jobs_b,
        number="004",
    )
    patch_a = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs_a[0], 0)],
    )
    conflicting = _result(
        jobs_b[0],
        0,
        reason="different",
    )
    patch_b = _write_patch(
        tmp_path,
        "20260921-004-000.json",
        "validator/batches/20260921/004.json",
        [conflicting],
    )

    originals = {
        path: path.read_bytes()
        for path in (
            batch_a,
            batch_b,
            patch_a,
            patch_b,
        )
    }

    with pytest.raises(
        ValueError,
        match="conflicts with existing result",
    ):
        apply_patches(tmp_path)

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_overlapping_queue_targets_fail_before_mutation(tmp_path):
    jobs = [_job(0), _job(1)]
    batch = _write_batch(tmp_path, jobs)

    p1 = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [
            _result(jobs[0], 0),
            _result(jobs[1], 1),
        ],
    )
    p2 = _write_patch(
        tmp_path,
        "20260921-003-001.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[1], 1)],
    )

    originals = {
        path: path.read_bytes()
        for path in (
            batch,
            p1,
            p2,
        )
    }

    with pytest.raises(
        ValueError,
        match="Overlapping Validator patch target",
    ):
        apply_patches(tmp_path)

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_identity_mismatch_fails_without_mutation(tmp_path):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)

    result = _result(jobs[0], 0)
    result["job_id"] = "wrong"

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [result],
    )

    originals = (
        batch.read_bytes(),
        patch.read_bytes(),
    )

    with pytest.raises(
        ValueError,
        match="identity mismatch",
    ):
        apply_patches(tmp_path)

    assert (
        batch.read_bytes(),
        patch.read_bytes(),
    ) == originals


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "line1\nline2",
        "x" * 301,
    ],
)
def test_invalid_reason_is_rejected(tmp_path, reason):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [
            _result(
                jobs[0],
                0,
                reason=reason,
            )
        ],
    )

    originals = (
        batch.read_bytes(),
        patch.read_bytes(),
    )

    with pytest.raises(ValueError):
        apply_patches(tmp_path)

    assert (
        batch.read_bytes(),
        patch.read_bytes(),
    ) == originals


def test_patch_over_size_limit_is_rejected_before_json_parse(tmp_path):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[0], 0)],
    )
    patch.write_bytes(
        patch.read_bytes()
        + b" " * (patcher.MAX_PATCH_BYTES + 1)
    )

    batch_bytes = batch.read_bytes()
    patch_bytes = patch.read_bytes()

    with pytest.raises(
        ValueError,
        match="patch size limit",
    ):
        apply_patches(tmp_path)

    assert batch.read_bytes() == batch_bytes
    assert patch.read_bytes() == patch_bytes


@pytest.mark.parametrize(
    "name",
    [
        "bad.json",
        "20260921-03-000.json",
        "20260921-003-00.json",
    ],
)
def test_invalid_json_filename_fails(name, tmp_path):
    patches = tmp_path / "validator/patches"
    patches.mkdir(parents=True)

    path = patches / name
    path.write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Invalid Validator patch filename",
    ):
        apply_patches(tmp_path)


def test_wrong_batch_for_filename_fails(tmp_path):
    jobs = [_job(0)]
    _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/004.json",
        [_result(jobs[0], 0)],
    )

    with pytest.raises(
        ValueError,
        match="batch does not match",
    ):
        apply_patches(tmp_path)

    assert patch.exists()


@pytest.mark.parametrize(
    "extra_at",
    [
        "top",
        "result",
    ],
)
def test_exact_key_contract_rejects_extra_fields(tmp_path, extra_at):
    jobs = [_job(0)]
    _write_batch(tmp_path, jobs)

    result = _result(jobs[0], 0)
    if extra_at == "result":
        result["extra"] = True
        extra = None
    else:
        extra = {"extra": True}

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [result],
        extra=extra,
    )

    with pytest.raises(
        ValueError,
        match="invalid",
    ):
        apply_patches(tmp_path)


@pytest.mark.parametrize(
    "indexes",
    [
        [1, 0],
        [0, 0],
    ],
)
def test_indexes_must_be_strictly_ascending_and_unique(tmp_path, indexes):
    jobs = [_job(0), _job(1)]
    _write_batch(tmp_path, jobs)

    results = [
        _result(
            jobs[index],
            index,
        )
        for index in indexes
    ]

    _write_patch(
        tmp_path,
        f"20260921-003-{indexes[0]:03d}.json",
        "validator/batches/20260921/003.json",
        results,
    )

    with pytest.raises(
        ValueError,
        match="strictly ascending",
    ):
        apply_patches(tmp_path)


def test_filename_index_must_match_first_result(tmp_path):
    jobs = [_job(0), _job(1)]
    _write_batch(tmp_path, jobs)

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[1], 1)],
    )

    with pytest.raises(
        ValueError,
        match="filename index",
    ):
        apply_patches(tmp_path)


def test_index_out_of_range_fails_without_mutation(tmp_path):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)

    fake = _job(9)
    patch = _write_patch(
        tmp_path,
        "20260921-003-009.json",
        "validator/batches/20260921/003.json",
        [_result(fake, 9)],
    )

    originals = (
        batch.read_bytes(),
        patch.read_bytes(),
    )

    with pytest.raises(
        ValueError,
        match="outside the target batch",
    ):
        apply_patches(tmp_path)

    assert (
        batch.read_bytes(),
        patch.read_bytes(),
    ) == originals


def test_invalid_qualified_validated_shape_fails(tmp_path):
    jobs = [_job(0)]
    _write_batch(tmp_path, jobs)

    result = _result(
        jobs[0],
        0,
        status="QUALIFIED",
        reason="QUALIFIED: no rejection rule applied.",
        validated={"bad": True},
    )

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [result],
    )

    with pytest.raises(
        ValueError,
        match="validated keys",
    ):
        apply_patches(tmp_path)


def test_finalized_batch_cannot_be_patched(tmp_path):
    jobs = [_job(0)]
    _write_batch(
        tmp_path,
        jobs,
        finalized=True,
    )

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[0], 0)],
    )

    with pytest.raises(
        ValueError,
        match="finalized=false",
    ):
        apply_patches(tmp_path)


def test_pending_job_with_existing_result_data_is_rejected(tmp_path):
    jobs = [
        _job(
            0,
            status=None,
            reason="unexpected",
        )
    ]
    _write_batch(tmp_path, jobs)

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[0], 0)],
    )

    with pytest.raises(
        ValueError,
        match="pending job must have reason=null and validated=null",
    ):
        apply_patches(tmp_path)


def test_missing_patch_directory_is_noop(tmp_path):
    summary = apply_patches(tmp_path)

    assert summary["patches_discovered"] == 0
    assert summary["consumed_patches"] == 0


def test_more_than_ten_results_in_one_patch_is_rejected(tmp_path):
    jobs = [_job(i) for i in range(11)]
    batch = _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(job, index) for index, job in enumerate(jobs)],
    )

    originals = (batch.read_bytes(), patch.read_bytes())

    with pytest.raises(ValueError, match="1..10"):
        apply_patches(tmp_path)

    assert (batch.read_bytes(), patch.read_bytes()) == originals


@pytest.mark.parametrize(
    ("field", "limit"),
    sorted(patcher.VALIDATED_STRING_MAX_CHARS.items()),
)
def test_validated_string_limits_are_enforced(tmp_path, field, limit):
    jobs = [_job(0)]
    _write_batch(tmp_path, jobs)

    validated = _validated()
    validated[field] = "x" * (limit + 1)
    if field == "direct_application_link":
        validated[field] = "https://example.com/" + "x" * limit

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [
            _result(
                jobs[0],
                0,
                status="QUALIFIED",
                reason="QUALIFIED: no rejection rule applied.",
                validated=validated,
            )
        ],
    )

    with pytest.raises(ValueError, match="exceeds"):
        apply_patches(tmp_path)


def test_job_id_length_limit_is_enforced(tmp_path):
    jobs = [_job(0)]
    _write_batch(tmp_path, jobs)

    result = _result(jobs[0], 0)
    result["job_id"] = "x" * (patcher.MAX_JOB_ID_CHARS + 1)

    _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [result],
    )

    with pytest.raises(ValueError, match="job_id exceeds"):
        apply_patches(tmp_path)


def test_queue_file_count_limit_is_enforced_before_parsing(tmp_path):
    patches = tmp_path / "validator/patches"
    patches.mkdir(parents=True)

    for index in range(patcher.MAX_PATCH_FILES + 1):
        (patches / f"20260921-{index:03d}-000.json").write_text(
            "{}",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="queue exceeds"):
        apply_patches(tmp_path)


def test_queue_byte_limit_is_enforced_before_parsing(tmp_path):
    patches = tmp_path / "validator/patches"
    patches.mkdir(parents=True)

    per_file = patcher.MAX_PATCH_BYTES - 1
    file_count = patcher.MAX_QUEUE_BYTES // per_file + 1

    for index in range(file_count):
        (patches / f"20260921-{index:03d}-000.json").write_bytes(
            b"{" + b" " * (per_file - 2) + b"}"
        )

    with pytest.raises(ValueError, match="queue exceeds"):
        apply_patches(tmp_path)


@pytest.mark.parametrize(
    "bad_status",
    ["BROKEN", "", 123],
)
def test_malformed_existing_batch_status_blocks_all_mutation(tmp_path, bad_status):
    jobs = [_job(0), _job(1)]
    jobs[0]["status"] = bad_status
    jobs[0]["reason"] = "existing"
    batch = _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-001.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[1], 1)],
    )

    originals = (batch.read_bytes(), patch.read_bytes())

    with pytest.raises(ValueError, match="invalid status"):
        apply_patches(tmp_path)

    assert (batch.read_bytes(), patch.read_bytes()) == originals


def test_malformed_existing_final_result_blocks_all_mutation(tmp_path):
    jobs = [
        _job(
            0,
            status="QUALIFIED",
            reason="QUALIFIED: no rejection rule applied.",
            validated={"bad": True},
        ),
        _job(1),
    ]
    batch = _write_batch(tmp_path, jobs)

    patch = _write_patch(
        tmp_path,
        "20260921-003-001.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[1], 1)],
    )

    originals = (batch.read_bytes(), patch.read_bytes())

    with pytest.raises(ValueError, match="validated keys"):
        apply_patches(tmp_path)

    assert (batch.read_bytes(), patch.read_bytes()) == originals


def test_batch_job_count_must_match_jobs_length(tmp_path):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)
    payload = _read(batch)
    payload["job_count"] = 2
    batch.write_text(json.dumps(payload), encoding="utf-8")

    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[0], 0)],
    )

    originals = (batch.read_bytes(), patch.read_bytes())

    with pytest.raises(ValueError, match="job_count"):
        apply_patches(tmp_path)

    assert (batch.read_bytes(), patch.read_bytes()) == originals


def test_second_batch_write_failure_keeps_patches_for_recovery(tmp_path, monkeypatch):
    jobs_a = [_job(0)]
    jobs_b = [_job(0)]

    batch_a = _write_batch(tmp_path, jobs_a, number="003")
    batch_b = _write_batch(tmp_path, jobs_b, number="004")
    patch_a = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs_a[0], 0)],
    )
    patch_b = _write_patch(
        tmp_path,
        "20260921-004-000.json",
        "validator/batches/20260921/004.json",
        [_result(jobs_b[0], 0)],
    )

    real_write = patcher._atomic_write_json

    def fail_second(path, payload):
        if path == batch_b:
            raise OSError("second batch write failed")
        real_write(path, payload)

    monkeypatch.setattr(patcher, "_atomic_write_json", fail_second)

    with pytest.raises(OSError, match="second batch write failed"):
        apply_patches(tmp_path)

    assert _read(batch_a)["jobs"][0]["status"] == "REJECTED"
    assert _read(batch_b)["jobs"][0]["status"] is None
    assert patch_a.exists()
    assert patch_b.exists()

    monkeypatch.setattr(patcher, "_atomic_write_json", real_write)
    summary = apply_patches(tmp_path)

    assert summary["results_already_applied"] == 1
    assert summary["results_applied"] == 1
    assert _read(batch_b)["jobs"][0]["status"] == "REJECTED"
    assert not patch_a.exists()
    assert not patch_b.exists()


def test_patch_delete_failure_raises_after_batch_write(tmp_path, monkeypatch):
    jobs = [_job(0)]
    batch = _write_batch(tmp_path, jobs)
    patch = _write_patch(
        tmp_path,
        "20260921-003-000.json",
        "validator/batches/20260921/003.json",
        [_result(jobs[0], 0)],
    )

    real_unlink = Path.unlink

    def fail_patch_unlink(self, *args, **kwargs):
        if self == patch:
            raise OSError("patch delete failed")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_patch_unlink)

    with pytest.raises(OSError, match="patch delete failed"):
        apply_patches(tmp_path)

    assert _read(batch)["jobs"][0]["status"] == "REJECTED"
    assert patch.exists()


def test_apply_patch_contract_matches_finalizer_contract():
    assert patcher.FINAL_STATUSES == finalizer.FINAL_STATUSES
    assert patcher.VALIDATED_KEYS == finalizer.VALIDATED_KEYS
    assert patcher.VALIDATED_STRING_KEYS == finalizer.VALIDATED_STRING_KEYS
    assert patcher.DATE_POSTED_PRECISIONS == finalizer.DATE_POSTED_PRECISIONS
