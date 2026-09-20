"""Behavioral tests for deterministic Validator email dispatch."""

import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import validator.email_dispatch as dispatcher
from validator.email_dispatch import dispatch_email


HELSINKI = ZoneInfo("Europe/Helsinki")
FIXED_TIME = datetime(2026, 9, 21, 14, 5, 9, tzinfo=HELSINKI)
EXPECTED_REPORTED_AT = "2026-09-21T14:05:09+03:00"
ENV = {
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "465",
    "SMTP_USERNAME": "user",
    "SMTP_PASSWORD": "secret",
    "SMTP_SECURITY": "ssl",
    "EMAIL_FROM": "from@example.test",
    "EMAIL_RECIPIENT": "to@example.test",
}
EXPECTED_VALIDATED_KEYS = {
    "work_mode", "required_experience", "date_posted", "date_posted_at",
    "date_posted_date", "date_posted_precision", "work_authorization",
    "residence_requirement", "short_description", "direct_application_link",
}


def _validated(**overrides):
    value = {
        "work_mode": "Remote",
        "required_experience": "3 years",
        "date_posted": "Today",
        "date_posted_at": "2026-09-21T10:00:00+00:00",
        "date_posted_date": None,
        "date_posted_precision": "exact",
        "work_authorization": "EU authorization",
        "residence_requirement": "EU resident",
        "short_description": "Build APIs",
        "direct_application_link": "https://apply.example.test/1",
    }
    value.update(overrides)
    return value


def _job(number=1, **overrides):
    value = {
        "source": "Indeed",
        "job_id": f"id-{number}",
        "url": f"https://jobs.example.test/{number}",
        "company": f"Company {number}",
        "title": f"Title {number}",
        "location": f"Location {number}",
        "status": "QUALIFIED",
        "reason": "Matches requirements",
        "validated": _validated(direct_application_link=f"https://apply.example.test/{number}"),
    }
    value.update(overrides)
    return value


def _state(*, seen_indeed=None, seen_linkedin=None, reported=None):
    return {
        "seen_indeed": [] if seen_indeed is None else seen_indeed,
        "seen_linkedin": [] if seen_linkedin is None else seen_linkedin,
        "reported": [] if reported is None else reported,
    }


def _write_state(root: Path, payload=None):
    path = root / "validator/state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_state() if payload is None else payload), encoding="utf-8")
    return path


def _write_batch(root: Path, jobs, *, day="20260921", name="001.json", finalized=True):
    path = root / "validator/batches" / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"finalized": finalized, "jobs": jobs}), encoding="utf-8")
    return path


class FakeSMTP:
    instances = []
    refused = {}
    send_error = None

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.events = []
        self.message = None
        type(self).instances.append(self)

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *args):
        self.events.append("exit")

    def starttls(self, **kwargs):
        self.events.append(("starttls", kwargs))

    def login(self, username, password):
        self.events.append(("login", username, password))

    def send_message(self, message, *, from_addr=None, to_addrs=None):
        self.events.append(("send", from_addr, to_addrs))
        self.message = message
        if type(self).send_error is not None:
            raise type(self).send_error
        return type(self).refused


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.refused = {}
    FakeSMTP.send_error = None
    monkeypatch.setattr(dispatcher.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(dispatcher.smtplib, "SMTP", FakeSMTP)


def _message_parts():
    message = FakeSMTP.instances[-1].message
    return message, message.get_body(preferencelist=("plain",)).get_content(), message.get_body(preferencelist=("html",)).get_content()


def test_dispatches_one_combined_multipart_email_and_appends_reported_in_email_order(tmp_path):
    seen_indeed = [{"keep": {"nested": True}}]
    seen_linkedin = [{"keep": [1, 2]}]
    existing_reported = [{"source": "Indeed", "job_id": "old", "custom": True}]
    state_path = _write_state(tmp_path, _state(
        seen_indeed=copy.deepcopy(seen_indeed),
        seen_linkedin=copy.deepcopy(seen_linkedin),
        reported=copy.deepcopy(existing_reported),
    ))
    older = _job(1, source="LinkedIn", company="Zulu", validated=_validated(
        date_posted_at="2026-09-21T08:00:00+00:00",
        direct_application_link="not a URL",
    ))
    newer = _job(2, company="Alpha", validated=_validated(
        date_posted_at="2026-09-21T11:00:00+00:00",
        direct_application_link="https://apply.example.test/2?x=1&y=2",
    ))
    _write_batch(tmp_path, [older], day="20260920", name="999.json")
    _write_batch(tmp_path, [newer], day="20260921", name="001.json")

    summary = dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)

    assert summary == {"pending_count": 2, "sent_count": 2}
    assert len(FakeSMTP.instances) == 1
    message, plain, html_body = _message_parts()
    assert message.is_multipart()
    assert message.get_content_type() == "multipart/alternative"
    assert message.get_body(preferencelist=("plain",)) is not None
    assert message.get_body(preferencelist=("html",)) is not None
    assert message["Subject"] == ".NET Backend Job Matches — Indeed + LinkedIn — 2026-09-21 14:05"
    assert plain.index("Title 2") < plain.index("Title 1")
    for label in (
        "Job Title", "Company", "Location", "Work Mode", "Required Experience",
        "Date Posted", "Visa / Work Authorization", "Current Residence Requirement",
        "Short Description", "Direct Application Link",
    ):
        assert plain.count(f"{label}:") == 2
    assert "https://jobs.example.test/1" in plain
    assert 'href="https://apply.example.test/2?x=1&amp;y=2"' in html_body

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["seen_indeed"] == seen_indeed
    assert state["seen_linkedin"] == seen_linkedin
    assert state["reported"][:1] == existing_reported
    assert [row["job_id"] for row in state["reported"][1:]] == ["id-2", "id-1"]
    assert state["reported"][1]["reported_at"] == EXPECTED_REPORTED_AT
    assert state["reported"][2]["reported_at"] == EXPECTED_REPORTED_AT
    assert state["reported"][2]["canonical_url"] == "https://jobs.example.test/1"


