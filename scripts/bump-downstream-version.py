#!/usr/bin/env python3
"""Advance the downstream CalVer used by the registry mirror."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "DOWNSTREAM_VERSION"
current = VERSION_FILE.read_text(encoding="utf-8").strip()
match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
if not match:
    raise SystemExit(f"invalid downstream version: {current!r}")

now = datetime.now(timezone.utc)
year, month, patch = map(int, match.groups())
if (year, month) == (now.year, now.month):
    next_version = f"{year}.{month}.{patch + 1}"
else:
    next_version = f"{now.year}.{now.month}.0"

VERSION_FILE.write_text(next_version + "\n", encoding="utf-8")
print(next_version)
