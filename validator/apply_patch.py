"""Apply small Validator result patches to batch files deterministically."""

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


REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_ROOT = Path("validator/patches")
MAX_PATCH_BYTES = 64 * 1024
MAX_RESULTS_PER_PATCH = 20
MAX_REASON_CHARS = 300

FINAL_STATUSES = frozenset({"REJECTED", "QUALIFIED", "UNVALIDATED"})
SOURCES = frozenset({"Indeed", "LinkedIn"})

PATCH_KEYS = frozenset({"schema_version", "batch", "results"})
RESULT_KEYS = frozenset({
    "index", "source", "job_id", "url", "status", "reason", "validated",
})
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
    "exact", "relative", "date_only", "approximate", "missing_or_ambiguous",
})

_PATCH_NAME_RE = re.compile(r"^(\d{8})-(\d{3})-(\d{3})\.json$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} must contain valid UTF-8 JSON") from exc


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _usable_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value != value.strip() or any(char.isspace() for char in value):
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _validate_iso_datetime_or_none(value: Any, context: str, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{context} {field_name} must be an ISO-8601 string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} {field_name} must be a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} {field_name} must include a timezone offset")


def _validate_iso_date_or_none(value: Any, context: str, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _DATE_ONLY_RE.fullmatch(value) is None:
        raise ValueError(f"{context} {field_name} must be YYYY-MM-DD or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} {field_name} must be a valid calendar date") from exc


def _validate_validated_output(validated: Any, context: str) -> None:
    if not isinstance(validated, dict):
        raise ValueError(f"{context} validated must be an object")
    if set(validated) != VALIDATED_KEYS:
        raise ValueError(f"{context} has invalid validated keys")

    for key in VALIDATED_STRING_KEYS:
        if _nonempty_string(validated[key]) is None:
            raise ValueError(f"{context} validated.{key} must be a non-empty string")

    precision = validated["date_posted_precision"]
    if precision not in DATE_POSTED_PRECISIONS:
        raise ValueError(f"{context} has invalid date_posted_precision")

    date_posted_at = validated["date_posted_at"]
    date_posted_date = validated["date_posted_date"]
    _validate_iso_datetime_or_none(date_posted_at, context, "validated.date_posted_at")
    _validate_iso_date_or_none(date_posted_date, context, "validated.date_posted_date")

    if precision == "exact":
        if date_posted_at is None or date_posted_date is not None:
            raise ValueError(
                f"{context} exact precision requires date_posted_at and date_posted_date=null"
            )
    elif precision == "relative":
        if date_posted_date is not None:
            raise ValueError(f"{context} relative precision requires date_posted_date=null")
    elif precision == "date_only":
        if date_posted_at is not None or date_posted_date is None:
            raise ValueError(
                f"{context} date_only precision requires date_posted_at=null and date_posted_date"
            )
    elif precision in {"approximate", "missing_or_ambiguous"}:
        if date_posted_at is not None or date_posted_date is not None:
            raise ValueError(
                f"{context} {precision} precision requires date_posted_at=null and date_posted_date=null"
            )


def _expected_batch_from_name(path: Path) -> tuple[str, int]:
    match = _PATCH_NAME_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"Invalid Validator patch filename: {path.name}")
    day, batch_number, first_index_text = match.groups()
    expected_batch = f"validator/batches/{day}/{batch_number}.json"
    return expected_batch, int(first_index_text)


def _validate_result(result: Any, context: str) -> None:
    if not isinstance(result, dict):
        raise ValueError(f"{context} must be an object")
    if set(result) != RESULT_KEYS:
        raise ValueError(f"{context} has invalid keys")

    index = result["index"]
    if type(index) is not int or not 0 <= index <= 999:
        raise ValueError(f"{context} index must be an integer from 0 to 999")

    source = result["source"]
    if source not in SOURCES:
        raise ValueError(f"{context} has unsupported source {source!r}")

    job_id = result["job_id"]
    if job_id is not None and not isinstance(job_id, str):
        raise ValueError(f"{context} job_id must be a string or null")

    if _usable_http_url(result["url"]) is None:
        raise ValueError(f"{context} url must be a usable http/https URL")

    status = result["status"]
    if status not in FINAL_STATUSES:
        raise ValueError(f"{context} has invalid status {status!r}")

    reason = result["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{context} reason must be a non-empty string")
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"{context} reason exceeds {MAX_REASON_CHARS} characters")
    if "\n" in reason or "\r" in reason:
        raise ValueError(f"{context} reason must be a single line")

    validated = result["validated"]
    if status in {"REJECTED", "UNVALIDATED"}:
        if validated is not None:
            raise ValueError(f"{context} with status {status} must have validated=null")
    else:
        _validate_validated_output(validated, context)