def test_untrusted_html_is_escaped_and_missing_display_values_use_placeholder(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(
        title="<script>alert(1)</script>", company=None, location="   ",
        validated=_validated(short_description="<b>unsafe & text</b>"),
    )])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    _, plain, html_body = _message_parts()
    assert plain.count("Not specified") == 2
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;b&gt;unsafe &amp; text&lt;/b&gt;" in html_body
    reported = json.loads((tmp_path / "validator/state.json").read_text())["reported"][0]
    assert reported["company"] == ""
    assert reported["location"] == "   "
    assert reported["title"] == "<script>alert(1)</script>"


def test_reported_dedupe_matches_prepare_semantics_and_does_not_dedupe_pending(tmp_path):
    reported = [
        {"source": "Indeed", "job_id": "known", "company": "", "title": "", "location": ""},
        {"source": "LinkedIn", "job_id": "other", "company": "Acme", "title": "Dev", "location": "EU"},
    ]
    _write_state(tmp_path, _state(reported=reported))
    duplicate_id = _job(1, job_id="known")
    duplicate_triple = _job(2, source="Indeed", company="Acme", title="Dev", location="EU")
    whitespace_identity = _job(3, job_id=" ", company=" ", title=" ", location=" ")
    duplicate_pending_a = _job(4, job_id="same", company="Same", title="Same", location="Same")
    duplicate_pending_b = _job(5, job_id="same", company="Same", title="Same", location="Same")
    _write_batch(tmp_path, [duplicate_id, duplicate_triple, whitespace_identity, duplicate_pending_a, duplicate_pending_b])
    summary = dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    assert summary["sent_count"] == 3
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert len(rows) == 5
    assert [row["job_id"] for row in rows[-3:]].count("same") == 2
    assert " " in [row["job_id"] for row in rows[-3:]]


def test_id_dedupe_is_source_specific_and_triple_is_source_independent(tmp_path):
    _write_state(tmp_path, _state(reported=[{
        "source": "Indeed", "job_id": "same-id", "company": "A", "title": "B", "location": "C",
    }]))
    _write_batch(tmp_path, [_job(1, source="LinkedIn", job_id="same-id", company="X", title="Y", location="Z")])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1


def test_noop_validates_inputs_but_needs_no_smtp_config_and_preserves_state_bytes(tmp_path):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job(status="REJECTED", validated=None)])
    original = state_path.read_bytes()
    assert dispatch_email(tmp_path, now=FIXED_TIME, env={}) == {"pending_count": 0, "sent_count": 0}
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_unfinalized_batch_is_ignored_without_validating_jobs(tmp_path):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, "malformed jobs are ignored", finalized=False)
    original = state_path.read_bytes()
    assert dispatch_email(tmp_path, env={})["sent_count"] == 0
    assert state_path.read_bytes() == original


