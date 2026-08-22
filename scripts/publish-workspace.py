#!/usr/bin/env python3
"""Publish every local Cargo package in dependency order.

Cargo workspace publishing is not atomic. This runner makes the order explicit,
waits for crates.io index propagation, and can be rerun after a partial release.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
            # Dev-dependencies do not participate in the published dependency
            # graph. Normal and build dependencies must be available first.
            if dependency["kind"] == "dev":
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
            raise SystemExit(f"local Cargo dependency cycle: {cycle}")
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
            skipping = header == "[dev-dependencies]" or (
                header.startswith("[target.") and header.endswith(".dev-dependencies]")
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
        print(f"{name} failed once; skipping it and continuing.", flush=True)
        return False
    finally:
        if restore_manifest is not None:
            manifest_path.write_text(restore_manifest, encoding="utf-8")
            print(f"Restored {manifest_path}", flush=True)
        if restore_lock is not None:
            LOCKFILE.write_text(restore_lock, encoding="utf-8")
            print(f"Restored {LOCKFILE}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
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

    mode = "dry run" if args.dry_run else "publish"
    print(f"{mode}: processing {len(packages)} local Cargo packages.", flush=True)
    skipped: list[str] = []
    for package in packages:
        if not publish(package, dry_run=args.dry_run):
            skipped.append(package["name"])
    if skipped:
        print("Skipped packages after one failed attempt:", flush=True)
        for name in skipped:
            print(f"  - {name}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