def _load_and_validate_patch(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > MAX_PATCH_BYTES:
        raise ValueError(f"{path} exceeds the {MAX_PATCH_BYTES}-byte patch size limit")

    expected_batch, filename_first_index = _expected_batch_from_name(path)
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    if set(payload) != PATCH_KEYS:
        raise ValueError(f"{path} has invalid top-level keys")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError(f"{path} schema_version must be exactly 1")
    if payload["batch"] != expected_batch:
        raise ValueError(f"{path} batch does not match its filename")

    results = payload["results"]
    if not isinstance(results, list) or not 1 <= len(results) <= MAX_RESULTS_PER_PATCH:
        raise ValueError(
            f"{path} results must contain 1..{MAX_RESULTS_PER_PATCH} entries"
        )

    previous_index = -1
    for position, result in enumerate(results):
        context = f"{path} result {position}"
        _validate_result(result, context)
        index = result["index"]
        if index <= previous_index:
            raise ValueError(f"{path} result indexes must be strictly ascending and unique")
        previous_index = index

    if results[0]["index"] != filename_first_index:
        raise ValueError(f"{path} filename index must equal the first result index")

    return payload


def _load_batch(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    if payload.get("finalized") is not False:
        raise ValueError(f"{path} must have finalized=false while applying patches")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError(f"{path} must contain a jobs array of objects")
    return payload


def _atomic_write_json(path: Path, payload: Any) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def apply_patches(root: Path = REPO_ROOT) -> dict[str, int]:
    patches_dir = root / PATCH_ROOT
    if not patches_dir.exists():
        return {
            "patches_discovered": 0,
            "patches_applied": 0,
            "patches_idempotent": 0,
            "results_applied": 0,
            "results_already_applied": 0,
            "consumed_patches": 0,
        }
    if not patches_dir.is_dir():
        raise ValueError(f"{patches_dir} must be a directory")

    patch_paths = sorted(
        path
        for path in patches_dir.iterdir()
        if path.is_file() and path.suffix == ".json"
    )
    patches: list[tuple[Path, dict[str, Any]]] = []
    seen_targets: set[tuple[str, int]] = set()

    for path in patch_paths:
        payload = _load_and_validate_patch(path)
        for result in payload["results"]:
            target = (payload["batch"], result["index"])
            if target in seen_targets:
                raise ValueError(
                    f"Overlapping Validator patch target: {target[0]} index {target[1]}"
                )
            seen_targets.add(target)
        patches.append((path, payload))

    batch_payloads: dict[str, dict[str, Any]] = {}
    original_batch_paths: dict[str, Path] = {}
    changed_batches: set[str] = set()
    patch_had_apply: dict[Path, bool] = {}
    results_applied = 0
    results_already_applied = 0

    for _, patch in patches:
        batch_rel = patch["batch"]
        if batch_rel not in batch_payloads:
            batch_path = root / batch_rel
            original_batch_paths[batch_rel] = batch_path
            batch_payloads[batch_rel] = copy.deepcopy(_load_batch(batch_path))

    for patch_path, patch in patches:
        batch_rel = patch["batch"]
        batch = batch_payloads[batch_rel]
        jobs = batch["jobs"]
        patch_applied = False

        for result in patch["results"]:
            index = result["index"]
            if index >= len(jobs):
                raise ValueError(
                    f"{patch_path} index {index} is outside the target batch"
                )

            job = jobs[index]
            for key in ("source", "job_id", "url", "status", "reason", "validated"):
                if key not in job:
                    raise ValueError(
                        f"{patch_path} target job {index} is missing {key}"
                    )

            if (
                job["source"] != result["source"]
                or job["job_id"] != result["job_id"]
                or job["url"] != result["url"]
            ):
                raise ValueError(
                    f"{patch_path} identity mismatch at job index {index}"
                )

            existing_status = job["status"]
            if existing_status is None:
                if job["reason"] is not None or job["validated"] is not None:
                    raise ValueError(
                        f"{patch_path} pending job {index} has unexpected result data"
                    )
                job["status"] = result["status"]
                job["reason"] = result["reason"]
                job["validated"] = copy.deepcopy(result["validated"])
                changed_batches.add(batch_rel)
                patch_applied = True
                results_applied += 1
                continue

            if (
                existing_status == result["status"]
                and job["reason"] == result["reason"]
                and job["validated"] == result["validated"]
            ):
                results_already_applied += 1
                continue

            raise ValueError(
                f"{patch_path} conflicts with existing result at job index {index}"
            )

        patch_had_apply[patch_path] = patch_applied

    # No repository mutation occurs before the entire queue has validated and simulated.
    for batch_rel in sorted(changed_batches):
        _atomic_write_json(
            original_batch_paths[batch_rel],
            batch_payloads[batch_rel],
        )

    for patch_path, _ in patches:
        patch_path.unlink()

    return {
        "patches_discovered": len(patches),
        "patches_applied": sum(
            1 for applied in patch_had_apply.values() if applied
        ),
        "patches_idempotent": sum(
            1 for applied in patch_had_apply.values() if not applied
        ),
        "results_applied": results_applied,
        "results_already_applied": results_already_applied,
        "consumed_patches": len(patches),
    }


def _print_summary(summary: dict[str, int]) -> None:
    print(f"Patches discovered: {summary['patches_discovered']}")
    print(f"Patches applied: {summary['patches_applied']}")
    print(f"Patches idempotent: {summary['patches_idempotent']}")
    print(f"Results applied: {summary['results_applied']}")
    print(f"Results already applied: {summary['results_already_applied']}")
    print(f"Consumed patches: {summary['consumed_patches']}")


def main() -> int:
    summary = apply_patches()
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