def test_only_exact_batch_paths_are_scanned_in_sorted_order(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(2)], day="20260922", name="001.json")
    _write_batch(tmp_path, [_job(1)], day="20260921", name="999.json")
    _write_batch(tmp_path, [{"status": None}], day="20260921", name="debug.json")
    _write_batch(tmp_path, [{"status": None}], day="bad-day", name="001.json")
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 2


@pytest.mark.parametrize("finalized", [None, 0, 1, "true"])
def test_invalid_finalized_value_fails_without_state_change(tmp_path, finalized):
    state_path = _write_state(tmp_path)
    batch = _write_batch(tmp_path, [_job()], finalized=finalized)
    state_bytes, batch_bytes = state_path.read_bytes(), batch.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == state_bytes
    assert batch.read_bytes() == batch_bytes
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("status", [None, "PENDING", "OTHER", 1])
def test_invalid_status_in_finalized_batch_fails_before_send(tmp_path, status):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job(status=status)])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_missing_status_in_finalized_batch_fails_before_send(tmp_path):
    _write_state(tmp_path)
    job = _job()
    del job["status"]
    _write_batch(tmp_path, [job])
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("key", sorted(EXPECTED_VALIDATED_KEYS))
def test_missing_validated_key_fails_before_send(tmp_path, key):
    state_path = _write_state(tmp_path)
    validated = _validated()
    del validated[key]
    _write_batch(tmp_path, [_job(validated=validated)])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_dispatcher_schema_constant_matches_contract():
    assert dispatcher.VALIDATED_KEYS == frozenset(EXPECTED_VALIDATED_KEYS)
    assert dispatcher.HELSINKI.key == "Europe/Helsinki"


@pytest.mark.parametrize("overrides", [
    {"date_posted_precision": "exact", "date_posted_at": None},
    {"date_posted_precision": "exact", "date_posted_date": "2026-09-21"},
    {"date_posted_precision": "relative", "date_posted_date": "2026-09-21"},
    {"date_posted_precision": "date_only", "date_posted_at": "2026-09-21T10:00:00+00:00", "date_posted_date": "2026-09-21"},
    {"date_posted_precision": "date_only", "date_posted_at": None, "date_posted_date": None},
    {"date_posted_precision": "approximate", "date_posted_at": "2026-09-21T10:00:00+00:00"},
    {"date_posted_precision": "missing_or_ambiguous", "date_posted_date": "2026-09-21"},
    {"date_posted_precision": "unknown"},
    {"date_posted_at": "2026-09-21T10:00:00"},
    {"date_posted_date": "2026-02-30"},
])
def test_invalid_date_contract_fails_before_send(tmp_path, overrides):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job(validated=_validated(**overrides))])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("field", [
    "work_mode", "required_experience", "date_posted", "work_authorization",
    "residence_requirement", "short_description", "direct_application_link",
])
@pytest.mark.parametrize("bad_value", [None, "", "   ", 1])
def test_required_validated_strings_are_nonblank_strings(tmp_path, field, bad_value):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(validated=_validated(**{field: bad_value}))])
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("overrides", [
    {"source": "Other"}, {"job_id": 7}, {"url": "ftp://example.test/job"},
    {"url": " https://example.test/job"}, {"url": "https://example.test/a b"},
])
def test_invalid_qualified_identity_fails_before_send(tmp_path, overrides):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job(**overrides)])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_unhashable_invalid_source_fails_as_validation_error(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(source={"bad": True})])
    with pytest.raises(ValueError, match="unsupported source"):
        dispatch_email(tmp_path, env=ENV)


def test_sorting_groups_and_tiebreakers_are_deterministic(tmp_path):
    _write_state(tmp_path)
    jobs = [
        _job(1, company="Z", validated=_validated(date_posted_precision="date_only", date_posted_at=None, date_posted_date="1900-01-01")),
        _job(2, company="Z", validated=_validated(date_posted_precision="relative", date_posted_at=None)),
        _job(3, company="B", validated=_validated(date_posted_precision="approximate", date_posted_at=None)),
        _job(4, company="A", validated=_validated(date_posted_precision="missing_or_ambiguous", date_posted_at=None)),
        _job(5, company="B", title="Beta", validated=_validated(date_posted_at="2026-09-20T00:00:00+00:00")),
        _job(6, company="A", title="Alpha", validated=_validated(date_posted_at="2026-09-21T00:00:00+00:00")),
    ]
    _write_batch(tmp_path, jobs)
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert [row["job_id"] for row in rows] == ["id-6", "id-5", "id-2", "id-1", "id-3", "id-4"]


