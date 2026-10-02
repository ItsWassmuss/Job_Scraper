"""Tests for deterministic Validator open-batch selection."""

import json
from pathlib import Path

import pytest

from validator.select_batch import (
    load_open_batch_ids,
    reserved_batch_ids,
    select_open_batch,
)


def _write_index(root: Path, batch_ids):
    path = root / "validator/open_batches.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "batches": batch_ids}),
        encoding="utf-8",
    )
    return path


def _write_batch(root: Path, batch_id: str, statuses, *, finalized=False):
    day, number = batch_id.split("-")
    path = root / "validator/batches" / day / f"{number}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    jobs = [{"status": status} for status in statuses]
    path.write_text(
        json.dumps({
            "created_at": "2026-10-01T00:00:00+03:00",
            "job_count": len(jobs),
            "finalized": finalized,
            "jobs": jobs,
        }),
        encoding="utf-8",
    )
    return path


def _write_patch(root: Path, name: str):
    path = root / "validator/patches" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_selects_newest_pending_batch(tmp_path):
    _write_index(tmp_path, ["20261001-013", "20260930-013"])
    _write_batch(tmp_path, "20261001-013", [None, "REJECTED"])
    _write_batch(tmp_path, "20260930-013", [None])

    selection = select_open_batch(tmp_path)

    assert selection["batch_id"] == "20261001-013"
    assert selection["batch_path"] == "validator/batches/20261001/013.json"
    assert selection["pending_indexes"] == [0]
    assert selection["reserved_batches"] == []
    assert selection["stale_skipped"] == []


def test_reserved_newest_is_skipped_without_reading_batch(tmp_path):
    _write_index(tmp_path, ["20261001-013", "20260930-013"])
    _write_patch(tmp_path, "20261001-013-000.json")
    _write_batch(tmp_path, "20260930-013", [None])

    selection = select_open_batch(tmp_path)

    assert selection["batch_id"] == "20260930-013"
    assert selection["reserved_batches"] == ["20261001-013"]


def test_stale_entries_are_skipped_within_index(tmp_path):
    _write_index(
        tmp_path,
        [
            "20261002-002",
            "20261002-001",
            "20261001-016",
            "20261001-015",
            "20261001-014",
            "20261001-013",
        ],
    )
    _write_batch(tmp_path, "20261002-002", ["REJECTED"], finalized=True)
    _write_batch(tmp_path, "20261002-001", ["QUALIFIED"], finalized=True)
    _write_batch(tmp_path, "20261001-016", ["UNVALIDATED"], finalized=True)
    _write_batch(tmp_path, "20261001-015", ["REJECTED"], finalized=False)
    _write_batch(tmp_path, "20261001-014", ["QUALIFIED"], finalized=False)
    _write_batch(tmp_path, "20261001-013", [None] * 57, finalized=False)

    selection = select_open_batch(tmp_path)

    assert selection["batch_id"] == "20261001-013"
    assert selection["stale_skipped"] == [
        "20261002-002",
        "20261002-001",
        "20261001-016",
        "20261001-015",
        "20261001-014",
    ]
    assert len(selection["pending_indexes"]) == 57


@pytest.mark.parametrize(
    "batch_ids",
    [
        ["20261001-013", "20261001-013"],
        ["20260930-013", "20261001-013"],
        ["20261001-13"],
        ["validator/batches/20261001/013.json"],
    ],
)
def test_invalid_index_contract_is_rejected(tmp_path, batch_ids):
    _write_index(tmp_path, batch_ids)

    with pytest.raises(ValueError):
        load_open_batch_ids(tmp_path)


def test_missing_unreserved_batch_is_fatal(tmp_path):
    _write_index(tmp_path, ["20261001-013"])

    with pytest.raises(FileNotFoundError):
        select_open_batch(tmp_path)


def test_invalid_patch_json_filename_is_fatal(tmp_path):
    _write_patch(tmp_path, "bad.json")

    with pytest.raises(ValueError):
        reserved_batch_ids(tmp_path)


def test_no_eligible_unreserved_batch_returns_none(tmp_path):
    _write_index(tmp_path, ["20261001-013"])
    _write_patch(tmp_path, "20261001-013-000.json")

    assert select_open_batch(tmp_path) is None
