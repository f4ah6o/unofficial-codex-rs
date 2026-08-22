#!/usr/bin/env python3
"""Delete the accidental unofficial-codex-* crates.io publication.

The crates.io crate-deletion endpoint intentionally accepts website session
cookies, not Cargo API tokens. The default mode is a read-only plan. Actual
deletion requires both --execute and an explicit confirmation phrase.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = ROOT / "scripts" / "publish-workspace.py"

API_BASE = "https://crates.io"
USER_AGENT = (
    "unofficial-codex-crate-cleanup/1.0 "
    "(https://github.com/f4ah6o/unofficial-codex-rs)"
)
CRATE_PREFIX = "unofficial-codex-"
EXPECTED_CRATE_COUNT = 119
EXPECTED_VERSION = "2026.8.0"
EXPECTED_REPOSITORY = "https://github.com/f4ah6o/unofficial-codex-rs"
MAX_CRATE_AGE = timedelta(hours=72)
REQUEST_INTERVAL_SECONDS = 1.1
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CONFIRMATION = "DELETE_UNOFFICIAL_CODEX_CRATES"
SESSION_ENV = "CRATES_IO_CARGO_SESSION"
DEFAULT_MESSAGE = (
    "Accidental bulk publication of an unofficial downstream workspace; "
    "publication abandoned."
)

class CleanupError(RuntimeError):
    """A safe, user-facing cleanup failure."""


class ReverseDependenciesRemain(CleanupError):
    """Deletion is temporarily blocked by another target crate."""


@dataclass(frozen=True)
class ApiResult:
    status: int
    payload: bytes


class RequestLimiter:
    """Keep crates.io API traffic below the documented one-request/second cap."""

    def __init__(self, interval_seconds: float = REQUEST_INTERVAL_SECONDS) -> None:
        self.interval_seconds = interval_seconds
        self.last_request_started: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_request_started is not None:
            remaining = self.interval_seconds - (now - self.last_request_started)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_started = time.monotonic()


class CratesIoClient:
    def __init__(
        self,
        *,
        session_cookie: str | None = None,
        limiter: RequestLimiter | None = None,
    ) -> None:
        self.session_cookie = session_cookie
        self.limiter = limiter or RequestLimiter()

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticate: bool = False,
        accepted_statuses: frozenset[int] = frozenset({200}),
        attempts: int = 5,
    ) -> ApiResult:
        url = f"{API_BASE}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if authenticate:
            if self.session_cookie is None:
                raise CleanupError("crates.io website session cookie is required")
            headers.update(
                {
                    "Cookie": f"cargo_session={self.session_cookie}",
                    "Origin": API_BASE,
                    "Referer": f"{API_BASE}/",
                }
            )

        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            request = urllib.request.Request(url, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise CleanupError(
                            f"{method} {path} exceeded the response size limit"
                        )
                    result = ApiResult(response.status, payload)
                    if result.status not in accepted_statuses:
                        raise CleanupError(
                            f"{method} {path} returned unexpected HTTP {result.status}"
                        )
                    return result
            except urllib.error.HTTPError as error:
                payload = error.read(65_537)
                if error.code in accepted_statuses:
                    return ApiResult(error.code, payload)
                if error.code == 429 or 500 <= error.code <= 504:
                    if attempt < attempts:
                        retry_after = _retry_after_seconds(error)
                        time.sleep(max(retry_after, float(attempt)))
                        continue
                detail = _api_error_detail(payload)
                raise CleanupError(
                    f"{method} {path} failed with HTTP {error.code}: {detail}"
                ) from error
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt < attempts:
                    time.sleep(float(attempt))
                    continue
                raise CleanupError(f"{method} {path} failed: {error}") from error

        raise AssertionError("request retry loop exhausted unexpectedly")

    def get_json(self, path: str) -> dict[str, Any]:
        result = self.request("GET", path)
        try:
            value = json.loads(result.payload)
        except json.JSONDecodeError as error:
            raise CleanupError(f"GET {path} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise CleanupError(f"GET {path} returned a non-object JSON response")
        return value

    def delete_crate(self, name: str, message: str) -> str:
        encoded_name = urllib.parse.quote(name, safe="")
        query = urllib.parse.urlencode({"message": message})
        path = f"/api/v1/crates/{encoded_name}?{query}"
        result = self.request(
            "DELETE",
            path,
            authenticate=True,
            accepted_statuses=frozenset({204, 404, 422}),
        )
        if result.status == 422:
            detail = _api_error_detail(result.payload)
            if "only crates without reverse dependencies can be deleted" in detail:
                raise ReverseDependenciesRemain(detail)
            raise CleanupError(f"DELETE {path} failed with HTTP 422: {detail}")
        return "deleted" if result.status == 204 else "already absent"


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    value = error.headers.get("Retry-After")
    if value is None:
        return 0.0
    try:
        return max(float(value), 0.0)
    except ValueError:
        return 0.0


def _api_error_detail(payload: bytes) -> str:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = payload.decode("utf-8", errors="replace").strip()
        return text[:500] or "empty response"

    if isinstance(value, dict):
        errors = value.get("errors")
        if isinstance(errors, list):
            details = [
                item.get("detail")
                for item in errors
                if isinstance(item, dict) and isinstance(item.get("detail"), str)
            ]
            if details:
                return "; ".join(details)
    return json.dumps(value, ensure_ascii=False)[:500]


def is_publish_process(command_name: str, command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    if not argv:
        return False

    executable = Path(command_name).name.lower()
    if executable.startswith("python"):
        script_args = argv[1:]
        return any(
            argument == "/tmp/unofficial-codex-publish-loop.py"
            or argument == "scripts/publish-workspace.py"
            or argument.endswith("/scripts/publish-workspace.py")
            for argument in script_args
        )
    return executable == "cargo" and len(argv) > 1 and argv[1] == "publish"


def active_publish_processes() -> list[str]:
    result = subprocess.run(
        ["ps", "ax", "-o", "pid=,comm=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    current_pid = str(os.getpid())
    matches = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3 or fields[0] == current_pid:
            continue
        if is_publish_process(fields[1], fields[2]):
            matches.append(line.strip())
    return matches


def assert_publish_stopped() -> None:
    matches = active_publish_processes()
    if matches:
        rendered = "\n".join(f"  {line}" for line in matches)
        raise CleanupError(f"publish processes are still active:\n{rendered}")


def load_publish_plan() -> list[str]:
    result = subprocess.run(
        [sys.executable, str(PUBLISH_SCRIPT), "--plan"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(names) != EXPECTED_CRATE_COUNT:
        raise CleanupError(
            f"expected {EXPECTED_CRATE_COUNT} workspace crates, found {len(names)}"
        )
    if len(names) != len(set(names)):
        raise CleanupError("publish plan contains duplicate crate names")
    invalid = [name for name in names if not name.startswith(CRATE_PREFIX)]
    if invalid:
        raise CleanupError(f"publish plan contains unexpected crate names: {invalid}")
    return names


def discover_published_crates(client: CratesIoClient) -> dict[str, dict[str, Any]]:
    crates: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "q": CRATE_PREFIX,
                "page": page,
                "per_page": 100,
            }
        )
        data = client.get_json(f"/api/v1/crates?{query}")
        rows = data.get("crates")
        if not isinstance(rows, list):
            raise CleanupError("crates.io search response has no crates list")
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("id")
            if isinstance(name, str) and name.startswith(CRATE_PREFIX):
                crates[name] = row

        meta = data.get("meta")
        total = meta.get("total") if isinstance(meta, dict) else None
        if not rows or (isinstance(total, int) and page * 100 >= total):
            break
        page += 1
        if page > 10:
            raise CleanupError("crates.io search exceeded 10 pages")
    return crates


def parse_crates_io_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CleanupError("crate metadata has no valid created_at timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CleanupError(f"invalid crates.io timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise CleanupError(f"crates.io timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def validate_crate_metadata(
    name: str,
    metadata: dict[str, Any],
    *,
    now: datetime,
) -> None:
    errors = []
    if metadata.get("id") != name or metadata.get("name") != name:
        errors.append("name/id mismatch")
    if metadata.get("repository") != EXPECTED_REPOSITORY:
        errors.append(f"repository is {metadata.get('repository')!r}")
    if metadata.get("num_versions") != 1:
        errors.append(f"num_versions is {metadata.get('num_versions')!r}")
    for field in ("default_version", "max_version", "newest_version"):
        if metadata.get(field) != EXPECTED_VERSION:
            errors.append(f"{field} is {metadata.get(field)!r}")

    created_at = parse_crates_io_timestamp(metadata.get("created_at"))
    age = now - created_at
    if age < timedelta(minutes=-5):
        errors.append(f"created_at is unexpectedly in the future: {created_at.isoformat()}")
    if age > MAX_CRATE_AGE:
        errors.append(f"crate age exceeds 72 hours: {age}")

    if errors:
        raise CleanupError(f"refusing to delete {name}: " + "; ".join(errors))


def deletion_plan(
    publish_plan: list[str],
    published: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> list[str]:
    plan_names = set(publish_plan)
    unexpected = sorted(set(published) - plan_names)
    if unexpected:
        raise CleanupError(
            "crates.io search returned unexpected prefixed crates: "
            + ", ".join(unexpected)
        )

    for name, metadata in published.items():
        validate_crate_metadata(name, metadata, now=now)

    # publish_plan is dependency-first, so reverse it to delete dependents first.
    return [name for name in reversed(publish_plan) if name in published]


def delete_in_dependency_passes(
    client: CratesIoClient,
    targets: list[str],
    message: str,
) -> int:
    """Delete each target once per pass, retrying reverse-dependency blocks."""

    pending = list(targets)
    total = len(pending)
    completed = 0
    pass_number = 1

    while pending:
        print(
            f"pass {pass_number}: attempting {len(pending)} crate(s)",
            flush=True,
        )
        blocked: list[tuple[str, str]] = []
        completed_before_pass = completed

        for name in pending:
            try:
                outcome = client.delete_crate(name, message)
            except ReverseDependenciesRemain as error:
                blocked.append((name, str(error)))
                print(f"deferred: {name}: {error}", flush=True)
                continue

            completed += 1
            print(f"{completed:>3}/{total} {outcome}: {name}", flush=True)

        if not blocked:
            return completed

        if completed == completed_before_pass:
            details = "\n".join(
                f"  {name}: {detail}" for name, detail in blocked
            )
            raise CleanupError(
                "no deletion progress; unresolved reverse dependencies remain:\n"
                + details
            )

        print(
            f"pass {pass_number}: deferred {len(blocked)} crate(s) "
            "until the next pass",
            flush=True,
        )
        pending = [name for name, _ in blocked]
        pass_number += 1

    return completed


def read_session_cookie() -> str:
    value = os.environ.pop(SESSION_ENV, None)
    if value is None:
        raise CleanupError(
            f"{SESSION_ENV} is required for --execute; use the cargo_session "
            "value from an authenticated crates.io website session"
        )
    if not value or value != value.strip():
        raise CleanupError(f"{SESSION_ENV} must be a non-empty cookie value")
    if any(character in value for character in (";", "\r", "\n")):
        raise CleanupError(
            f"{SESSION_ENV} must contain only the cargo_session value, "
            "not a complete Cookie header"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute deletion of the accidental unofficial-codex-* "
            "crates.io publication"
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform irreversible crate deletion (default: read-only plan)",
    )
    parser.add_argument(
        "--confirm",
        metavar="PHRASE",
        help=f"required with --execute; exact value: {CONFIRMATION}",
    )
    parser.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help="deletion message recorded by crates.io",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        assert_publish_stopped()
        publish_plan = load_publish_plan()
        limiter = RequestLimiter()
        discovery_client = CratesIoClient(limiter=limiter)
        published = discover_published_crates(discovery_client)
        targets = deletion_plan(
            publish_plan,
            published,
            now=datetime.now(timezone.utc),
        )

        mode = "DELETE" if args.execute else "dry-run"
        print(
            f"{mode}: {len(targets)} published crates in reverse dependency order",
            flush=True,
        )
        for index, name in enumerate(targets, start=1):
            print(f"{index:>3}/{len(targets)} {name}", flush=True)

        if not args.execute:
            print(
                f"To execute, set {SESSION_ENV} and pass "
                f"--execute --confirm {CONFIRMATION}",
                flush=True,
            )
            return 0

        if args.confirm != CONFIRMATION:
            raise CleanupError(
                f"--confirm must be exactly {CONFIRMATION} when using --execute"
            )
        if not args.message or len(args.message) > 500:
            raise CleanupError("--message must contain between 1 and 500 characters")

        cookie = read_session_cookie()
        delete_client = CratesIoClient(
            session_cookie=cookie,
            limiter=limiter,
        )
        completed = delete_in_dependency_passes(
            delete_client,
            targets,
            args.message,
        )

        remaining = discover_published_crates(CratesIoClient(limiter=limiter))
        remaining_targets = sorted(set(remaining) & set(publish_plan))
        if remaining_targets:
            raise CleanupError(
                "deletion pass completed but crates.io still lists: "
                + ", ".join(remaining_targets)
            )

        print(f"completed: removed {completed} crate(s)", flush=True)
        return 0
    except (CleanupError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
