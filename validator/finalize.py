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

try:
    from validator.batch_storage import (
        BatchRef,
        discover_batch_refs,
        first_pending_shard,
        flatten_shards,
        load_all_shards,
        load_manifest,
        load_monolithic,
        validate_job_statuses,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from batch_storage import (
        BatchRef,
        discover_batch_refs,
        first_pending_shard,
        flatten_shards,
        load_all_shards,
        load_manifest,
        load_monolithic,
        validate_job_statuses,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
HELSINKI = ZoneInfo("Europe/Helsinki")
OPEN_BATCH_INDEX = Path("validator/open_batches.json")

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


def _batch_refs(root: Path) -> list[BatchRef]:
    return discover_batch_refs(root)


def _batch_paths(root: Path) -> list[Path]:
    """Compatibility helper returning logical-batch metadata paths."""
    return [ref.metadata_path for ref in _batch_refs(root)]


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
    batch_refs = _batch_refs(root)

    eligible_batches: list[
        tuple[BatchRef, dict[str, Any], list[dict[str, Any]]]
    ] = []

    pending_batches = 0
    pending_entries: list[dict[str, Any]] = []

    # Validate every non-finalized logical batch before mutating state.
    for ref in batch_refs:
        if ref.storage == "monolithic":
            metadata = load_monolithic(ref)
            if metadata["finalized"] is True:
                continue

            jobs = metadata["jobs"]
            validate_job_statuses(jobs, str(ref.metadata_path))

            if any(job.get("status") is None for job in jobs):
                pending_batches += 1
                pending_entries.append({
                    "batch_id": ref.batch_id,
                    "storage": "monolithic",
                    "shard_id": None,
                })
                continue
        else:
            metadata = load_manifest(ref)
            if metadata["finalized"] is True:
                continue

            shards = load_all_shards(
                root,
                ref,
                manifest=metadata,
            )
            jobs = flatten_shards(shards)
            next_shard = first_pending_shard(shards)

            if next_shard is not None:
                pending_batches += 1
                pending_entries.append({
                    "batch_id": ref.batch_id,
                    "storage": "sharded",
                    "shard_id": next_shard,
                })
                continue

        for index, job in enumerate(jobs):
            _validate_completed_job(
                job,
                ref.metadata_path,
                index,
            )

        eligible_batches.append(
            (ref, metadata, jobs)
        )

    updated_state = copy.deepcopy(state)

    seen_indexes = {
        ledger: _seen_indexes(updated_state[ledger])
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
    for ref, _, jobs in eligible_batches:
        for index, job in enumerate(jobs):
            if job["status"] != "REJECTED":
                continue

            ledger, row = _seen_row(
                job,
                timestamp,
                ref.metadata_path,
                index,
            )

            seen_ids, seen_urls = seen_indexes[ledger]

            duplicate_id = (
                bool(row["job_id"])
                and row["job_id"] in seen_ids
            )
            duplicate_url = row["job_url"] in seen_urls

            if duplicate_id or duplicate_url:
                continue

            updated_state[ledger].append(row)

            if row["job_id"]:
                seen_ids.add(row["job_id"])
            seen_urls.add(row["job_url"])
            seen_added += 1

    finalized_payloads: list[
        tuple[Path, dict[str, Any]]
    ] = []

    for ref, metadata, _ in eligible_batches:
        finalized_payload = copy.deepcopy(metadata)
        finalized_payload["finalized"] = True
        finalized_payloads.append(
            (ref.metadata_path, finalized_payload)
        )

    # Persist state before marking any logical batch finalized.
    # If a later metadata write fails, rerunning remains safe because
    # Seen writes are idempotent.
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

    pending_entries = sorted(
        pending_entries,
        key=lambda entry: entry["batch_id"],
        reverse=True,
    )
    pending_batch_ids = [
        entry["batch_id"]
        for entry in pending_entries
    ]

    # Keep schema v1 byte/shape compatibility until sharded storage actually
    # exists. The first real v2 logical batch deterministically activates
    # schema v2 for the derived discovery index.
    use_index_v2 = any(
        ref.storage == "sharded"
        for ref in batch_refs
    )
    if use_index_v2:
        open_batch_index = {
            "schema_version": 2,
            "batches": pending_entries,
        }
    else:
        open_batch_index = {
            "schema_version": 1,
            "batches": pending_batch_ids,
        }

    open_batch_index_path = root / OPEN_BATCH_INDEX

    try:
        existing_open_batch_index = (
            _load_json(open_batch_index_path)
            if open_batch_index_path.exists()
            else None
        )
    except (OSError, json.JSONDecodeError):
        existing_open_batch_index = None

    open_batch_index_updated = (
        existing_open_batch_index != open_batch_index
    )
    if open_batch_index_updated:
        _atomic_write_json(
            open_batch_index_path,
            open_batch_index,
        )

    return {
        "scanned_batches": len(batch_refs),
        "pending_batches": pending_batches,
        "finalized_files": [
            path
            for path, _ in finalized_payloads
        ],
        "seen_added": seen_added,
        "open_batches": pending_batch_ids,
        "open_batch_index_schema_version": open_batch_index["schema_version"],
        "open_batch_index_updated": open_batch_index_updated,
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
        f"Open batches indexed: "
        f"{len(summary['open_batches'])}"
    )

    print(
        f"Open batch index updated: "
        f"{summary['open_batch_index_updated']}"
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