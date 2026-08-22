from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).with_name("delete-published-crates.py")
SPEC = importlib.util.spec_from_file_location("delete_published_crates", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def crate_metadata(
    name: str,
    *,
    created_at: datetime | None = None,
    repository: str = MODULE.EXPECTED_REPOSITORY,
    version: str = MODULE.EXPECTED_VERSION,
    num_versions: int = 1,
) -> dict:
    created_at = created_at or datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "id": name,
        "name": name,
        "repository": repository,
        "num_versions": num_versions,
        "default_version": version,
        "max_version": version,
        "newest_version": version,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }


class DeletionPlanTest(unittest.TestCase):
    def test_reverses_publish_order_and_filters_unpublished_crates(self) -> None:
        now = datetime.now(timezone.utc)
        publish_plan = [
            "unofficial-codex-base",
            "unofficial-codex-middle",
            "unofficial-codex-leaf",
        ]
        published = {
            "unofficial-codex-base": crate_metadata("unofficial-codex-base"),
            "unofficial-codex-leaf": crate_metadata("unofficial-codex-leaf"),
        }

        actual = MODULE.deletion_plan(publish_plan, published, now=now)

        self.assertEqual(
            actual,
            [
                "unofficial-codex-leaf",
                "unofficial-codex-base",
            ],
        )

    def test_rejects_unexpected_prefixed_crate(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(MODULE.CleanupError, "unexpected prefixed"):
            MODULE.deletion_plan(
                ["unofficial-codex-base"],
                {
                    "unofficial-codex-other": crate_metadata(
                        "unofficial-codex-other"
                    )
                },
                now=now,
            )


class MetadataValidationTest(unittest.TestCase):
    def test_accepts_expected_recent_single_version_crate(self) -> None:
        now = datetime.now(timezone.utc)
        name = "unofficial-codex-core"

        MODULE.validate_crate_metadata(
            name,
            crate_metadata(name, created_at=now - timedelta(hours=1)),
            now=now,
        )

    def test_rejects_wrong_repository(self) -> None:
        now = datetime.now(timezone.utc)
        name = "unofficial-codex-core"
        with self.assertRaisesRegex(MODULE.CleanupError, "repository"):
            MODULE.validate_crate_metadata(
                name,
                crate_metadata(name, repository="https://example.com/wrong"),
                now=now,
            )

    def test_rejects_multiple_versions(self) -> None:
        now = datetime.now(timezone.utc)
        name = "unofficial-codex-core"
        with self.assertRaisesRegex(MODULE.CleanupError, "num_versions"):
            MODULE.validate_crate_metadata(
                name,
                crate_metadata(name, num_versions=2),
                now=now,
            )

    def test_rejects_crate_older_than_72_hours(self) -> None:
        now = datetime.now(timezone.utc)
        name = "unofficial-codex-core"
        with self.assertRaisesRegex(MODULE.CleanupError, "exceeds 72 hours"):
            MODULE.validate_crate_metadata(
                name,
                crate_metadata(name, created_at=now - timedelta(hours=73)),
                now=now,
            )


class PublishProcessDetectionTest(unittest.TestCase):
    def test_detects_actual_cargo_publish(self) -> None:
        self.assertTrue(
            MODULE.is_publish_process(
                "cargo",
                "/Users/example/.cargo/bin/cargo publish --package example",
            )
        )

    def test_ignores_prompt_text_containing_cargo_publish(self) -> None:
        self.assertFalse(
            MODULE.is_publish_process(
                "codex",
                "codex exec investigate the cargo publish process",
            )
        )

    def test_detects_publish_loop_python_process(self) -> None:
        self.assertTrue(
            MODULE.is_publish_process(
                "Python",
                "python3 /tmp/unofficial-codex-publish-loop.py",
            )
        )


class ErrorDetailTest(unittest.TestCase):
    def test_extracts_crates_io_error_details(self) -> None:
        payload = b'{"errors":[{"detail":"reverse dependency exists"}]}'

        actual = MODULE._api_error_detail(payload)

        self.assertEqual(actual, "reverse dependency exists")


if __name__ == "__main__":
    unittest.main()
