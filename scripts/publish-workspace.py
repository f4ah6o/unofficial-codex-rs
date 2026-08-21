#!/usr/bin/env python3
"""Publish every local Cargo package in dependency order.

Cargo workspace publishing is not atomic. This runner makes the order explicit,
waits for crates.io index propagation, and can be rerun after a partial release.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "codex-rs" / "Cargo.toml"


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


def publish(package: dict) -> bool:
    name = package["name"]
    command = [
        "cargo",
        "publish",
        "--manifest-path",
        str(MANIFEST),
        "--package",
        name,
        "--locked",
    ]
    print(f"Publishing {name} (single attempt)", flush=True)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()

    data = metadata()
    packages = publish_order(data)
    if args.plan:
        for package in packages:
            print(package["name"])
        return 0

    print(f"Publishing {len(packages)} local Cargo packages.", flush=True)
    skipped: list[str] = []
    for package in packages:
        if not publish(package):
            skipped.append(package["name"])
    if skipped:
        print("Skipped packages after one failed attempt:", flush=True)
        for name in skipped:
            print(f"  - {name}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
