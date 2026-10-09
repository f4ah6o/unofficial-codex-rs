#!/usr/bin/env python3
"""Rebuild the overlay from recorded upstream and compare the source tree.

Cargo.lock is checked separately by locked Cargo commands: its regeneration
depends on the registry index, whereas this check is deliberately offline.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workspace_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): (
            str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        )
        for path in sorted((root / "codex-rs").rglob("*"))
        if (path.is_file() or path.is_symlink())
        and "target" not in path.relative_to(root).parts
        and path != root / "codex-rs/Cargo.lock"
    }


def main() -> int:
    revision = (ROOT / "UPSTREAM_REVISION").read_text().strip()
    with tempfile.TemporaryDirectory(prefix="codex-overlay-") as directory:
        stage = Path(directory)
        archive = stage / "upstream.tar"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "archive", revision, "codex-rs"],
                cwd=ROOT, stdout=output, check=True,
            )
        subprocess.run(["tar", "xf", str(archive), "-C", str(stage)], check=True)
        (stage / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/apply-downstream-overlay.py", stage / "scripts")
        shutil.copytree(ROOT / "scripts/patches", stage / "scripts/patches")
        shutil.copy(ROOT / "DOWNSTREAM_VERSION", stage)
        command = [sys.executable, str(stage / "scripts/apply-downstream-overlay.py")]
        subprocess.run(command, cwd=stage, check=True)
        generated = workspace_snapshot(stage)
        subprocess.run(command, cwd=stage, check=True)
        if workspace_snapshot(stage) != generated:
            raise SystemExit("overlay is not idempotent")
        expected = workspace_snapshot(ROOT)
        changed = sorted(
            path for path in expected.keys() | generated.keys()
            if expected.get(path) != generated.get(path)
        )
        if changed:
            raise SystemExit("overlay regeneration differs:\n" + "\n".join(changed))
    print("Overlay regeneration and idempotence passed (excluding Cargo.lock).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