def test_starttls_without_auth_uses_expected_protocol_order(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    env = dict(ENV, SMTP_SECURITY="starttls", SMTP_PORT="587", SMTP_USERNAME="", SMTP_PASSWORD="")
    dispatch_email(tmp_path, now=FIXED_TIME, env=env)
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test" and smtp.port == 587
    assert smtp.events[0] == "enter"
    assert smtp.events[1][0] == "starttls"
    assert smtp.events[2] == (
        "send", "from@example.test", ["to@example.test"],
    )
    assert smtp.events[3] == "exit"


def test_ssl_with_auth_logs_in_before_send(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    smtp = FakeSMTP.instances[0]
    assert smtp.events[1:] == [
        ("login", "user", "secret"),
        ("send", "from@example.test", ["to@example.test"]),
        "exit",
    ]
    assert smtp.kwargs["timeout"] == dispatcher.SMTP_TIMEOUT_SECONDS
    assert smtp.kwargs["context"].verify_mode != 0


@pytest.mark.parametrize("updates", [
    {"SMTP_HOST": ""}, {"SMTP_PORT": "abc"}, {"SMTP_PORT": "0"},
    {"SMTP_SECURITY": "plain"}, {"EMAIL_FROM": "bad\nheader"},
    {"SMTP_USERNAME": "user", "SMTP_PASSWORD": ""},
])
def test_invalid_smtp_configuration_fails_before_connection_or_state_write(tmp_path, updates):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()
    env = dict(ENV, **updates)
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, now=FIXED_TIME, env=env)
    assert FakeSMTP.instances == []
    assert state_path.read_bytes() == original


@pytest.mark.parametrize("failure", [RuntimeError("send failed"), "refused"])
def test_smtp_failure_never_updates_state_or_batch(tmp_path, failure):
    state_path = _write_state(tmp_path)
    batch_path = _write_batch(tmp_path, [_job()])
    state_bytes, batch_bytes = state_path.read_bytes(), batch_path.read_bytes()
    if failure == "refused":
        FakeSMTP.refused = {"to@example.test": (550, b"no")}
    else:
        FakeSMTP.send_error = failure
    with pytest.raises(RuntimeError):
        dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    assert state_path.read_bytes() == state_bytes
    assert batch_path.read_bytes() == batch_bytes


def test_rerun_is_idempotent_and_batches_are_never_modified(tmp_path):
    state_path = _write_state(tmp_path)
    batch_path = _write_batch(tmp_path, [_job()])
    batch_bytes = batch_path.read_bytes()
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1
    state_after_first = state_path.read_bytes()
    FakeSMTP.instances = []
    assert dispatch_email(tmp_path, now=FIXED_TIME, env={})["sent_count"] == 0
    assert state_path.read_bytes() == state_after_first
    assert batch_path.read_bytes() == batch_bytes
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("payload", [
    [],
    {"seen_indeed": None, "seen_linkedin": [], "reported": []},
    {"seen_indeed": [], "seen_linkedin": None, "reported": []},
    {"seen_indeed": [], "seen_linkedin": [], "reported": None},
])
def test_invalid_state_fails_without_smtp(tmp_path, payload):
    state_path = _write_state(tmp_path, payload)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_no_batch_directory_and_no_matching_batches_are_noops(tmp_path):
    state_path = _write_state(tmp_path)
    original = state_path.read_bytes()
    assert dispatch_email(tmp_path, env={})["sent_count"] == 0
    _write_batch(tmp_path, [{"status": None}], name="01.json")
    assert dispatch_email(tmp_path, env={})["sent_count"] == 0
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("payload", [
    [],
    {},
    {"finalized": True, "jobs": {}},
    {"finalized": True, "jobs": [1]},
])
def test_malformed_matching_batch_fails_without_state_or_smtp(tmp_path, payload):
    state_path = _write_state(tmp_path)
    path = tmp_path / "validator/batches/20260921/001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original
    assert FakeSMTP.instances == []


def test_malformed_json_batch_fails_before_state_is_read(tmp_path):
    path = tmp_path / "validator/batches/20260921/001.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        dispatch_email(tmp_path, env=ENV)


@pytest.mark.parametrize("missing", ["seen_indeed", "seen_linkedin", "reported"])
def test_missing_state_ledger_fails(tmp_path, missing):
    payload = _state()
    del payload[missing]
    state_path = _write_state(tmp_path, payload)
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original


@pytest.mark.parametrize("ledger", ["seen_indeed", "seen_linkedin", "reported"])
def test_non_object_state_ledger_row_fails(tmp_path, ledger):
    payload = _state()
    payload[ledger].append("bad")
    _write_state(tmp_path, payload)
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)


