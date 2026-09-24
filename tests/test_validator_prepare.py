"""Behavioral tests for Validator batch preparation."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from validator.prepare import HELSINKI, prepare_batches


EMPTY_STATE = {
    "seen_indeed": [],
    "seen_linkedin": [],
    "reported": [],
}
FIXED_HELSINKI_TIME = datetime(2026, 9, 20, 12, 0, tzinfo=HELSINKI)


def _job(number: int, url: str, *, prefix: str = "Job") -> dict:
    return {
        "company": f"{prefix} Company {number}",
        "title": f"{prefix} Title {number}",
        "location": f"{prefix} Location {number}",
        "url": url,
        "description": f"Description {number}",
    }


def _write_inputs(
    root: Path,
    indeed_jobs: list[dict],
    linkedin_jobs: list[dict],
    *,
    state: dict | None = None,
    indeed_new_jobs: list[dict] | None = None,
    linkedin_new_jobs: list[dict] | None = None,
) -> tuple[Path, Path]:
    output_dir = root / "output"
    validator_dir = root / "validator"
    output_dir.mkdir()
    validator_dir.mkdir()
    indeed_path = output_dir / "indeed_jobs.json"
    linkedin_path = output_dir / "linkedin_jobs.json"
    indeed_path.write_text(json.dumps({
        "jobs": indeed_jobs,
        "new_jobs": indeed_new_jobs or [],
    }), encoding="utf-8")
    linkedin_path.write_text(json.dumps({
        "jobs": linkedin_jobs,
        "new_jobs": linkedin_new_jobs or [],
    }), encoding="utf-8")
    (validator_dir / "state.json").write_text(
        json.dumps(state or EMPTY_STATE), encoding="utf-8"
    )
    (validator_dir / "title_exclusions.json").write_text(
        json.dumps({"title_exclusions": ["NonTargetPlaceholder"]}),
        encoding="utf-8",
    )
    return indeed_path, linkedin_path


def _batch_payloads(summary: dict) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in summary["created_files"]
    ]


def test_builds_standard_jobs_from_top_level_jobs_only(tmp_path):
    indeed_jobs = [
        _job(1, "https://example.indeed.com/viewjob?jk=indeed-1"),
        _job(2, "https://example.indeed.com/viewjob?other=no-id"),
    ]
    linkedin_jobs = [
        _job(3, "https://www.linkedin.com/jobs/view/3003/", prefix="LinkedIn"),
        _job(4, "https://www.linkedin.com/jobs/search/?currentJobId=4004", prefix="LinkedIn"),
    ]
    ignored = _job(99, "https://example.indeed.com/viewjob?jk=ignored")
    indeed_path, linkedin_path = _write_inputs(
        tmp_path,
        indeed_jobs,
        linkedin_jobs,
        indeed_new_jobs=[ignored],
        linkedin_new_jobs=[ignored],
    )
    source_bytes = (indeed_path.read_bytes(), linkedin_path.read_bytes())

    summary = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    payload = _batch_payloads(summary)[0]

    assert summary["indeed_read"] == 2
    assert summary["linkedin_read"] == 2
    assert payload["job_count"] == len(payload["jobs"]) == 4
    assert payload["finalized"] is False
    assert payload["created_at"] == "2026-09-20T12:00:00+03:00"
    assert [job["company"] for job in payload["jobs"]] == [
        job["company"] for job in indeed_jobs + linkedin_jobs
    ]
    assert [job["source"] for job in payload["jobs"]] == [
        "Indeed", "Indeed", "LinkedIn", "LinkedIn",
    ]
    assert [job["job_id"] for job in payload["jobs"]] == [
        "indeed-1", None, "3003", None,
    ]
    assert all(job["status"] is None for job in payload["jobs"])
    assert all(job["reason"] is None for job in payload["jobs"])
    assert all(job["validated"] is None for job in payload["jobs"])
    assert summary["created_markers"] == [
        tmp_path / "validator/open_batches/20260920-001.open"
    ]
    assert summary["created_markers"][0].read_text(encoding="utf-8") == "open\n"
    assert (indeed_path.read_bytes(), linkedin_path.read_bytes()) == source_bytes


def test_applies_exact_state_dedupe_and_ignores_canonical_url(tmp_path):
    indeed_jobs = [
        _job(1, "https://indeed.test/viewjob?jk=seen-id"),
        _job(2, "https://indeed.test/viewjob?jk=seen-url"),
        _job(3, "https://indeed.test/viewjob?jk=reported-id"),
        _job(4, "https://indeed.test/viewjob?jk=reported-triple"),
        _job(5, "https://indeed.test/viewjob?jk=canonical-only"),
    ]
    linkedin_jobs = [
        _job(11, "https://www.linkedin.com/jobs/view/1011/", prefix="LinkedIn"),
        _job(12, "https://www.linkedin.com/jobs/view/1012/", prefix="LinkedIn"),
        _job(13, "https://www.linkedin.com/jobs/view/1013/", prefix="LinkedIn"),
        _job(14, "https://www.linkedin.com/jobs/view/1014/", prefix="LinkedIn"),
        _job(15, "https://www.linkedin.com/jobs/view/1015/", prefix="LinkedIn"),
    ]
    state = {
        "seen_indeed": [
            {"job_id": "seen-id", "job_url": "unused", "seen_at": "2026-09-20T00:00:00Z"},
            {"job_id": "unused", "job_url": indeed_jobs[1]["url"], "seen_at": "2026-09-20T00:00:00Z"},
        ],
        "seen_linkedin": [
            {"job_id": "1011", "job_url": "unused", "seen_at": "2026-09-20T00:00:00Z"},
            {"job_id": "unused", "job_url": linkedin_jobs[1]["url"], "seen_at": "2026-09-20T00:00:00Z"},
        ],
        "reported": [
            {"source": "Indeed", "job_id": "reported-id", "company": "x1", "title": "x1", "location": "x1", "canonical_url": "", "reported_at": "2026-09-20T00:00:00Z"},
            {"source": "LinkedIn", "job_id": "1013", "company": "x2", "title": "x2", "location": "x2", "canonical_url": "", "reported_at": "2026-09-20T00:00:00Z"},
            {"source": "LinkedIn", "job_id": None, "company": indeed_jobs[3]["company"], "title": indeed_jobs[3]["title"], "location": indeed_jobs[3]["location"], "canonical_url": "", "reported_at": "2026-09-20T00:00:00Z"},
            {"source": "Indeed", "job_id": None, "company": linkedin_jobs[3]["company"], "title": linkedin_jobs[3]["title"], "location": linkedin_jobs[3]["location"], "canonical_url": "", "reported_at": "2026-09-20T00:00:00Z"},
            {"source": "Indeed", "job_id": None, "company": "other", "title": "other", "location": "other", "canonical_url": indeed_jobs[4]["url"], "reported_at": "2026-09-20T00:00:00Z"},
            {"source": "LinkedIn", "job_id": None, "company": "other", "title": "other", "location": "other", "canonical_url": linkedin_jobs[4]["url"], "reported_at": "2026-09-20T00:00:00Z"},
        ],
    }
    _write_inputs(tmp_path, indeed_jobs, linkedin_jobs, state=state)

    summary = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    jobs = _batch_payloads(summary)[0]["jobs"]

    assert summary["removed_by_state"] == 8
    assert [(job["source"], job["job_id"]) for job in jobs] == [
        ("Indeed", "canonical-only"),
        ("LinkedIn", "1015"),
    ]


def test_reported_triple_requires_three_nonempty_strings(tmp_path):
    incomplete = _job(1, "https://indeed.test/viewjob?jk=incomplete")
    incomplete["location"] = ""
    exact = _job(2, "https://indeed.test/viewjob?jk=exact")
    different_case = _job(3, "https://indeed.test/viewjob?jk=different-case")
    state = {
        "seen_indeed": [],
        "seen_linkedin": [],
        "reported": [
            {
                "source": "LinkedIn",
                "job_id": None,
                "company": incomplete["company"],
                "title": incomplete["title"],
                "location": "",
                "canonical_url": "",
                "reported_at": "2026-09-20T00:00:00Z",
            },
            {
                "source": "LinkedIn",
                "job_id": None,
                "company": exact["company"],
                "title": exact["title"],
                "location": exact["location"],
                "canonical_url": "",
                "reported_at": "2026-09-20T00:00:00Z",
            },
            {
                "source": "LinkedIn",
                "job_id": None,
                "company": different_case["company"].lower(),
                "title": different_case["title"],
                "location": different_case["location"],
                "canonical_url": "",
                "reported_at": "2026-09-20T00:00:00Z",
            },
        ],
    }
    _write_inputs(tmp_path, [incomplete, exact, different_case], [], state=state)

    summary = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    jobs = _batch_payloads(summary)[0]["jobs"]

    assert summary["removed_by_state"] == 1
    assert [job["job_id"] for job in jobs] == ["incomplete", "different-case"]


def test_ignores_non_three_digit_batch_files(tmp_path):
    in_valid_batch = _job(1, "https://indeed.test/viewjob?jk=valid-batch")
    in_debug_file = _job(2, "https://indeed.test/viewjob?jk=debug-file")
    _write_inputs(tmp_path, [in_valid_batch, in_debug_file], [])
    day_dir = tmp_path / "validator/batches/20260920"
    day_dir.mkdir(parents=True)
    valid_path = day_dir / "001.json"
    valid_path.write_text(json.dumps({
        "created_at": "2026-09-20T00:00:00+03:00",
        "job_count": 1,
        "finalized": False,
        "jobs": [{**in_valid_batch, "source": "Indeed", "job_id": "valid-batch"}],
    }), encoding="utf-8")
    debug_path = day_dir / "debug.json"
    debug_path.write_text(json.dumps({
        "created_at": "2026-09-20T00:00:00+03:00",
        "job_count": 1,
        "finalized": False,
        "jobs": [{**in_debug_file, "source": "Indeed", "job_id": "debug-file"}],
    }), encoding="utf-8")
    valid_bytes = valid_path.read_bytes()
    debug_bytes = debug_path.read_bytes()

    summary = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    jobs = _batch_payloads(summary)[0]["jobs"]

    assert summary["removed_by_batches"] == 1
    assert [path.name for path in summary["created_files"]] == ["002.json"]
    assert [job["job_id"] for job in jobs] == ["debug-file"]
    assert valid_path.read_bytes() == valid_bytes
    assert debug_path.read_bytes() == debug_bytes


def test_splits_batches_and_continues_numbering_without_overwrite(tmp_path):
    indeed_jobs = [
        _job(number, f"https://indeed.test/viewjob?jk={number}")
        for number in range(120)
    ]
    _write_inputs(tmp_path, indeed_jobs, [])
    day_dir = tmp_path / "validator/batches/20260920"
    day_dir.mkdir(parents=True)
    existing_path = day_dir / "001.json"
    existing_path.write_text(json.dumps({
        "created_at": "2026-09-20T00:00:00+03:00",
        "job_count": 0,
        "finalized": False,
        "jobs": [],
    }), encoding="utf-8")
    existing_bytes = existing_path.read_bytes()
    utc_near_midnight = datetime(2026, 9, 19, 21, 30, tzinfo=timezone.utc)

    summary = prepare_batches(tmp_path, now=utc_near_midnight)
    payloads = _batch_payloads(summary)

    assert [path.name for path in summary["created_files"]] == [
        "002.json", "003.json",
    ]
    assert [payload["job_count"] for payload in payloads] == [100, 20]
    assert all(len(payload["jobs"]) <= 100 for payload in payloads)
    assert all(payload["created_at"] == "2026-09-20T00:30:00+03:00" for payload in payloads)
    assert existing_path.read_bytes() == existing_bytes


def test_rerun_uses_existing_daily_batches_without_modifying_them(tmp_path):
    jobs = [
        _job(1, "https://indeed.test/viewjob?jk=one"),
        _job(2, "https://indeed.test/viewjob?jk=two"),
    ]
    _write_inputs(tmp_path, jobs, [])
    first = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    path = first["created_files"][0]
    original_bytes = path.read_bytes()

    second = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)

    assert second["ready"] == 0
    assert second["removed_by_batches"] == 2
    assert second["created_files"] == []
    assert path.read_bytes() == original_bytes


def test_removes_run_duplicates_and_skips_jobs_without_identity(tmp_path):
    duplicate_id = _job(1, "https://indeed.test/viewjob?jk=duplicate")
    duplicate_url = _job(2, "https://indeed.test/viewjob?other=no-id")
    missing_identity = _job(3, "")
    _write_inputs(
        tmp_path,
        [
            duplicate_id,
            dict(duplicate_id),
            duplicate_url,
            dict(duplicate_url),
            missing_identity,
            dict(missing_identity),
        ],
        [],
    )

    summary = prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)
    jobs = _batch_payloads(summary)[0]["jobs"]

    assert summary["ready"] == 2
    assert summary["removed_within_run"] == 2
    assert summary["skipped_missing_identity"] == 2
    assert [job["company"] for job in jobs] == [
        duplicate_id["company"], duplicate_url["company"],
    ]


def test_open_marker_collision_rolls_back_new_batch(tmp_path):
    _write_inputs(
        tmp_path,
        [_job(1, "https://example.indeed.com/viewjob?jk=indeed-1")],
        [],
    )
    marker = tmp_path / "validator/open_batches/20260920-001.open"
    marker.parent.mkdir(parents=True)
    marker.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        prepare_batches(tmp_path, now=FIXED_HELSINKI_TIME)

    assert not (tmp_path / "validator/batches/20260920/001.json").exists()
    assert marker.read_text(encoding="utf-8") == "existing\n"
