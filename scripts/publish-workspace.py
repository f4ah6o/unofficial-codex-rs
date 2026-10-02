#!/usr/bin/env python3
"""Publish every local Cargo package in dependency order.

Cargo workspace publishing is not atomic. This runner makes the order explicit,
waits for crates.io index propagation, and can be rerun after a partial release.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import re
import subprocess
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "codex-rs" / "Cargo.toml"
LOCKFILE = ROOT / "codex-rs" / "Cargo.lock"

# These packages participate in test-only dependency cycles. Cargo still
# resolves dev-dependencies while preparing a publish, so publish these
# bootstrap packages with their dev-dependency sections omitted from the
# uploaded manifest. The workspace files are restored immediately afterwards.
PUBLISH_WITHOUT_DEV_DEPENDENCIES = {
    "unofficial-codex-exec-server",
    "unofficial-codex-linux-sandbox",
    "unofficial-codex-login",
    "unofficial-codex-core",
    "unofficial-codex-mcp-server",
    "unofficial-codex-tui",
}


def metadata() -> dict:
    output = subprocess.check_output(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(MANIFEST),
            "--locked",
            "--no-deps",
            "--format-version",
            "1",
        ],
        text=True,
    )
    return json.loads(output)


def publish_order(data: dict) -> list[dict]:
    packages = {
        package["id"]: package
        for package in data["packages"]
        if package["source"] is None
    }
    by_name = {package["name"]: package["id"] for package in packages.values()}
    dependencies: dict[str, set[str]] = {package_id: set() for package_id in packages}

    for package_id, package in packages.items():
        for dependency in package["dependencies"]:
            # Cargo resolves retained versioned dev-dependencies during
            # packaging too. Omit only the explicitly stripped bootstrap edges.
            if (dependency["kind"] == "dev"
                    and package["name"] in PUBLISH_WITHOUT_DEV_DEPENDENCIES):
                continue
            dependency_id = by_name.get(dependency["name"])
            if dependency_id and dependency_id != package_id:
                dependencies[package_id].add(dependency_id)

    ordered: list[str] = []
    pending = set(dependencies)
    while pending:
        ready = sorted(
            package_id
            for package_id in pending
            if not (dependencies[package_id] & pending)
        )
        if not ready:
            cycle = sorted(packages[package_id]["name"] for package_id in pending)
            raise SystemExit(
                f"local Cargo dependency cycle: {cycle}; review the explicit "
                "PUBLISH_WITHOUT_DEV_DEPENDENCIES bootstrap policy"
            )
        ordered.extend(ready)
        pending.difference_update(ready)

    return [packages[package_id] for package_id in ordered]


def without_dev_dependencies(manifest_path: Path) -> str:
    original = manifest_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    stripped: list[str] = []
    skipping = False

    for line in lines:
        header = line.strip()
        if header.startswith("[") and header.endswith("]"):
            skipping = header.startswith("[dev-dependencies") or (
                header.startswith("[target.") and ".dev-dependencies" in header
            )
        if not skipping:
            stripped.append(line)

    manifest_path.write_text("".join(stripped), encoding="utf-8")
    return original


def publish(package: dict, *, dry_run: bool) -> bool:
    name = package["name"]
    strip_dev_dependencies = name in PUBLISH_WITHOUT_DEV_DEPENDENCIES
    manifest_path = Path(package["manifest_path"])
    restore_manifest = None
    restore_lock = None
    restore_root = None

    if strip_dev_dependencies:
        print(
            f"Publishing {name} with dev-dependencies temporarily omitted "
            "for registry bootstrap.",
            flush=True,
        )
        restore_manifest = without_dev_dependencies(manifest_path)
        if LOCKFILE.exists():
            restore_lock = LOCKFILE.read_text(encoding="utf-8")

    command = [
        "cargo",
        "publish",
        "--manifest-path",
        str(MANIFEST),
        "--package",
        name,
    ]
    if dry_run:
        command.append("--dry-run")
    if strip_dev_dependencies:
        # Removing dev-dependencies changes the workspace lock resolution.
        # Let Cargo update it during the temporary publish, then restore it.
        command.append("--allow-dirty")
    else:
        command.append("--locked")

    try:
        if name == "unofficial-codex-tui":
            restore_root = MANIFEST.read_bytes()
            MANIFEST.write_text(registry_tui_dependencies(restore_root.decode()))
        mode = "dry run" if dry_run else "single attempt"
        print(f"Publishing {name} ({mode})", flush=True)
        result = subprocess.run(command, text=True, capture_output=True)
        combined = (result.stdout + "\n" + result.stderr).strip()
        print(combined, flush=True)
        if result.returncode == 0:
            return True
        if "already exists" in combined:
            print(f"{name} already exists; continuing for rerun safety.", flush=True)
            return True
        print(f"{name} failed this attempt; deferring to a later pass.", flush=True)
        return False
    finally:
        if restore_root is not None:
            MANIFEST.write_bytes(restore_root)
        if restore_manifest is not None:
            manifest_path.write_text(restore_manifest, encoding="utf-8")
            print(f"Restored {manifest_path}", flush=True)
        if restore_lock is not None:
            LOCKFILE.write_text(restore_lock, encoding="utf-8")
            print(f"Restored {LOCKFILE}", flush=True)


def publish_in_passes(
    packages: list[dict], *, dry_run: bool, max_passes: int, retry_delay: float,
) -> list[str]:
    """Retry incomplete packages without republishing successful packages."""
    pending = packages
    for attempt in range(max_passes):
        pending = [package for package in pending if not publish(package, dry_run=dry_run)]
        if not pending:
            return []
        if attempt + 1 < max_passes:
            print(f"Retrying {len(pending)} packages after index propagation.", flush=True)
            time.sleep(retry_delay)
    return [package["name"] for package in pending]


def registry_tui_dependencies(text: str) -> str:
    # The published ratatui 0.29.0 pins unicode-width to 0.2.0, whereas the
    # workspace's Git fork permits 0.2.1. Publish the compatible requirement
    # after dropping TUI's test-only vt100 dependency (which requires 0.2.1).
    updated, count = re.subn(r'(?m)^unicode-width\s*=\s*"0\.2"\s*$',
                            'unicode-width = "=0.2.0"', text)
    if count != 1:
        raise ValueError("unexpected unicode-width requirement; review registry compatibility")
    return updated


def verify_packages(packages: list[dict]) -> int:
    """Verify a release together so unpublished local dependencies are staged.

    Per-crate dry runs cannot bootstrap a fresh version: dependencies are never
    uploaded. Cargo's multi-package publishing stages artifacts in a temporary
    local registry and verifies their normalized manifests against each other.
    """
    if not packages:
        return 0
    command = ["cargo", "publish", "--manifest-path", str(MANIFEST), "--dry-run"]
    with ExitStack() as restore:
        bootstrap = [p for p in packages if p["name"] in PUBLISH_WITHOUT_DEV_DEPENDENCIES]
        if bootstrap:
            # Some test-support crates are implicit workspace members only
            # through dev edges. Keep every selected package selectable after
            # removing those edges for bootstrap.
            original_root = MANIFEST.read_bytes()
            restore.callback(MANIFEST.write_bytes, original_root)
            root_text = original_root.decode()
            members = tomllib.loads(root_text)["workspace"]["members"]
            members = sorted(set(members) | {
                Path(p["manifest_path"]).parent.relative_to(MANIFEST.parent).as_posix()
                for p in packages
            })
            root_text, replacements = re.subn(
                r"(?ms)^(\[workspace\].*?^members\s*=\s*)\[.*?\]",
                lambda match: match.group(1) + json.dumps(members),
                root_text, count=1,
            )
            if replacements != 1:
                raise ValueError("missing workspace members table")
            if any(p["name"] == "unofficial-codex-tui" for p in bootstrap):
                root_text = registry_tui_dependencies(root_text)
            MANIFEST.write_text(root_text)
            if LOCKFILE.exists():
                restore.callback(LOCKFILE.write_bytes, LOCKFILE.read_bytes())
            else:
                restore.callback(LOCKFILE.unlink, missing_ok=True)
            for package in bootstrap:
                manifest = Path(package["manifest_path"])
                # Register restoration before attempting a mutation.
                restore.callback(manifest.write_bytes, manifest.read_bytes())
                without_dev_dependencies(manifest)
            command.append("--allow-dirty")
        else:
            command.append("--locked")
        for package in packages:
            command.extend(["--package", package["name"]])
        print(f"Verifying {len(packages)} packaged artifacts together.", flush=True)
        return subprocess.run(command).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--max-passes", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=30)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="package and verify every selected crate without uploading",
    )
    parser.add_argument(
        "--package",
        dest="selected_packages",
        action="append",
        metavar="NAME",
        help="select one or more packages while preserving dependency order",
    )
    args = parser.parse_args()
    if args.max_passes < 1 or args.retry_delay < 0:
        parser.error("--max-passes must be positive and --retry-delay nonnegative")

    data = metadata()
    packages = publish_order(data)
    if args.selected_packages:
        all_names = {package["name"] for package in packages}
        unknown = sorted(set(args.selected_packages) - all_names)
        if unknown:
            raise SystemExit(f"unknown local Cargo packages: {unknown}")
        selected = set(args.selected_packages)
        packages = [package for package in packages if package["name"] in selected]

    if args.plan:
        for package in packages:
            print(package["name"])
        return 0

    if args.dry_run:
        return verify_packages(packages)

    mode = "dry run" if args.dry_run else "publish"
    print(f"{mode}: processing {len(packages)} local Cargo packages.", flush=True)
    skipped = publish_in_passes(
        packages, dry_run=args.dry_run,
        max_passes=args.max_passes, retry_delay=args.retry_delay,
    )
    if skipped:
        print(f"Failed packages after {args.max_passes} passes:", flush=True)
        for name in skipped:
            print(f"  - {name}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