@pytest.mark.parametrize("validated", [None, [], "bad"])
def test_qualified_validated_must_be_object(tmp_path, validated):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(validated=validated)])
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)
    assert FakeSMTP.instances == []


def test_extra_validated_key_fails(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(validated=_validated(extra="bad"))])
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=ENV)


@pytest.mark.parametrize("precision,posted_at,posted_date", [
    ("exact", "2026-09-21T12:00:00+03:00", None),
    ("relative", "2026-09-21T12:00:00+03:00", None),
    ("relative", None, None),
    ("date_only", None, "2026-09-21"),
    ("approximate", None, None),
    ("missing_or_ambiguous", None, None),
])
def test_all_valid_date_precision_shapes_dispatch(tmp_path, precision, posted_at, posted_date):
    _write_state(tmp_path)
    validated = _validated(
        date_posted_precision=precision,
        date_posted_at=posted_at,
        date_posted_date=posted_date,
    )
    _write_batch(tmp_path, [_job(validated=validated)])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1


@pytest.mark.parametrize("reported,candidate", [
    (
        {"source": "Indeed", "job_id": "ID", "company": "", "title": "", "location": ""},
        {"source": "Indeed", "job_id": "id", "company": "X", "title": "Y", "location": "Z"},
    ),
    (
        {"source": "Indeed", "job_id": " id ", "company": "", "title": "", "location": ""},
        {"source": "Indeed", "job_id": "id", "company": "X", "title": "Y", "location": "Z"},
    ),
    (
        {"source": "Indeed", "job_id": "old", "company": "ACME", "title": "Dev", "location": "EU"},
        {"source": "Indeed", "job_id": "new", "company": "Acme", "title": "Dev", "location": "EU"},
    ),
])
def test_reported_identity_has_no_case_or_whitespace_normalization(tmp_path, reported, candidate):
    _write_state(tmp_path, _state(reported=[reported]))
    _write_batch(tmp_path, [_job(**candidate)])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1


@pytest.mark.parametrize("incomplete", [
    {"company": "", "title": "Dev", "location": "EU"},
    {"company": "Acme", "title": None, "location": "EU"},
    {"company": "Acme", "title": "Dev", "location": 7},
])
def test_incomplete_reported_triple_does_not_dedupe(tmp_path, incomplete):
    reported = {"source": "Indeed", "job_id": "old", **incomplete}
    candidate = dict(incomplete)
    candidate["job_id"] = "new"
    _write_state(tmp_path, _state(reported=[reported]))
    _write_batch(tmp_path, [_job(**candidate)])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1


def test_urls_are_not_reported_dedupe_keys(tmp_path):
    reported = {
        "source": "Indeed", "job_id": "old", "company": "Old", "title": "Old",
        "location": "Old", "canonical_url": "https://jobs.example.test/shared",
    }
    _write_state(tmp_path, _state(reported=[reported]))
    _write_batch(tmp_path, [_job(
        job_id="new", url="https://jobs.example.test/shared",
        validated=_validated(direct_application_link="https://jobs.example.test/shared"),
    )])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1


@pytest.mark.parametrize("url", ["http://example.test/job", "https://example.test/job"])
def test_http_and_https_configured_urls_are_accepted(tmp_path, url):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(url=url, validated=_validated(direct_application_link="invalid-url"))])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    row = json.loads((tmp_path / "validator/state.json").read_text())["reported"][0]
    assert row["canonical_url"] == url


