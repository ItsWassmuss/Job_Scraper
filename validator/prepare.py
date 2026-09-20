"""Build daily Validator batches from the current Indeed and LinkedIn outputs."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent.parent
HELSINKI = ZoneInfo("Europe/Helsinki")
BATCH_SIZE = 50
SOURCES = (
    ("Indeed", "seen_indeed", Path("output/indeed_jobs.json")),
    ("LinkedIn", "seen_linkedin", Path("output/linkedin_jobs.json")),
)
_BATCH_NAME_RE = re.compile(r"^(\d{3})\.json$")
_LINKEDIN_JOB_PATH_RE = re.compile(r"/jobs/view/(\d+)(?:/|$)")


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_source_jobs(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError(f"{path} must contain a top-level jobs array")
    jobs = payload["jobs"]
    if any(not isinstance(job, dict) for job in jobs):
        raise ValueError(f"{path} jobs array must contain only objects")
    return jobs


def _load_state(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    required = ("seen_indeed", "seen_linkedin", "reported")
    for key in required:
        if not isinstance(payload.get(key), list):
            raise ValueError(f"{path} must contain a {key} array")
        if any(not isinstance(record, dict) for record in payload[key]):
            raise ValueError(f"{path} {key} array must contain only objects")
    return {key: payload[key] for key in required}


def _source_url(job: dict[str, Any]) -> str:
    value = job.get("url")
    return value if isinstance(value, str) else ""


def _indeed_job_id(url: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get("jk", [])
    return next((value for value in values if value), None)


def _linkedin_job_id(url: str) -> str | None:
    match = _LINKEDIN_JOB_PATH_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


def _job_id(source: str, url: str) -> str | None:
    if source == "Indeed":
        return _indeed_job_id(url)
    if source == "LinkedIn":
        return _linkedin_job_id(url)
    raise ValueError(f"Unsupported source: {source}")


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _batch_identity(job: dict[str, Any]) -> tuple[str, str, str] | None:
    source = _nonempty_string(job.get("source"))
    if source is None:
        return None
    job_id = _nonempty_string(job.get("job_id"))
    if job_id is not None:
        return ("id", source, job_id)
    url = _nonempty_string(job.get("url"))
    if url is not None:
        return ("url", source, url)
    return None


def _reported_triple(job: dict[str, Any]) -> tuple[str, str, str] | None:
    company = _nonempty_string(job.get("company"))
    title = _nonempty_string(job.get("title"))
    location = _nonempty_string(job.get("location"))
    if company is None or title is None or location is None:
        return None
    return (company, title, location)


def _state_indexes(
    state: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]], set[tuple[str, str, str]]]:
    seen_ids: dict[str, set[str]] = {"Indeed": set(), "LinkedIn": set()}
    seen_urls: dict[str, set[str]] = {"Indeed": set(), "LinkedIn": set()}
    for source, state_key, _ in SOURCES:
        for record in state[state_key]:
            job_id = _nonempty_string(record.get("job_id"))
            job_url = _nonempty_string(record.get("job_url"))
            if job_id is not None:
                seen_ids[source].add(job_id)
            if job_url is not None:
                seen_urls[source].add(job_url)

    reported_ids: dict[str, set[str]] = {"Indeed": set(), "LinkedIn": set()}
    reported_triples: set[tuple[str, str, str]] = set()
    for record in state["reported"]:
        source = record.get("source")
        job_id = _nonempty_string(record.get("job_id"))
        if source in reported_ids and job_id is not None:
            reported_ids[source].add(job_id)
        reported_triple = _reported_triple(record)
        if reported_triple is not None:
            reported_triples.add(reported_triple)

    return seen_ids, seen_urls, reported_ids, reported_triples


def _load_existing_batch_keys(day_dir: Path) -> tuple[set[tuple[str, str, str]], int]:
    keys: set[tuple[str, str, str]] = set()
    largest_number = 0
    if not day_dir.exists():
        return keys, largest_number

    for path in sorted(day_dir.glob("*.json")):
        name_match = _BATCH_NAME_RE.fullmatch(path.name)
        if name_match is None:
            continue
        largest_number = max(largest_number, int(name_match.group(1)))
        payload = _load_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"{path} must contain a top-level jobs array")
        for job in payload["jobs"]:
            if not isinstance(job, dict):
                raise ValueError(f"{path} jobs array must contain only objects")
            identity = _batch_identity(job)
            if identity is not None:
                keys.add(identity)
    return keys, largest_number


def _is_excluded_by_state(
    job: dict[str, Any],
    source: str,
    job_id: str | None,
    url: str,
    seen_ids: dict[str, set[str]],
    seen_urls: dict[str, set[str]],
    reported_ids: dict[str, set[str]],
    reported_triples: set[tuple[str, str, str]],
) -> bool:
    reported_triple = _reported_triple(job)
    return (
        (job_id is not None and job_id in seen_ids[source])
        or (bool(url) and url in seen_urls[source])
        or (job_id is not None and job_id in reported_ids[source])
        or (reported_triple is not None and reported_triple in reported_triples)
    )


def prepare_batches(root: Path = REPO_ROOT, *, now: datetime | None = None) -> dict[str, Any]:
    state = _load_state(root / "validator/state.json")
    seen_ids, seen_urls, reported_ids, reported_triples = _state_indexes(state)

    created_at = now.astimezone(HELSINKI) if now is not None else datetime.now(HELSINKI)
    day_dir = root / "validator/batches" / created_at.strftime("%Y%m%d")
    existing_batch_keys, largest_batch_number = _load_existing_batch_keys(day_dir)

    source_counts: dict[str, int] = {}
    removed_by_state = 0
    removed_by_batches = 0
    removed_within_run = 0
    skipped_missing_identity = 0
    ready_jobs: list[dict[str, Any]] = []
    run_keys: set[tuple[str, str, str]] = set()

    for source, _, relative_path in SOURCES:
        source_jobs = _load_source_jobs(root / relative_path)
        source_counts[source] = len(source_jobs)
        for source_job in source_jobs:
            url = _source_url(source_job)
            job_id = _job_id(source, url)
            if _is_excluded_by_state(
                source_job,
                source,
                job_id,
                url,
                seen_ids,
                seen_urls,
                reported_ids,
                reported_triples,
            ):
                removed_by_state += 1
                continue

            prepared_job = dict(source_job)
            prepared_job.update({
                "source": source,
                "job_id": job_id,
                "status": None,
                "reason": None,
                "validated": None,
            })
            identity = _batch_identity(prepared_job)
            if identity is None:
                skipped_missing_identity += 1
                continue
            if identity is not None and identity in existing_batch_keys:
                removed_by_batches += 1
                continue
            if identity is not None and identity in run_keys:
                removed_within_run += 1
                continue
            if identity is not None:
                run_keys.add(identity)
            ready_jobs.append(prepared_job)

    created_files: list[Path] = []
    if ready_jobs:
        day_dir.mkdir(parents=True, exist_ok=True)
        timestamp = created_at.isoformat(timespec="seconds")
        for offset in range(0, len(ready_jobs), BATCH_SIZE):
            largest_batch_number += 1
            if largest_batch_number > 999:
                raise RuntimeError(f"No three-digit batch numbers remain in {day_dir}")
            path = day_dir / f"{largest_batch_number:03d}.json"
            batch_jobs = ready_jobs[offset:offset + BATCH_SIZE]
            payload = {
                "created_at": timestamp,
                "job_count": len(batch_jobs),
                "finalized": False,
                "jobs": batch_jobs,
            }
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            created_files.append(path)

    summary = {
        "indeed_read": source_counts.get("Indeed", 0),
        "linkedin_read": source_counts.get("LinkedIn", 0),
        "removed_by_state": removed_by_state,
        "removed_by_batches": removed_by_batches,
        "removed_within_run": removed_within_run,
        "skipped_missing_identity": skipped_missing_identity,
        "ready": len(ready_jobs),
        "created_files": created_files,
    }
    return summary


def _print_summary(summary: dict[str, Any], root: Path = REPO_ROOT) -> None:
    print(f"Indeed jobs read: {summary['indeed_read']}")
    print(f"LinkedIn jobs read: {summary['linkedin_read']}")
    print(f"Removed by state: {summary['removed_by_state']}")
    print(f"Removed by existing daily batches: {summary['removed_by_batches']}")
    print(f"Removed as duplicates within this run: {summary['removed_within_run']}")
    print(f"Skipped without job ID or source URL: {summary['skipped_missing_identity']}")
    print(f"Jobs ready for batching: {summary['ready']}")
    print(f"Batches created: {len(summary['created_files'])}")
    if summary["created_files"]:
        names = [str(path.relative_to(root)) for path in summary["created_files"]]
        print(f"Batch files: {', '.join(names)}")
    else:
        print("No new jobs available for batching.")


def main() -> int:
    summary = prepare_batches()
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
