"""Email finalized qualified Validator jobs and record them as reported."""

from __future__ import annotations

import copy
import html
import json
import os
import re
import smtplib
import ssl
import tempfile
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
HELSINKI = ZoneInfo("Europe/Helsinki")
SMTP_TIMEOUT_SECONDS = 30

FINAL_STATUSES = frozenset({"REJECTED", "QUALIFIED", "UNVALIDATED"})
VALIDATED_KEYS = frozenset({
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
})
VALIDATED_STRING_KEYS = frozenset({
    "work_mode",
    "required_experience",
    "date_posted",
    "work_authorization",
    "residence_requirement",
    "short_description",
    "direct_application_link",
})
DATE_POSTED_PRECISIONS = frozenset({
    "exact",
    "relative",
    "date_only",
    "approximate",
    "missing_or_ambiguous",
})

_DAY_NAME_RE = re.compile(r"^\d{8}$")
_BATCH_NAME_RE = re.compile(r"^\d{3}\.json$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SOURCES = frozenset({"Indeed", "LinkedIn"})
_PRECISION_RANK = {
    "relative": 1,
    "date_only": 2,
    "approximate": 3,
    "missing_or_ambiguous": 4,
}


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_state(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    for key in ("seen_indeed", "seen_linkedin", "reported"):
        records = payload.get(key)
        if not isinstance(records, list):
            raise ValueError(f"{path} must contain a {key} array")
        if any(not isinstance(record, dict) for record in records):
            raise ValueError(f"{path} {key} array must contain only objects")
    return payload


def _batch_paths(root: Path) -> list[Path]:
    batches_root = root / "validator/batches"
    if not batches_root.is_dir():
        return []
    paths: list[Path] = []
    for day_dir in sorted(batches_root.iterdir()):
        if not day_dir.is_dir() or _DAY_NAME_RE.fullmatch(day_dir.name) is None:
            continue
        for path in sorted(day_dir.iterdir()):
            if path.is_file() and _BATCH_NAME_RE.fullmatch(path.name) is not None:
                paths.append(path)
    return paths


def _identity_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _display_string(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else "Not specified"


def _usable_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(character.isspace() for character in value):
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _reported_triple(record: dict[str, Any]) -> tuple[str, str, str] | None:
    company = _identity_string(record.get("company"))
    title = _identity_string(record.get("title"))
    location = _identity_string(record.get("location"))
    if company is None or title is None or location is None:
        return None
    return company, title, location


def _reported_indexes(
    records: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], set[tuple[str, str, str]]]:
    ids = {source: set() for source in _SOURCES}
    triples: set[tuple[str, str, str]] = set()
    for record in records:
        source = record.get("source")
        job_id = _identity_string(record.get("job_id"))
        if isinstance(source, str) and source in ids and job_id is not None:
            ids[source].add(job_id)
        triple = _reported_triple(record)
        if triple is not None:
            triples.add(triple)
    return ids, triples


def _already_reported(
    job: dict[str, Any],
    reported_ids: dict[str, set[str]],
    reported_triples: set[tuple[str, str, str]],
) -> bool:
    source = job.get("source")
    job_id = _identity_string(job.get("job_id"))
    triple = _reported_triple(job)
    return (
        isinstance(source, str)
        and source in reported_ids
        and job_id is not None
        and job_id in reported_ids[source]
    ) or (triple is not None and triple in reported_triples)


def _parse_aware_datetime(value: Any, context: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{context} must be an ISO-8601 string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include a timezone offset")
    return parsed


def _validate_date_or_none(value: Any, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _DATE_ONLY_RE.fullmatch(value) is None:
        raise ValueError(f"{context} must be YYYY-MM-DD or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be a valid calendar date") from exc


def _validate_qualified_job(
    job: dict[str, Any], path: Path, index: int, scan_index: int,
) -> dict[str, Any]:
    context = f"{path} job {index}"
    source = job.get("source")
    if not isinstance(source, str) or source not in _SOURCES:
        raise ValueError(f"{context} has unsupported source {source!r}")
    job_id = job.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        raise ValueError(f"{context} has a non-string job_id")
    configured_url = _usable_http_url(job.get("url"))
    if configured_url is None:
        raise ValueError(f"{context} must have a usable http/https configured-source url")

    validated = job.get("validated")
    if not isinstance(validated, dict):
        raise ValueError(f"{context} with status QUALIFIED must have a validated object")
    if set(validated) != VALIDATED_KEYS:
        raise ValueError(f"{context} has invalid validated keys")
    for key in VALIDATED_STRING_KEYS:
        value = validated[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{context} validated.{key} must be a non-empty string")

    precision = validated["date_posted_precision"]
    if precision not in DATE_POSTED_PRECISIONS:
        raise ValueError(f"{context} has invalid date_posted_precision")
    posted_at = _parse_aware_datetime(
        validated["date_posted_at"], f"{context} validated.date_posted_at"
    )
    posted_date = validated["date_posted_date"]
    _validate_date_or_none(posted_date, f"{context} validated.date_posted_date")

    if precision == "exact" and (posted_at is None or posted_date is not None):
        raise ValueError(f"{context} exact precision requires date_posted_at and date_posted_date=null")
    if precision == "relative" and posted_date is not None:
        raise ValueError(f"{context} relative precision requires date_posted_date=null")
    if precision == "date_only" and (posted_at is not None or posted_date is None):
        raise ValueError(f"{context} date_only precision requires date_posted_at=null and date_posted_date")
    if precision in {"approximate", "missing_or_ambiguous"} and (
        posted_at is not None or posted_date is not None
    ):
        raise ValueError(f"{context} {precision} precision requires null date fields")

    direct_url = _usable_http_url(validated["direct_application_link"])
    canonical_url = direct_url if direct_url is not None else configured_url
    return {
        "job": job,
        "validated": validated,
        "canonical_url": canonical_url,
        "posted_at": posted_at,
        "scan_index": scan_index,
    }


def _sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    validated = candidate["validated"]
    posted_at = candidate["posted_at"]
    if posted_at is not None:
        rank = 0
        time_rank = -posted_at.timestamp()
    else:
        rank = _PRECISION_RANK[validated["date_posted_precision"]]
        time_rank = 0.0
    job = candidate["job"]
    return (
        rank,
        time_rank,
        _display_string(job.get("company")),
        _display_string(job.get("title")),
        candidate["scan_index"],
    )


def _display_rows(candidates: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    rows = []
    for candidate in candidates:
        job = candidate["job"]
        validated = candidate["validated"]
        rows.append([
            ("Job Title", _display_string(job.get("title"))),
            ("Company", _display_string(job.get("company"))),
            ("Location", _display_string(job.get("location"))),
            ("Work Mode", validated["work_mode"]),
            ("Required Experience", validated["required_experience"]),
            ("Date Posted", validated["date_posted"]),
            ("Visa / Work Authorization", validated["work_authorization"]),
            ("Current Residence Requirement", validated["residence_requirement"]),
            ("Short Description", validated["short_description"]),
            ("Direct Application Link", candidate["canonical_url"]),
        ])
    return rows


def _render_bodies(rows: list[list[tuple[str, str]]]) -> tuple[str, str]:
    plain_jobs = []
    html_jobs = []
    for number, fields in enumerate(rows, start=1):
        plain_jobs.append(f"Job {number}\n" + "\n".join(f"{label}: {value}" for label, value in fields))
        html_fields = []
        for label, value in fields:
            escaped_label = html.escape(label)
            escaped_value = html.escape(value)
            if label == "Direct Application Link":
                escaped_url = html.escape(value, quote=True)
                escaped_value = f'<a href="{escaped_url}">{escaped_value}</a>'
            html_fields.append(f"<dt>{escaped_label}</dt><dd>{escaped_value}</dd>")
        html_jobs.append(f"<section><h2>Job {number}</h2><dl>{''.join(html_fields)}</dl></section>")
    return "\n\n".join(plain_jobs) + "\n", "<!doctype html><html><body>" + "".join(html_jobs) + "</body></html>"


def _smtp_config(env: Mapping[str, str]) -> dict[str, Any]:
    def required(name: str) -> str:
        value = env.get(name)
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise ValueError(f"{name} must be a non-empty value without CR/LF")
        return value

    host = required("SMTP_HOST")
    port_text = required("SMTP_PORT")
    security = required("SMTP_SECURITY")
    sender = required("EMAIL_FROM")
    recipient = required("EMAIL_RECIPIENT")
    sender_mailbox = _single_mailbox("EMAIL_FROM", sender)
    recipient_mailbox = _single_mailbox("EMAIL_RECIPIENT", recipient)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535")
    if security not in {"ssl", "starttls"}:
        raise ValueError("SMTP_SECURITY must be ssl or starttls")
    username = env.get("SMTP_USERNAME", "")
    password = env.get("SMTP_PASSWORD", "")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("SMTP credentials must be strings")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must both be set or both be empty")
    return {
        "host": host, "port": port, "security": security,
        "sender": sender, "recipient": recipient,
        "sender_mailbox": sender_mailbox,
        "recipient_mailbox": recipient_mailbox,
        "username": username, "password": password,
    }


def _single_mailbox(variable: str, value: str) -> str:
    header = Parser(policy=policy.default).parsestr(
        f"To: {value}\n\n"
    )["To"]
    if header is None:
        raise ValueError(f"{variable} must contain exactly one valid mailbox")
    addresses = header.addresses
    has_named_group = any(group.display_name is not None for group in header.groups)
    if (
        header.defects
        or has_named_group
        or len(addresses) != 1
        or not addresses[0].username
        or not addresses[0].domain
    ):
        raise ValueError(f"{variable} must contain exactly one valid mailbox")
    return addresses[0].addr_spec


def _message(subject: str, sender: str, recipient: str, plain: str, html_body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    return message


def _send_message(message: EmailMessage, config: dict[str, Any]) -> None:
    context = ssl.create_default_context()
    connection: Any
    if config["security"] == "ssl":
        connection = smtplib.SMTP_SSL(
            config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    else:
        connection = smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT_SECONDS)
    with connection as client:
        if config["security"] == "starttls":
            client.starttls(context=context)
        if config["username"]:
            client.login(config["username"], config["password"])
        refused = client.send_message(
            message,
            from_addr=config["sender_mailbox"],
            to_addrs=[config["recipient_mailbox"]],
        )
        if refused:
            raise RuntimeError(f"SMTP refused {len(refused)} recipient(s)")


def _serialized_json_bytes(payload: Any) -> bytes:
    serialized = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    return serialized.encode("utf-8")


def _atomic_write_json(path: Path, serialized: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _qualified_jobs(root: Path) -> list[tuple[Path, int, dict[str, Any]]]:
    qualified: list[tuple[Path, int, dict[str, Any]]] = []
    for path in _batch_paths(root):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        finalized = payload.get("finalized")
        if finalized is False:
            continue
        if finalized is not True:
            raise ValueError(f"{path} must contain finalized=true or finalized=false")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise ValueError(f"{path} must contain a jobs array of objects")
        for index, job in enumerate(jobs):
            status = job.get("status")
            if status not in FINAL_STATUSES:
                raise ValueError(f"{path} job {index} has invalid status {status!r}")
            if status == "QUALIFIED":
                qualified.append((path, index, job))
    return qualified


def dispatch_email(
    root: Path = REPO_ROOT,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, int]:
    qualified = _qualified_jobs(root)
    state_path = root / "validator/state.json"
    state = _load_state(state_path)
    reported_ids, reported_triples = _reported_indexes(state["reported"])
    pending: list[tuple[Path, int, dict[str, Any], int]] = []

    for scan_index, (path, index, job) in enumerate(qualified):
        if not _already_reported(job, reported_ids, reported_triples):
            pending.append((path, index, job, scan_index))

    if not pending:
        return {"pending_count": 0, "sent_count": 0}

    candidates = [
        _validate_qualified_job(job, path, index, order)
        for path, index, job, order in pending
    ]
    candidates.sort(key=_sort_key)
    dispatch_time = now.astimezone(HELSINKI) if now is not None else datetime.now(HELSINKI)
    reported_at = dispatch_time.isoformat(timespec="seconds")
    subject = f".NET Backend Job Matches — Indeed + LinkedIn — {dispatch_time:%Y-%m-%d %H:%M}"
    rows = _display_rows(candidates)
    plain, html_body = _render_bodies(rows)

    updated_state = copy.deepcopy(state)
    for candidate in candidates:
        job = candidate["job"]
        updated_state["reported"].append({
            "source": job["source"],
            "job_id": job.get("job_id"),
            "company": job.get("company") if isinstance(job.get("company"), str) else "",
            "title": job.get("title") if isinstance(job.get("title"), str) else "",
            "location": job.get("location") if isinstance(job.get("location"), str) else "",
            "canonical_url": candidate["canonical_url"],
            "reported_at": reported_at,
        })
    serialized_state = _serialized_json_bytes(updated_state)

    config = _smtp_config(os.environ if env is None else env)
    message = _message(subject, config["sender"], config["recipient"], plain, html_body)
    _send_message(message, config)
    _atomic_write_json(state_path, serialized_state)
    return {"pending_count": len(candidates), "sent_count": len(candidates)}


def main() -> int:
    summary = dispatch_email()
    if summary["sent_count"]:
        print(f"Validator jobs emailed: {summary['sent_count']}")
    else:
        print("No qualified Validator jobs to email.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
