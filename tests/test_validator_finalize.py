"""Behavioral tests for deterministic Validator batch finalization."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import validator.finalize as finalizer
from validator.finalize import HELSINKI, finalize_batches


EXPECTED_HELSINKI = ZoneInfo("Europe/Helsinki")
EXPECTED_VALIDATED_KEYS = {
    "work_mode",
    "required_experience",
    "date_posted",
    "date_posted_at",
    "date_posted_date",
    "date_posted_precision",
    "work_authorization",
    "residence_requirement",
    "short_description",
    "direct_application_link",
}
FIXED_TIME = datetime(2026, 9, 21, 12, 30, tzinfo=EXPECTED_HELSINKI)
EXPECTED_SEEN_AT = "2026-09-21T12:30:00+03:00"
_UNSET = object()
STRING_FIELDS = (
    "work_mode", "required_experience", "date_posted", "work_authorization",
    "residence_requirement", "short_description", "direct_application_link",
)


def _validated(**overrides):
    value = {
        "work_mode": "Remote",
        "required_experience": "3 years",
        "date_posted": "2026-09-21",
        "date_posted_at": "2026-09-21T08:00:00+00:00",
        "date_posted_date": None,
        "date_posted_precision": "exact",
        "work_authorization": "Not stated",
        "residence_requirement": "EU",
        "short_description": "Backend role",
        "direct_application_link": "https://example.test/apply",
    }
    value.update(overrides)
    return value


def _state(*, seen_indeed=None, seen_linkedin=None, reported=None):
    return {
        "seen_indeed": [] if seen_indeed is None else seen_indeed,
        "seen_linkedin": [] if seen_linkedin is None else seen_linkedin,
        "reported": [] if reported is None else reported,
    }


def _job(number, status, *, source="Indeed", reason="Reviewed", validated=_UNSET,
         job_id=_UNSET, url=_UNSET):
    if validated is _UNSET:
        validated = _validated() if status == "QUALIFIED" else None
    if job_id is _UNSET:
        job_id = f"job-{number}"
    if url is _UNSET:
        url = f"https://example.com/jobs/{number}?x=1"
    return {
        "company": f"Company {number}", "title": f"Title {number}",
        "location": f"Location {number}", "url": url, "source": source,
        "job_id": job_id, "status": status, "reason": reason,
        "validated": validated,
    }


def _batch_payload(jobs, *, finalized=False):
    return {
        "created_at": "2026-09-21T11:00:00+03:00", "job_count": len(jobs),
        "finalized": finalized, "jobs": jobs,
    }


def _write_state(root: Path, payload) -> Path:
    validator_dir = root / "validator"
    validator_dir.mkdir(exist_ok=True)
    path = validator_dir / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_batch(root: Path, jobs=None, *, name="001.json", day="20260921",
                 finalized=False, payload=_UNSET) -> Path:
    day_dir = root / "validator/batches" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / name
    if payload is _UNSET:
        payload = _batch_payload(jobs or [], finalized=finalized)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_failure_without_writes(root, state_path, *batch_paths):
    state_bytes = state_path.read_bytes()
    batch_bytes = {path: path.read_bytes() for path in batch_paths}
    with pytest.raises(ValueError):
        finalize_batches(root, now=FIXED_TIME)
    assert state_path.read_bytes() == state_bytes
    for path, original in batch_bytes.items():
        assert path.read_bytes() == original


def test_final_status_outcomes_and_content_preservation(tmp_path):
    existing_seen = [{
        "job_id": "existing", "job_url": "https://example.com/existing",
        "seen_at": "2026-09-20T00:00:00+03:00", "custom": {"keep": True},
    }]
    reported = [{"source": "Indeed", "job_id": "reported", "custom": {"keep": True}}]
    state_path = _write_state(tmp_path, _state(
        seen_indeed=copy.deepcopy(existing_seen), reported=copy.deepcopy(reported),
    ))
    jobs = [
        _job(1, "REJECTED", source="Indeed"),
        _job(2, "REJECTED", source="LinkedIn", job_id=None),
        _job(3, "QUALIFIED", source="Indeed"),
        _job(4, "UNVALIDATED", source="LinkedIn"),
    ]
    original_jobs = copy.deepcopy(jobs)
    original_payload = _batch_payload(copy.deepcopy(jobs))
    batch_path = _write_batch(tmp_path, payload=original_payload)

    summary = finalize_batches(tmp_path, now=FIXED_TIME)

    state = _read(state_path)
    batch = _read(batch_path)
    assert summary["seen_added"] == 2
    assert summary["finalized_files"] == [batch_path]
    assert state["seen_indeed"] == existing_seen + [{
        "job_id": "job-1", "job_url": "https://example.com/jobs/1?x=1",
        "seen_at": EXPECTED_SEEN_AT,
    }]
    assert state["seen_linkedin"] == [{
        "job_id": "", "job_url": "https://example.com/jobs/2?x=1",
        "seen_at": EXPECTED_SEEN_AT,
    }]
    assert state["reported"] == reported
    expected_payload = copy.deepcopy(original_payload)
    expected_payload["finalized"] = True
    assert batch == expected_payload
    assert batch["jobs"] == original_jobs
    assert batch["jobs"][2]["validated"] == original_jobs[2]["validated"]


def test_pending_batch_causes_no_state_write_and_remains_unchanged(tmp_path):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [
        _job(1, "REJECTED"), _job(2, None, reason=None),
    ])
    state_bytes = state_path.read_bytes()
    batch_bytes = batch_path.read_bytes()
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    assert summary["pending_batches"] == 1
    assert summary["finalized_files"] == []
    assert state_path.read_bytes() == state_bytes
    assert batch_path.read_bytes() == batch_bytes


def test_already_finalized_batch_is_ignored_byte_for_byte(tmp_path):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(
        tmp_path, payload={"finalized": True, "jobs": "not validated again", "custom": 1},
    )
    state_bytes = state_path.read_bytes()
    batch_bytes = batch_path.read_bytes()
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    assert summary["finalized_files"] == []
    assert state_path.read_bytes() == state_bytes
    assert batch_path.read_bytes() == batch_bytes


def test_only_exact_batch_paths_are_considered(tmp_path):
    state_path = _write_state(tmp_path, _state())
    valid_path = _write_batch(tmp_path, [_job(1, "UNVALIDATED")])
    ignored_paths = [
        valid_path.with_name("debug.json"), valid_path.with_name("1.json"),
        valid_path.with_name("0001.json"),
        _write_batch(tmp_path, name="002.json", day="not-a-day", payload="invalid"),
        _write_batch(tmp_path, name="003.json", day="2026092", payload="invalid"),
    ]
    for path in ignored_paths[:3]:
        path.write_text("not json", encoding="utf-8")
    ignored_bytes = {path: path.read_bytes() for path in ignored_paths}
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    assert summary["scanned_batches"] == 1
    assert _read(valid_path)["finalized"] is True
    assert _read(state_path) == _state()
    for path, original in ignored_bytes.items():
        assert path.read_bytes() == original


@pytest.mark.parametrize("source", ["Indeed", "LinkedIn"])
@pytest.mark.parametrize("dedupe_key", ["id", "url"])
def test_existing_seen_deduplicates_by_corresponding_source(tmp_path, source, dedupe_key):
    ledger = "seen_indeed" if source == "Indeed" else "seen_linkedin"
    job = _job(1, "REJECTED", source=source, job_id="same-id", url="https://example.com/same")
    existing = {
        "job_id": "same-id" if dedupe_key == "id" else "other-id",
        "job_url": "https://example.com/same" if dedupe_key == "url" else "https://example.com/other",
        "seen_at": "2026-09-20T00:00:00+03:00",
    }
    state_path = _write_state(tmp_path, _state(**{ledger: [existing]}))
    batch_path = _write_batch(tmp_path, [job])
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    assert summary["seen_added"] == 0
    assert _read(state_path)[ledger] == [existing]
    assert _read(batch_path)["finalized"] is True


@pytest.mark.parametrize("source", ["Indeed", "LinkedIn"])
def test_other_source_seen_identity_does_not_deduplicate(tmp_path, source):
    other_ledger = "seen_linkedin" if source == "Indeed" else "seen_indeed"
    target_ledger = "seen_indeed" if source == "Indeed" else "seen_linkedin"
    existing = {
        "job_id": "same-id", "job_url": "https://example.com/same",
        "seen_at": "2026-09-20T00:00:00+03:00",
    }
    state_path = _write_state(tmp_path, _state(**{other_ledger: [existing]}))
    _write_batch(tmp_path, [
        _job(1, "REJECTED", source=source, job_id="same-id", url="https://example.com/same"),
    ])
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    state = _read(state_path)
    assert summary["seen_added"] == 1
    assert len(state[target_ledger]) == 1
    assert state[other_ledger] == [existing]


def test_seen_deduplication_within_one_run(tmp_path):
    state_path = _write_state(tmp_path, _state())
    exact = _job(1, "REJECTED", job_id="exact", url="https://example.com/exact")
    jobs = [
        exact, copy.deepcopy(exact),
        _job(2, "REJECTED", job_id="shared", url="https://example.com/id-a"),
        _job(3, "REJECTED", job_id="shared", url="https://example.com/id-b"),
        _job(4, "REJECTED", job_id="", url="https://example.com/empty-a"),
        _job(5, "REJECTED", job_id="", url="https://example.com/empty-b"),
        _job(6, "REJECTED", job_id="   ", url="https://example.com/space-a"),
        _job(7, "REJECTED", job_id="\t", url="https://example.com/space-b"),
        _job(8, "REJECTED", job_id="", url="https://example.com/same-url"),
        _job(9, "REJECTED", job_id="", url="https://example.com/same-url"),
    ]
    _write_batch(tmp_path, jobs)
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    seen = _read(state_path)["seen_indeed"]
    assert summary["seen_added"] == 7
    assert [row["job_id"] for row in seen] == ["exact", "shared", "", "", "", "", ""]
    assert [row["job_url"] for row in seen] == [
        "https://example.com/exact", "https://example.com/id-a",
        "https://example.com/empty-a", "https://example.com/empty-b",
        "https://example.com/space-a", "https://example.com/space-b",
        "https://example.com/same-url",
    ]


@pytest.mark.parametrize("job_url", [None, 123, "", "   ", "relative", "ftp://example.com/job"])
def test_invalid_existing_seen_urls_are_not_dedupe_keys(job_url):
    _, urls = finalizer._seen_indexes([{"job_id": "", "job_url": job_url}])
    assert urls == set()


def test_valid_existing_seen_http_urls_are_exact_dedupe_keys():
    _, urls = finalizer._seen_indexes([
        {"job_id": "", "job_url": "https://example.com/jobs/1?x=1"},
        {"job_id": "", "job_url": "http://example.com/jobs/2"},
    ])
    assert urls == {"https://example.com/jobs/1?x=1", "http://example.com/jobs/2"}


def test_invalid_existing_seen_url_does_not_block_valid_incoming_url(tmp_path):
    existing = {
        "job_id": "",
        "job_url": "ftp://example.com/job",
        "seen_at": "2026-09-20T00:00:00+03:00",
    }
    state_path = _write_state(tmp_path, _state(seen_indeed=[existing]))
    batch_path = _write_batch(tmp_path, [
        _job(1, "REJECTED", job_id="", url="https://example.com/job"),
    ])

    summary = finalize_batches(tmp_path, now=FIXED_TIME)

    assert summary["seen_added"] == 1
    assert _read(state_path)["seen_indeed"] == [
        existing,
        {
            "job_id": "",
            "job_url": "https://example.com/job",
            "seen_at": EXPECTED_SEEN_AT,
        },
    ]
    assert _read(batch_path)["finalized"] is True


INVALID_IDENTITIES = (
    ("unsupported-source", "source", "Other"),
    ("missing-source", "source", _UNSET),
    ("empty-url", "url", ""),
    ("whitespace-url", "url", "   "),
    ("leading-url-whitespace", "url", " https://example.com/jobs/1"),
    ("trailing-url-whitespace", "url", "https://example.com/jobs/1 "),
    ("embedded-url-whitespace", "url", "https://example.com/jobs/a b"),
    ("relative-url", "url", "/jobs/1"),
    ("non-http-url", "url", "ftp://example.com/jobs/1"),
    ("missing-host", "url", "https:///jobs/1"),
    ("non-string-job-id", "job_id", 123),
)


@pytest.mark.parametrize("status", ["REJECTED", "QUALIFIED"])
@pytest.mark.parametrize(
    ("case_name", "field", "value"), INVALID_IDENTITIES,
    ids=[case[0] for case in INVALID_IDENTITIES],
)
def test_rejected_and_qualified_require_valid_identity(tmp_path, status, case_name, field, value):
    del case_name
    state_path = _write_state(tmp_path, _state())
    job = _job(1, status)
    job.pop(field) if value is _UNSET else job.__setitem__(field, value)
    batch_path = _write_batch(tmp_path, [job])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("status", ["REJECTED", "QUALIFIED"])
@pytest.mark.parametrize("job_id", [None, ""])
def test_final_identity_accepts_empty_or_null_id_with_valid_url(tmp_path, status, job_id):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [
        _job(1, status, job_id=job_id, url="https://example.com/jobs/123?x=1"),
    ])
    finalize_batches(tmp_path, now=FIXED_TIME)
    assert _read(batch_path)["finalized"] is True
    if status == "REJECTED":
        assert _read(state_path)["seen_indeed"][0]["job_id"] == ""
    else:
        assert _read(state_path) == _state()


@pytest.mark.parametrize("identity", [
    {},
    {"source": "Other", "url": "relative", "job_id": 123},
    {"source": " ", "url": " ", "job_id": " "},
])
def test_unvalidated_allows_missing_or_malformed_identity(tmp_path, identity):
    state_path = _write_state(tmp_path, _state())
    job = _job(1, "UNVALIDATED")
    for key in ("source", "url", "job_id"):
        job.pop(key)
    job.update(identity)
    original = copy.deepcopy(job)
    batch_path = _write_batch(tmp_path, [job])
    finalize_batches(tmp_path, now=FIXED_TIME)
    batch = _read(batch_path)
    assert batch["finalized"] is True
    assert batch["jobs"] == [original]
    assert _read(state_path) == _state()


@pytest.mark.parametrize(
    "finalized_value", [_UNSET, None, "false", 0, 1, [], {}],
    ids=["missing", "null", "string", "zero", "one", "list", "object"],
)
def test_invalid_batch_finalized_value_fails_safely(tmp_path, finalized_value):
    state_path = _write_state(tmp_path, _state())
    payload = _batch_payload([_job(1, "REJECTED")])
    payload.pop("finalized") if finalized_value is _UNSET else payload.__setitem__("finalized", finalized_value)
    batch_path = _write_batch(tmp_path, payload=payload)
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_non_object_batch_root_fails_safely(tmp_path):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, payload=[])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("jobs_value", [_UNSET, None, {}, ["not-an-object"]])
def test_invalid_jobs_shape_fails_safely(tmp_path, jobs_value):
    state_path = _write_state(tmp_path, _state())
    payload = _batch_payload([])
    payload.pop("jobs") if jobs_value is _UNSET else payload.__setitem__("jobs", jobs_value)
    batch_path = _write_batch(tmp_path, payload=payload)
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_invalid_non_null_status_fails_before_any_batch_is_finalized(tmp_path):
    state_path = _write_state(tmp_path, _state())
    first_path = _write_batch(tmp_path, [_job(1, "REJECTED")], name="001.json")
    later_path = _write_batch(tmp_path, [_job(2, "INVALID")], name="002.json")
    _assert_failure_without_writes(tmp_path, state_path, first_path, later_path)


INVALID_STATES = (
    ("non-object-root", []),
    ("missing-seen-indeed", {"seen_linkedin": [], "reported": []}),
    ("null-seen-indeed", {"seen_indeed": None, "seen_linkedin": [], "reported": []}),
    ("non-list-seen-indeed", _state(seen_indeed={})),
    ("missing-seen-linkedin", {"seen_indeed": [], "reported": []}),
    ("null-seen-linkedin", {"seen_indeed": [], "seen_linkedin": None, "reported": []}),
    ("non-list-seen-linkedin", _state(seen_linkedin={})),
    ("missing-reported", {"seen_indeed": [], "seen_linkedin": []}),
    ("null-reported", {"seen_indeed": [], "seen_linkedin": [], "reported": None}),
    ("non-list-reported", _state(reported={})),
    ("invalid-seen-indeed-row", _state(seen_indeed=["invalid"])),
    ("invalid-seen-linkedin-row", _state(seen_linkedin=["invalid"])),
    ("invalid-reported-row", _state(reported=["invalid"])),
)


@pytest.mark.parametrize(
    ("case_name", "state_payload"), INVALID_STATES,
    ids=[case[0] for case in INVALID_STATES],
)
def test_invalid_state_schema_never_finalizes_batch(tmp_path, case_name, state_payload):
    del case_name
    state_path = _write_state(tmp_path, state_payload)
    batch_path = _write_batch(tmp_path, [_job(1, "REJECTED")])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("status", ["REJECTED", "QUALIFIED", "UNVALIDATED"])
@pytest.mark.parametrize("reason", [None, "", "   ", 123])
def test_every_final_status_requires_nonempty_reason(tmp_path, status, reason):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, status, reason=reason)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("status", ["REJECTED", "UNVALIDATED"])
@pytest.mark.parametrize("validated", [{}, {"unexpected": True}, "value"])
def test_rejected_and_unvalidated_require_null_validated(tmp_path, status, validated):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, status, validated=validated)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("validated", [None, [], "value", 123])
def test_qualified_requires_validated_object(tmp_path, validated):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_canonical_validated_output_passes(tmp_path):
    state_path = _write_state(tmp_path, _state())
    validated = _validated()
    batch_path = _write_batch(tmp_path, [
        _job(1, "QUALIFIED", validated=copy.deepcopy(validated)),
    ])
    finalize_batches(tmp_path, now=FIXED_TIME)
    assert _read(batch_path)["jobs"][0]["validated"] == validated
    assert _read(state_path) == _state()


def test_production_contract_constants_match_expected():
    assert finalizer.VALIDATED_KEYS == frozenset(EXPECTED_VALIDATED_KEYS)
    assert HELSINKI.key == "Europe/Helsinki"


@pytest.mark.parametrize("missing_key", sorted(EXPECTED_VALIDATED_KEYS))
def test_each_missing_validated_key_fails(tmp_path, missing_key):
    state_path = _write_state(tmp_path, _state())
    validated = _validated()
    validated.pop(missing_key)
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_extra_validated_key_fails(tmp_path):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [
        _job(1, "QUALIFIED", validated={**_validated(), "extra": "value"}),
    ])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize("invalid_value", [123, "", "   "])
def test_required_validated_strings_must_be_nonempty(tmp_path, field, invalid_value):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [
        _job(1, "QUALIFIED", validated=_validated(**{field: invalid_value})),
    ])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize(
    "date_posted_at", ["2026-09-21T08:00:00", "not-a-datetime", 123, []],
)
def test_invalid_date_posted_at_fails(tmp_path, date_posted_at):
    state_path = _write_state(tmp_path, _state())
    validated = _validated(
        date_posted_precision="relative", date_posted_at=date_posted_at,
        date_posted_date=None,
    )
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("date_posted_at", ["2026-09-21T08:00:00Z", None])
def test_relative_precision_accepts_aware_or_null_timestamp(tmp_path, date_posted_at):
    state_path = _write_state(tmp_path, _state())
    validated = _validated(
        date_posted_precision="relative", date_posted_at=date_posted_at,
        date_posted_date=None,
    )
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    finalize_batches(tmp_path, now=FIXED_TIME)
    assert _read(batch_path)["finalized"] is True
    assert _read(state_path) == _state()


@pytest.mark.parametrize("date_posted_date", ["2026-02-30", "09/21/2026", 123, []])
def test_invalid_date_posted_date_fails(tmp_path, date_posted_date):
    state_path = _write_state(tmp_path, _state())
    validated = _validated(
        date_posted_precision="date_only", date_posted_at=None,
        date_posted_date=date_posted_date,
    )
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_date_only_precision_accepts_valid_calendar_date(tmp_path):
    state_path = _write_state(tmp_path, _state())
    validated = _validated(
        date_posted_precision="date_only", date_posted_at=None,
        date_posted_date="2026-09-21",
    )
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    finalize_batches(tmp_path, now=FIXED_TIME)
    assert _read(batch_path)["finalized"] is True
    assert _read(state_path) == _state()


PRECISION_CASES = (
    ("exact-timestamp", "exact", "2026-09-21T08:00:00+00:00", None, True),
    ("exact-missing-timestamp", "exact", None, None, False),
    ("exact-with-date", "exact", "2026-09-21T08:00:00+00:00", "2026-09-21", False),
    ("relative-timestamp", "relative", "2026-09-21T08:00:00+00:00", None, True),
    ("relative-no-timestamp", "relative", None, None, True),
    ("relative-with-date", "relative", None, "2026-09-21", False),
    ("date-only", "date_only", None, "2026-09-21", True),
    ("date-only-with-timestamp", "date_only", "2026-09-21T08:00:00+00:00", "2026-09-21", False),
    ("date-only-missing-date", "date_only", None, None, False),
    ("approximate-empty", "approximate", None, None, True),
    ("approximate-with-timestamp", "approximate", "2026-09-21T08:00:00+00:00", None, False),
    ("approximate-with-date", "approximate", None, "2026-09-21", False),
    ("ambiguous-empty", "missing_or_ambiguous", None, None, True),
    ("ambiguous-with-timestamp", "missing_or_ambiguous", "2026-09-21T08:00:00+00:00", None, False),
    ("ambiguous-with-date", "missing_or_ambiguous", None, "2026-09-21", False),
)


@pytest.mark.parametrize(
    ("case_name", "precision", "date_posted_at", "date_posted_date", "is_valid"),
    PRECISION_CASES, ids=[case[0] for case in PRECISION_CASES],
)
def test_date_precision_consistency_matrix(
    tmp_path, case_name, precision, date_posted_at, date_posted_date, is_valid,
):
    del case_name
    state_path = _write_state(tmp_path, _state())
    validated = _validated(
        date_posted_precision=precision, date_posted_at=date_posted_at,
        date_posted_date=date_posted_date,
    )
    batch_path = _write_batch(tmp_path, [_job(1, "QUALIFIED", validated=validated)])
    if is_valid:
        finalize_batches(tmp_path, now=FIXED_TIME)
        assert _read(batch_path)["finalized"] is True
    else:
        _assert_failure_without_writes(tmp_path, state_path, batch_path)


@pytest.mark.parametrize("precision", ["invalid", "", "   ", 123, None])
def test_invalid_date_posted_precision_fails(tmp_path, precision):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [
        _job(1, "QUALIFIED", validated=_validated(date_posted_precision=precision)),
    ])
    _assert_failure_without_writes(tmp_path, state_path, batch_path)


def test_all_batches_validate_before_any_mutation(tmp_path):
    state_path = _write_state(tmp_path, _state())
    first_path = _write_batch(tmp_path, [_job(1, "REJECTED")], name="001.json")
    second_path = _write_batch(
        tmp_path, [_job(2, "QUALIFIED", validated=None)], name="002.json",
    )
    _assert_failure_without_writes(tmp_path, state_path, first_path, second_path)


def test_state_write_failure_does_not_finalize_batch(tmp_path, monkeypatch):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, "REJECTED")])
    state_bytes = state_path.read_bytes()
    batch_bytes = batch_path.read_bytes()
    real_write = finalizer._atomic_write_json

    def fail_state_write(path, payload):
        if path == state_path:
            raise OSError("state write failed")
        real_write(path, payload)

    monkeypatch.setattr(finalizer, "_atomic_write_json", fail_state_write)
    with pytest.raises(OSError, match="state write failed"):
        finalize_batches(tmp_path, now=FIXED_TIME)
    assert state_path.read_bytes() == state_bytes
    assert batch_path.read_bytes() == batch_bytes


def test_batch_write_failure_recovers_idempotently_on_rerun(tmp_path, monkeypatch):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, "REJECTED")])
    batch_bytes = batch_path.read_bytes()
    real_write = finalizer._atomic_write_json

    def fail_batch_write(path, payload):
        if path == batch_path:
            raise OSError("batch write failed")
        real_write(path, payload)

    monkeypatch.setattr(finalizer, "_atomic_write_json", fail_batch_write)
    with pytest.raises(OSError, match="batch write failed"):
        finalize_batches(tmp_path, now=FIXED_TIME)
    assert len(_read(state_path)["seen_indeed"]) == 1
    assert batch_path.read_bytes() == batch_bytes

    monkeypatch.setattr(finalizer, "_atomic_write_json", real_write)
    summary = finalize_batches(tmp_path, now=FIXED_TIME)
    assert summary["seen_added"] == 0
    assert len(_read(state_path)["seen_indeed"]) == 1
    assert _read(batch_path)["finalized"] is True


def test_successful_rerun_is_idempotent(tmp_path):
    state_path = _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, "REJECTED")])
    first = finalize_batches(tmp_path, now=FIXED_TIME)
    state_bytes = state_path.read_bytes()
    batch_bytes = batch_path.read_bytes()
    second = finalize_batches(tmp_path, now=FIXED_TIME)
    assert first["seen_added"] == 1
    assert second["seen_added"] == 0
    assert second["finalized_files"] == []
    assert state_path.read_bytes() == state_bytes
    assert batch_path.read_bytes() == batch_bytes


def test_seen_timestamp_represents_supplied_instant_in_helsinki(tmp_path):
    supplied = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    state_path = _write_state(tmp_path, _state())
    _write_batch(tmp_path, [_job(1, "REJECTED")])
    finalize_batches(tmp_path, now=supplied)
    seen_at = _read(state_path)["seen_indeed"][0]["seen_at"]
    parsed = datetime.fromisoformat(seen_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
    assert parsed == supplied.astimezone(EXPECTED_HELSINKI)


def test_pending_batch_creates_missing_open_marker(tmp_path):
    _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, None)])

    summary = finalize_batches(tmp_path, now=FIXED_TIME)

    marker = tmp_path / "validator/open_batches/20260921-001.open"
    sidecar_marker = batch_path.with_suffix(".open")
    assert marker.read_text(encoding="utf-8") == "open\n"
    assert sidecar_marker.read_text(encoding="utf-8") == "open\n"
    assert summary["markers_created"] == [marker]
    assert summary["sidecar_markers_created"] == [sidecar_marker]
    assert summary["markers_removed"] == []
    assert summary["sidecar_markers_removed"] == []
    assert _read(batch_path)["finalized"] is False


def test_completed_batch_removes_open_marker(tmp_path):
    _write_state(tmp_path, _state())
    batch_path = _write_batch(tmp_path, [_job(1, "REJECTED")])
    marker = tmp_path / "validator/open_batches/20260921-001.open"
    sidecar_marker = batch_path.with_suffix(".open")
    marker.parent.mkdir(parents=True)
    marker.write_text("open\n", encoding="utf-8")
    sidecar_marker.write_text("open\n", encoding="utf-8")

    summary = finalize_batches(tmp_path, now=FIXED_TIME)

    assert _read(batch_path)["finalized"] is True
    assert not marker.exists()
    assert not sidecar_marker.exists()
    assert summary["markers_removed"] == [marker]
    assert summary["sidecar_markers_removed"] == [sidecar_marker]


def test_already_finalized_batch_removes_stale_open_marker(tmp_path):
    _write_state(tmp_path, _state())
    _write_batch(
        tmp_path,
        payload={"finalized": True, "jobs": "not validated again"},
    )
    marker = tmp_path / "validator/open_batches/20260921-001.open"
    sidecar_marker = (
        tmp_path / "validator/batches/20260921/001.open"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text("open\n", encoding="utf-8")
    sidecar_marker.write_text("open\n", encoding="utf-8")

    summary = finalize_batches(tmp_path, now=FIXED_TIME)

    assert not marker.exists()
    assert not sidecar_marker.exists()
    assert summary["markers_removed"] == [marker]
    assert summary["sidecar_markers_removed"] == [sidecar_marker]