@pytest.mark.parametrize("direct_url", [
    "ftp://example.test/apply", "https:///missing-host", "https://example.test/a b",
    " https://example.test/padded", "https://example.test/padded ",
])
def test_unusable_nonblank_direct_url_falls_back_to_configured_url(tmp_path, direct_url):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(validated=_validated(direct_application_link=direct_url))])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    row = json.loads((tmp_path / "validator/state.json").read_text())["reported"][0]
    assert row["canonical_url"] == "https://jobs.example.test/1"


def test_date_only_value_does_not_affect_order_and_company_title_break_ties(tmp_path):
    _write_state(tmp_path)
    jobs = [
        _job(1, company="B", title="A", validated=_validated(
            date_posted_precision="date_only", date_posted_at=None, date_posted_date="2099-12-31")),
        _job(2, company="A", title="Z", validated=_validated(
            date_posted_precision="date_only", date_posted_at=None, date_posted_date="1900-01-01")),
        _job(3, company="A", title="A", validated=_validated(
            date_posted_precision="date_only", date_posted_at=None, date_posted_date="2000-01-01")),
    ]
    _write_batch(tmp_path, jobs)
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert [row["job_id"] for row in rows] == ["id-3", "id-2", "id-1"]


def test_injected_now_is_converted_to_helsinki_for_subject_and_reported_rows(tmp_path):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job(1), _job(2)])
    utc_now = datetime.fromisoformat("2026-01-15T20:30:45+00:00")
    dispatch_email(tmp_path, now=utc_now, env=ENV)
    message, _, _ = _message_parts()
    assert message["Subject"].endswith("2026-01-15 22:30")
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert {row["reported_at"] for row in rows} == {"2026-01-15T22:30:45+02:00"}


@pytest.mark.parametrize("updates", [
    {"EMAIL_RECIPIENT": "bad\rheader"},
    {"SMTP_USERNAME": "", "SMTP_PASSWORD": "secret"},
    {"SMTP_SECURITY": "SSL"},
])
def test_additional_invalid_smtp_configuration_fails_before_connection(tmp_path, updates):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()
    with pytest.raises(ValueError):
        dispatch_email(tmp_path, env=dict(ENV, **updates))
    assert FakeSMTP.instances == []
    assert state_path.read_bytes() == original


def test_tls_failure_and_authentication_failure_preserve_state(tmp_path, monkeypatch):
    class FailingSMTP(FakeSMTP):
        failure_point = "starttls"

        def starttls(self, **kwargs):
            raise RuntimeError("TLS failed")

        def login(self, username, password):
            raise RuntimeError("authentication failed")

    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()
    monkeypatch.setattr(dispatcher.smtplib, "SMTP", FailingSMTP)
    with pytest.raises(RuntimeError, match="TLS failed"):
        dispatch_email(tmp_path, env=dict(ENV, SMTP_SECURITY="starttls"))
    assert state_path.read_bytes() == original
    monkeypatch.setattr(dispatcher.smtplib, "SMTP_SSL", FailingSMTP)
    with pytest.raises(RuntimeError, match="authentication failed"):
        dispatch_email(tmp_path, env=ENV)
    assert state_path.read_bytes() == original


def test_future_state_serialization_is_checked_before_smtp(tmp_path, monkeypatch):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    real_dumps = dispatcher.json.dumps

    def fail_dumps(payload, *args, **kwargs):
        if isinstance(payload, dict) and payload.get("reported"):
            raise TypeError("not serializable")
        return real_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(dispatcher.json, "dumps", fail_dumps)
    with pytest.raises(TypeError, match="not serializable"):
        dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    assert FakeSMTP.instances == []


def test_atomic_write_failure_after_send_raises_and_leaves_original_state(tmp_path, monkeypatch):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()

    def fail_write(path, payload):
        raise OSError("disk failed")

    monkeypatch.setattr(dispatcher, "_atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk failed"):
        dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    assert len(FakeSMTP.instances) == 1
    assert state_path.read_bytes() == original


@pytest.mark.parametrize("variable", ["EMAIL_FROM", "EMAIL_RECIPIENT"])
@pytest.mark.parametrize("value", [
    "a@example.com, b@example.com",
    "a@example.com; b@example.com",
    "Display Name <>",
    "not-an-address",
])
def test_email_addresses_must_be_exactly_one_valid_mailbox(tmp_path, variable, value):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    original = state_path.read_bytes()
    with pytest.raises(ValueError, match="exactly one valid mailbox"):
        dispatch_email(tmp_path, now=FIXED_TIME, env=dict(ENV, **{variable: value}))
    assert FakeSMTP.instances == []
    assert state_path.read_bytes() == original


