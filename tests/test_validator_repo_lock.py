"""Tests for the shared Validator repository lock."""

import validator.repo_lock as repo_lock


def test_acquire_success(monkeypatch):
    monkeypatch.setattr(repo_lock, "_request", lambda *args, **kwargs: (201, {}))
    assert repo_lock.acquire(
        repo="o/r", branch="main", api_token="api", owner="apply", lock_token="t1"
    ) is True


def test_acquire_existing_lock_is_fail_fast(monkeypatch):
    monkeypatch.setattr(
        repo_lock,
        "_request",
        lambda *args, **kwargs: (422, {"message": 'Invalid request. "sha" was not supplied.'}),
    )
    assert repo_lock.acquire(
        repo="o/r", branch="main", api_token="api", owner="apply", lock_token="t2"
    ) is False


def test_release_refuses_foreign_lock(monkeypatch):
    monkeypatch.setattr(
        repo_lock,
        "read_lock",
        lambda **kwargs: ("abc", {"schema_version": 1, "owner": "validator", "token": "other"}),
    )
    calls = []
    monkeypatch.setattr(repo_lock, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    try:
        repo_lock.release(
            repo="o/r", branch="main", api_token="api", owner="apply", lock_token="mine"
        )
    except repo_lock.LockError as exc:
        assert "ownership changed" in str(exc)
    else:
        raise AssertionError("release should reject a foreign lock")
    assert calls == []


def test_release_deletes_exact_current_sha(monkeypatch):
    monkeypatch.setattr(
        repo_lock,
        "read_lock",
        lambda **kwargs: ("sha-123", {"schema_version": 1, "owner": "apply", "token": "mine"}),
    )
    captured = {}

    def fake_request(method, url, token, payload=None):
        captured.update(method=method, url=url, token=token, payload=payload)
        return 200, {}

    monkeypatch.setattr(repo_lock, "_request", fake_request)
    repo_lock.release(
        repo="o/r", branch="main", api_token="api", owner="apply", lock_token="mine"
    )
    assert captured["method"] == "DELETE"
    assert captured["payload"]["sha"] == "sha-123"
