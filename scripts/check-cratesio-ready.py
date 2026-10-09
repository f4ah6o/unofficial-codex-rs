#!/usr/bin/env python3
"""Cheap guardrails for the namespaced Cargo workspace."""

from __future__ import annotations

import re
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1] / "codex-rs"
PACKAGE_SECTION_RE = re.compile(r"(?ms)^\[package\](.*?)(?=^\[|\Z)")
NAME_RE = re.compile(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$')
PATH_DEP_RE = re.compile(
    r'^\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*\{(?P<body>[^{}\n]*\bpath\s*=\s*"[^"]+"[^{}\n]*)\}',
    re.MULTILINE,
)
DEPENDENCY_TABLE_RE = re.compile(
    r"(?ms)^\[(?:[^\]]+\.)?(?:dependencies|dev-dependencies|build-dependencies)\."
    r"(?P<key>[A-Za-z0-9_-]+)\](?P<body>.*?)(?=^\[|\Z)"
)


errors: list[str] = []
local_names: set[str] = set()

for manifest in sorted(WORKSPACE.rglob("Cargo.toml")):
    if "target" in manifest.parts or ".git" in manifest.parts:
        continue
    text = manifest.read_text(encoding="utf-8")
    package_match = PACKAGE_SECTION_RE.search(text)
    if not package_match:
        continue
    name_match = NAME_RE.search(package_match.group(0))
    if not name_match:
        continue
    name = name_match.group(1)
    local_names.add(name)
    if not name.startswith("unofficial-codex-"):
        errors.append(f"{manifest}: package is not namespaced: {name}")
    if re.search(r"(?m)^\s*publish\s*=\s*false\s*$", package_match.group(0)):
        errors.append(f"{manifest}: publish=false blocks the all-crates release")

root = (WORKSPACE / "Cargo.toml").read_text(encoding="utf-8")
root_version = re.search(r"(?ms)^\[workspace\.package\](.*?)(?=^\[|\Z)", root)
if not root_version or 'version = "0.0.0"' in root_version.group(0):
    errors.append("codex-rs/Cargo.toml: workspace version is still 0.0.0")

for manifest in sorted(WORKSPACE.rglob("Cargo.toml")):
    if "target" in manifest.parts or ".git" in manifest.parts:
        continue
    text = manifest.read_text(encoding="utf-8")
    for match in PATH_DEP_RE.finditer(text):
        key = match.group("key")
        body = match.group("body")
        if key in local_names or re.search(r'\bpackage\s*=\s*"unofficial-codex-', body):
            if not re.search(r'\bversion\s*=\s*"\d+\.\d+\.\d+"', body):
                errors.append(f"{manifest}: path dependency {key} has no registry version")
            if not re.search(r'\bpackage\s*=\s*"unofficial-codex-', body):
                errors.append(f"{manifest}: path dependency {key} has no package rename")


for manifest in sorted(WORKSPACE.rglob("Cargo.toml")):
    if "target" in manifest.parts or ".git" in manifest.parts:
        continue
    text = manifest.read_text(encoding="utf-8")
    for match in DEPENDENCY_TABLE_RE.finditer(text):
        key = match.group("key")
        body = match.group("body")
        if not re.search(r"(?m)^\s*path\s*=", body):
            continue
        if key in local_names or re.search(r'\bpackage\s*=\s*"unofficial-codex-', body):
            if not re.search(r'\bversion\s*=\s*"\d+\.\d+\.\d+"', body):
                errors.append(f"{manifest}: table dependency {key} has no registry version")
            if not re.search(r'\bpackage\s*=\s*"unofficial-codex-', body):
                errors.append(f"{manifest}: table dependency {key} has no package rename")

if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)

print(f"crates.io guardrails passed for {len(local_names)} packages")
