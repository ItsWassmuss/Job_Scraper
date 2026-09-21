"""Shared fail-fast repository mutex for Validator and Apply."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LOCK_PATH = "validator/runtime.lock"
SCHEMA_VERSION = 1
API_VERSION = "2022-11-28"


class LockError(RuntimeError):
    pass


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = _json_bytes(payload) if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "job-scraper-validator-lock",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        return exc.code, body
    except URLError as exc:
        raise LockError(f"GitHub lock request failed: {exc}") from exc


def _contents_url(repo: str, branch: str) -> str:
    path = quote(LOCK_PATH, safe="/")
    return f"https://api.github.com/repos/{repo}/contents/{path}?ref={quote(branch, safe='')}"


def _write_url(repo: str) -> str:
    path = quote(LOCK_PATH, safe="/")
    return f"https://api.github.com/repos/{repo}/contents/{path}"


def _busy_response(status: int, body: Any) -> bool:
    if status == 409:
        return True
    if status != 422:
        return False
    text = json.dumps(body, ensure_ascii=False).lower()
    return "sha" in text or "already exists" in text or "already_exists" in text


def acquire(
    *,
    repo: str,
    branch: str,
    api_token: str,
    owner: str,
    lock_token: str,
) -> bool:
    lock_payload = {
        "schema_version": SCHEMA_VERSION,
        "owner": owner,
        "token": lock_token,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    content = base64.b64encode(_json_bytes(lock_payload) + b"\n").decode("ascii")
    status, body = _request(
        "PUT",
        _write_url(repo),
        api_token,
        {
            "message": f"chore: acquire validator runtime lock [{owner}]",
            "content": content,
            "branch": branch,
        },
    )
    if status in {200, 201}:
        return True
    if _busy_response(status, body):
        return False
    raise LockError(f"GitHub lock acquire failed with HTTP {status}: {body!r}")


def read_lock(*, repo: str, branch: str, api_token: str) -> tuple[str, dict[str, Any]] | None:
    status, body = _request("GET", _contents_url(repo, branch), api_token)
    if status == 404:
        return None
    if status != 200 or not isinstance(body, dict):
        raise LockError(f"GitHub lock read failed with HTTP {status}: {body!r}")
    sha = body.get("sha")
    encoded = body.get("content")
    if not isinstance(sha, str) or not isinstance(encoded, str):
        raise LockError("GitHub lock response is missing sha/content")
    try:
        payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockError("GitHub runtime lock contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise LockError("GitHub runtime lock payload must be an object")
    return sha, payload


def release(
    *,
    repo: str,
    branch: str,
    api_token: str,
    owner: str,
    lock_token: str,
) -> None:
    current = read_lock(repo=repo, branch=branch, api_token=api_token)
    if current is None:
        raise LockError("Runtime lock disappeared before release")
    sha, payload = current
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LockError("Runtime lock has unsupported schema_version")
    if payload.get("owner") != owner or payload.get("token") != lock_token:
        raise LockError("Runtime lock ownership changed; refusing to delete it")
    status, body = _request(
        "DELETE",
        _write_url(repo),
        api_token,
        {
            "message": f"chore: release validator runtime lock [{owner}]",
            "sha": sha,
            "branch": branch,
        },
    )
    if status not in {200, 204}:
        raise LockError(f"GitHub lock release failed with HTTP {status}: {body!r}")


def _required(value: str | None, name: str) -> str:
    if value:
        return value
    raise LockError(f"{name} is required")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("acquire", "release"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--token", dest="lock_token", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME") or "main")
    args = parser.parse_args()

    try:
        repo = _required(args.repo, "repository")
        api_token = _required(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"), "GH_TOKEN")
        if args.action == "acquire":
            acquired = acquire(
                repo=repo,
                branch=args.branch,
                api_token=api_token,
                owner=args.owner,
                lock_token=args.lock_token,
            )
            print(f"acquired={'true' if acquired else 'false'}")
            return 0 if acquired else 10
        release(
            repo=repo,
            branch=args.branch,
            api_token=api_token,
            owner=args.owner,
            lock_token=args.lock_token,
        )
        print("released=true")
        return 0
    except LockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