@pytest.mark.parametrize("sender,recipient,sender_mailbox,recipient_mailbox", [
    (
        "from@example.test", "to@example.test",
        "from@example.test", "to@example.test",
    ),
    (
        "Job Bot <bot@example.test>", "Hiring Team <jobs@example.test>",
        "bot@example.test", "jobs@example.test",
    ),
])
def test_valid_single_mailboxes_are_used_as_single_envelope_addresses(
    tmp_path, sender, recipient, sender_mailbox, recipient_mailbox,
):
    _write_state(tmp_path)
    _write_batch(tmp_path, [_job()])
    dispatch_email(
        tmp_path,
        now=FIXED_TIME,
        env=dict(ENV, EMAIL_FROM=sender, EMAIL_RECIPIENT=recipient),
    )
    smtp = FakeSMTP.instances[0]
    assert smtp.events[-2] == (
        "send", sender_mailbox, [recipient_mailbox],
    )
    assert smtp.message["From"] == sender
    assert smtp.message["To"] == recipient


def test_lone_surrogate_fails_utf8_preflight_before_smtp(tmp_path):
    state_path = _write_state(tmp_path)
    _write_batch(tmp_path, [_job(title="broken-\ud800-title")])
    original = state_path.read_bytes()
    with pytest.raises(UnicodeEncodeError):
        dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    assert FakeSMTP.instances == []
    assert state_path.read_bytes() == original


def test_normal_finnish_persian_and_emoji_text_dispatches_and_persists(tmp_path):
    _write_state(tmp_path)
    title = "Ohjelmistokehittäjä — توسعه‌دهنده 👩‍💻"
    _write_batch(tmp_path, [_job(title=title)])
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 1
    _, plain, html_body = _message_parts()
    assert title in plain
    assert title in html_body
    state = json.loads((tmp_path / "validator/state.json").read_text(encoding="utf-8"))
    assert state["reported"][0]["title"] == title


@pytest.mark.parametrize("field,value", [
    ("title", None), ("title", 7), ("title", "   "),
    ("company", None), ("company", 7), ("company", "   "),
    ("location", None), ("location", 7), ("location", "   "),
])
def test_display_placeholders_do_not_change_reported_originals(tmp_path, field, value):
    _write_state(tmp_path)
    job = _job(**{field: value})
    _write_batch(tmp_path, [job])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    _, plain, _ = _message_parts()
    assert f"{ {'title': 'Job Title', 'company': 'Company', 'location': 'Location'}[field]}: Not specified" in plain
    reported = json.loads((tmp_path / "validator/state.json").read_text())["reported"][0]
    assert reported[field] == (value if isinstance(value, str) else "")


def test_url_attribute_quotes_are_escaped(tmp_path):
    _write_state(tmp_path)
    dangerous = 'https://example.test/apply?value="quoted"&next=1'
    _write_batch(tmp_path, [_job(validated=_validated(direct_application_link=dangerous))])
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    message, _, html_body = _message_parts()
    assert '&quot;quoted&quot;' in html_body
    assert 'value="quoted"' not in html_body
    assert "Title 1" not in message["Subject"]
    assert "Company 1" not in message["From"]


def test_no_top_n_limit_and_each_pending_job_is_reported_once(tmp_path):
    _write_state(tmp_path)
    jobs = [_job(number) for number in range(1, 61)]
    _write_batch(tmp_path, jobs)
    assert dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)["sent_count"] == 60
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert len(rows) == 60
    assert len({row["job_id"] for row in rows}) == 60
    _, plain, _ = _message_parts()
    assert plain.count("Job Title:") == 60


def test_scan_order_breaks_complete_sort_ties_by_day_then_batch_filename(tmp_path):
    _write_state(tmp_path)

    def tied(number):
        return _job(number, company="Same", title="Same")

    _write_batch(tmp_path, [tied(3)], day="20260922", name="001.json")
    _write_batch(tmp_path, [tied(2)], day="20260921", name="010.json")
    _write_batch(tmp_path, [tied(1)], day="20260921", name="002.json")
    dispatch_email(tmp_path, now=FIXED_TIME, env=ENV)
    rows = json.loads((tmp_path / "validator/state.json").read_text())["reported"]
    assert [row["job_id"] for row in rows] == ["id-1", "id-2", "id-3"]
