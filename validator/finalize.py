"""Finalize completed Validator batches and record rejected jobs as seen."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
HELSINKI = ZoneInfo("Europe/Helsinki")
OPEN_BATCH_ROOT = Path("validator/open_batches")

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

_SEEN_LEDGER_BY_SOURCE = {
    "Indeed": "seen_indeed",
    "LinkedIn": "seen_linkedin",
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
            raise ValueError(
                f"{path} {key} array must contain only objects"
            )

    return payload


def _batch_paths(root: Path) -> list[Path]:
    batches_root = root / "validator/batches"

    if not batches_root.is_dir():
        return []

    paths: list[Path] = []

    for day_dir in sorted(batches_root.iterdir()):
        if (
            not day_dir.is_dir()
            or _DAY_NAME_RE.fullmatch(day_dir.name) is None
        ):
            continue

        for path in sorted(day_dir.iterdir()):
            if (
                path.is_file()
                and _BATCH_NAME_RE.fullmatch(path.name) is not None
            ):
                paths.append(path)

    return paths


def _open_marker_path(root: Path, batch_path: Path) -> Path:
    relative = batch_path.relative_to(root / "validator/batches")
    return root / OPEN_BATCH_ROOT / (
        f"{relative.parent.name}-{relative.stem}.open"
    )


def _sidecar_open_marker_path(batch_path: Path) -> Path:
    return batch_path.with_suffix(".open")


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value

    return None


def _usable_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None

    # Do not silently normalize configured-source URLs.
    if value != value.strip() or any(char.isspace() for char in value):
        return None

    parsed = urlparse(value)

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    if not parsed.netloc:
        return None

    return value


def _validate_iso_datetime_or_none(
    value: Any,
    context: str,
    field_name: str,
) -> None:
    if value is None:
        return

    if not isinstance(value, str):
        raise ValueError(
            f"{context} {field_name} must be an ISO-8601 string or null"
        )

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{context} {field_name} must be a valid ISO-8601 datetime"
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{context} {field_name} must include a timezone offset"
        )


def _validate_iso_date_or_none(
    value: Any,
    context: str,
    field_name: str,
) -> None:
    if value is None:
        return

    if (
        not isinstance(value, str)
        or _DATE_ONLY_RE.fullmatch(value) is None
    ):
        raise ValueError(
            f"{context} {field_name} must be YYYY-MM-DD or null"
        )

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{context} {field_name} must be a valid calendar date"
        ) from exc


def _validate_final_identity(
    job: dict[str, Any],
    context: str,
) -> None:
    source = job.get("source")

    if source not in _SEEN_LEDGER_BY_SOURCE:
        raise ValueError(
            f"{context} has unsupported source {source!r}"
        )

    job_id = job.get("job_id")

    if job_id is not None and not isinstance(job_id, str):
        raise ValueError(
            f"{context} has a non-string job_id"
        )

    if _usable_http_url(job.get("url")) is None:
        raise ValueError(
            f"{context} must have a usable http/https "
            "configured-source url"
        )


def _validate_validated_output(
    validated: dict[str, Any],
    context: str,
) -> None:
    if set(validated) != VALIDATED_KEYS:
        raise ValueError(
            f"{context} has invalid validated keys"
        )

    for key in VALIDATED_STRING_KEYS:
        if _nonempty_string(validated[key]) is None:
            raise ValueError(
                f"{context} validated.{key} "
                "must be a non-empty string"
            )

    precision = validated["date_posted_precision"]

    if precision not in DATE_POSTED_PRECISIONS:
        raise ValueError(
            f"{context} has invalid date_posted_precision"
        )

    date_posted_at = validated["date_posted_at"]
    date_posted_date = validated["date_posted_date"]

    _validate_iso_datetime_or_none(
        date_posted_at,
        context,
        "validated.date_posted_at",
    )

    _validate_iso_date_or_none(
        date_posted_date,
        context,
        "validated.date_posted_date",
    )

    if precision == "exact":
        if date_posted_at is None or date_posted_date is not None:
            raise ValueError(
                f"{context} exact precision requires "
                "date_posted_at and date_posted_date=null"
            )

    elif precision == "relative":
        if date_posted_date is not None:
            raise ValueError(
                f"{context} relative precision requires "
                "date_posted_date=null"
            )

    elif precision == "date_only":
        if date_posted_at is not None or date_posted_date is None:
            raise ValueError(
                f"{context} date_only precision requires "
                "date_posted_at=null and date_posted_date"
            )

    elif precision in {"approximate", "missing_or_ambiguous"}:
        if date_posted_at is not None or date_posted_date is not None:
            raise ValueError(
                f"{context} {precision} precision requires "
                "date_posted_at=null and date_posted_date=null"
            )


def _validate_completed_job(
    job: dict[str, Any],
    path: Path,
    index: int,
) -> None:
    status = job.get("status")
    reason = _nonempty_string(job.get("reason"))
    validated = job.get("validated")
    context = f"{path} job {index}"

    if status not in FINAL_STATUSES:
        raise ValueError(
            f"{context} has invalid status {status!r}"
        )

    if reason is None:
        raise ValueError(
            f"{context} must have a non-empty reason"
        )

    if status == "UNVALIDATED":
        if validated is not None:
            raise ValueError(
                f"{context} with status UNVALIDATED "
                "must have validated=null"
            )

        # Input may legitimately be malformed/missing because that
        # may be the reason validation could not complete.
        return

    # REJECTED and QUALIFIED must retain usable source identity.
    _validate_final_identity(job, context)

    if status == "REJECTED":
        if validated is not None:
            raise ValueError(
                f"{context} with status REJECTED "
                "must have validated=null"
            )

        return

    if not isinstance(validated, dict):
        raise ValueError(
            f"{context} with status QUALIFIED "
            "must have a validated object"
        )

    _validate_validated_output(
        validated,
        context,
    )


def _seen_indexes(
    records: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    ids = {
        job_id
        for record in records
        if (
            job_id := _nonempty_string(record.get("job_id"))
        ) is not None
    }

    urls = {
        job_url
        for record in records
        if (
            job_url := _usable_http_url(record.get("job_url"))
        ) is not None
    }

    return ids, urls


def _seen_row(
    job: dict[str, Any],
    seen_at: str,
    path: Path,
    index: int,
) -> tuple[str, dict[str, str]]:
    source = job.get("source")

    if source not in _SEEN_LEDGER_BY_SOURCE:
        raise ValueError(
            f"{path} job {index} "
            f"has unsupported source {source!r}"
        )

    job_id_value = job.get("job_id")

    if job_id_value is None:
        job_id = ""

    elif isinstance(job_id_value, str):
        job_id = (
            job_id_value
            if job_id_value.strip()
            else ""
        )

    else:
        raise ValueError(
            f"{path} job {index} has a non-string job_id"
        )

    job_url = _usable_http_url(
        job.get("url")
    )

    if job_url is None:
        raise ValueError(
            f"{path} job {index} must have a usable "
            "http/https configured-source url"
        )

    return _SEEN_LEDGER_BY_SOURCE[source], {
        "job_id": job_id,
        "job_url": job_url,
        "seen_at": seen_at,
    }


def _atomic_write_json(
    path: Path,
    payload: Any,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )

    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            temporary_path,
            path,
        )

    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def finalize_batches(
    root: Path = REPO_ROOT,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    state_path = root / "validator/state.json"
    state = _load_state(state_path)
    batch_paths = _batch_paths(root)

    eligible_batches: list[
        tuple[Path, dict[str, Any]]
    ] = []

    pending_batches = 0
    pending_marker_paths: list[Path] = []
    pending_sidecar_marker_paths: list[Path] = []
    stale_marker_paths: list[Path] = []
    stale_sidecar_marker_paths: list[Path] = []

    # Validate every candidate batch before mutating state.
    for path in batch_paths:
        payload = _load_json(path)

        if not isinstance(payload, dict):
            raise ValueError(
                f"{path} must contain a JSON object"
            )

        finalized = payload.get("finalized")
        marker_path = _open_marker_path(root, path)
        sidecar_marker_path = _sidecar_open_marker_path(path)

        if finalized is True:
            if marker_path.exists():
                stale_marker_paths.append(marker_path)
            if sidecar_marker_path.exists():
                stale_sidecar_marker_paths.append(sidecar_marker_path)
            continue

        if finalized is not False:
            raise ValueError(
                f"{path} must contain "
                "finalized=true or finalized=false"
            )

        jobs = payload.get("jobs")

        if (
            not isinstance(jobs, list)
            or any(
                not isinstance(job, dict)
                for job in jobs
            )
        ):
            raise ValueError(
                f"{path} must contain "
                "a jobs array of objects"
            )

        statuses = [
            job.get("status")
            for job in jobs
        ]

        invalid_statuses = [
            status
            for status in statuses
            if (
                status is not None
                and status not in FINAL_STATUSES
            )
        ]

        if invalid_statuses:
            raise ValueError(
                f"{path} contains invalid job status "
                f"{invalid_statuses[0]!r}"
            )

        if any(
            status is None
            for status in statuses
        ):
            pending_batches += 1
            pending_marker_paths.append(marker_path)
            pending_sidecar_marker_paths.append(sidecar_marker_path)
            continue

        for index, job in enumerate(
            jobs
        ):
            _validate_completed_job(
                job,
                path,
                index,
            )

        eligible_batches.append(
            (path, payload)
        )

    updated_state = copy.deepcopy(
        state
    )

    seen_indexes = {
        ledger: _seen_indexes(
            updated_state[ledger]
        )
        for ledger in (
            "seen_indeed",
            "seen_linkedin",
        )
    }

    timestamp = (
        now.astimezone(HELSINKI)
        if now is not None
        else datetime.now(HELSINKI)
    ).isoformat(timespec="seconds")

    seen_added = 0

    # Prepare all Seen changes in memory first.
    for path, payload in eligible_batches:
        for index, job in enumerate(
            payload["jobs"]
        ):
            if job["status"] != "REJECTED":
                continue

            ledger, row = _seen_row(
                job,
                timestamp,
                path,
                index,
            )

            seen_ids, seen_urls = (
                seen_indexes[ledger]
            )

            duplicate_id = (
                bool(row["job_id"])
                and row["job_id"] in seen_ids
            )

            duplicate_url = (
                row["job_url"]
                in seen_urls
            )

            if duplicate_id or duplicate_url:
                continue

            updated_state[ledger].append(
                row
            )

            if row["job_id"]:
                seen_ids.add(
                    row["job_id"]
                )

            seen_urls.add(
                row["job_url"]
            )

            seen_added += 1

    finalized_payloads: list[
        tuple[Path, dict[str, Any]]
    ] = []

    for path, payload in eligible_batches:
        finalized_payload = copy.deepcopy(
            payload
        )

        finalized_payload["finalized"] = True

        finalized_payloads.append(
            (path, finalized_payload)
        )

    # Persist state before marking any batch finalized.
    # If a later batch write fails, rerunning remains safe
    # because Seen writes are idempotent.
    if updated_state != state:
        _atomic_write_json(
            state_path,
            updated_state,
        )

    for path, payload in finalized_payloads:
        _atomic_write_json(
            path,
            payload,
        )

    markers_created: list[Path] = []
    for marker_path in pending_marker_paths:
        if marker_path.exists():
            if not marker_path.is_file():
                raise ValueError(
                    f"{marker_path} must be a regular file"
                )
            continue

        marker_path.parent.mkdir(parents=True, exist_ok=True)
        with marker_path.open("x", encoding="utf-8") as handle:
            handle.write("open\n")
        markers_created.append(marker_path)

    sidecar_markers_created: list[Path] = []
    for marker_path in pending_sidecar_marker_paths:
        if marker_path.exists():
            if not marker_path.is_file():
                raise ValueError(
                    f"{marker_path} must be a regular file"
                )
            continue

        with marker_path.open("x", encoding="utf-8") as handle:
            handle.write("open\n")
        sidecar_markers_created.append(marker_path)

    markers_removed: list[Path] = []
    markers_to_remove = stale_marker_paths + [
        _open_marker_path(root, path)
        for path, _ in finalized_payloads
    ]
    for marker_path in markers_to_remove:
        if not marker_path.exists():
            continue
        if not marker_path.is_file():
            raise ValueError(
                f"{marker_path} must be a regular file"
            )
        marker_path.unlink()
        markers_removed.append(marker_path)

    sidecar_markers_removed: list[Path] = []
    sidecar_markers_to_remove = stale_sidecar_marker_paths + [
        _sidecar_open_marker_path(path)
        for path, _ in finalized_payloads
    ]
    for marker_path in sidecar_markers_to_remove:
        if not marker_path.exists():
            continue
        if not marker_path.is_file():
            raise ValueError(
                f"{marker_path} must be a regular file"
            )
        marker_path.unlink()
        sidecar_markers_removed.append(marker_path)

    return {
        "scanned_batches": len(batch_paths),
        "pending_batches": pending_batches,
        "finalized_files": [
            path
            for path, _ in finalized_payloads
        ],
        "seen_added": seen_added,
        "markers_created": markers_created,
        "sidecar_markers_created": sidecar_markers_created,
        "markers_removed": markers_removed,
        "sidecar_markers_removed": sidecar_markers_removed,
    }


def _print_summary(
    summary: dict[str, Any],
    root: Path = REPO_ROOT,
) -> None:
    print(
        f"Batches scanned: "
        f"{summary['scanned_batches']}"
    )

    print(
        "Pending batches left unchanged: "
        f"{summary['pending_batches']}"
    )

    print(
        "Batches finalized: "
        f"{len(summary['finalized_files'])}"
    )

    print(
        f"Seen rows added: "
        f"{summary['seen_added']}"
    )

    print(
        f"Open markers created: "
        f"{len(summary['markers_created'])}"
    )

    print(
        f"Sidecar open markers created: "
        f"{len(summary['sidecar_markers_created'])}"
    )

    print(
        f"Open markers removed: "
        f"{len(summary['markers_removed'])}"
    )

    print(
        f"Sidecar open markers removed: "
        f"{len(summary['sidecar_markers_removed'])}"
    )

    if summary["finalized_files"]:
        names = [
            str(path.relative_to(root))
            for path
            in summary["finalized_files"]
        ]

        print(
            f"Finalized files: "
            f"{', '.join(names)}"
        )


def main() -> int:
    summary = finalize_batches()
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())