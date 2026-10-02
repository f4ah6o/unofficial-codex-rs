from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("publish-workspace.py")
SPEC = importlib.util.spec_from_file_location("publish_workspace", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def package(name: str, dependencies: list[tuple[str, str | None]] = ()) -> dict:
    return {
        "id": name, "name": name, "source": None,
        "dependencies": [{"name": dep, "kind": kind} for dep, kind in dependencies],
    }


class PublishOrderTest(unittest.TestCase):
    def test_retained_dev_and_build_dependencies_precede_parent(self):
        parent = package("a-parent", [("z-dev", "dev"), ("y-build", "build")])
        data = {"packages": [parent, package("z-dev"), package("y-build")]}
        self.assertEqual(
            [p["name"] for p in MODULE.publish_order(data)],
            ["y-build", "z-dev", "a-parent"],
        )

    def test_bootstrap_omits_only_dev_edges(self):
        core = "unofficial-codex-core"
        data = {"packages": [
            package(core, [("support", "dev"), ("base", None)]),
            package("support", [(core, None)]), package("base"),
        ]}
        self.assertEqual(
            [p["name"] for p in MODULE.publish_order(data)],
            ["base", core, "support"],
        )

    def test_unlisted_dev_cycle_fails_closed(self):
        data = {"packages": [package("a", [("b", "dev")]), package("b", [("a", None)])]}
        with self.assertRaisesRegex(SystemExit, "bootstrap policy"):
            MODULE.publish_order(data)

    def test_current_workspace_regressions(self):
        names = [p["name"] for p in MODULE.publish_order(MODULE.metadata())]
        self.assertLess(names.index("unofficial-codex-git-utils"), names.index("unofficial-codex-state"))
        self.assertLess(names.index("unofficial-codex-app-test-support"), names.index("unofficial-codex-app-server"))

    def test_cargo_resolves_versioned_dev_dependencies_during_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cargo").mkdir()
            (root / "empty-registry").mkdir()
            (root / ".cargo/config.toml").write_text(
                '[source.crates-io]\nreplace-with = "empty"\n'
                '[source.empty]\ndirectory = "empty-registry"\n'
            )
            (root / "Cargo.toml").write_text('[workspace]\nmembers = ["parent", "dev"]\nresolver = "2"\n')
            for name in ["parent", "dev"]:
                path = root / name
                (path / "src").mkdir(parents=True)
                (path / "src/lib.rs").write_text("")
                manifest = f'[package]\nname = "overlay-fixture-{name}"\nversion = "1.0.0"\nedition = "2021"\n'
                if name == "parent":
                    manifest += '[dev-dependencies]\noverlay-fixture-dev = { path = "../dev", version = "1.0.0" }\n'
                (path / "Cargo.toml").write_text(manifest)
            result = subprocess.run(
                ["cargo", "package", "--offline", "--allow-dirty", "--no-verify", "-p", "overlay-fixture-parent"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no matching package", result.stderr)
            self.assertIn("overlay-fixture-dev", result.stderr)


class PublishPassTest(unittest.TestCase):
    def test_tui_publish_uses_registry_requirement_and_restores_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "Cargo.toml"
            original_root = '[workspace.dependencies]\nunicode-width = "0.2"\n'
            workspace.write_text(original_root)
            manifest = root / "tui/Cargo.toml"
            manifest.parent.mkdir()
            original = '[dev-dependencies]\nvt100 = "0.16.2"\n'
            manifest.write_text(original)
            lock = root / "Cargo.lock"
            lock.write_text("original lock")
            selected = package("unofficial-codex-tui")
            selected["manifest_path"] = str(manifest)
            def failed_run(command, **kwargs):
                self.assertNotIn("dev-dependencies", manifest.read_text())
                self.assertIn('unicode-width = "=0.2.0"', workspace.read_text())
                lock.write_text("changed lock")
                raise OSError("publish failed")
            with patch.object(MODULE, "MANIFEST", workspace), patch.object(MODULE, "LOCKFILE", lock), patch.object(MODULE.subprocess, "run", side_effect=failed_run):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    MODULE.publish(selected, dry_run=False)
            self.assertEqual(workspace.read_text(), original_root)
            self.assertEqual(manifest.read_text(), original)
            self.assertEqual(lock.read_text(), "original lock")

    def test_grouped_dry_run_restores_multiple_bootstrap_manifests_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "Cargo.lock"
            lock.write_text("original lock")
            workspace = root / "Cargo.toml"
            root_original = '[workspace]\nmembers = []\n[workspace.dependencies]\nunicode-width = "0.2"\n'
            workspace.write_text(root_original)
            packages = []
            original = '[dependencies]\nnormal = "1"\n[dev-dependencies]\nhelper = "1"\n'
            for name in ["unofficial-codex-core", "unofficial-codex-tui"]:
                manifest = root / name / "Cargo.toml"
                manifest.parent.mkdir()
                manifest.write_text(original)
                selected = package(name)
                selected["manifest_path"] = str(manifest)
                packages.append(selected)
            def failed_run(command):
                self.assertIn("--dry-run", command)
                self.assertIn("--allow-dirty", command)
                self.assertEqual(command.count("--package"), 2)
                self.assertIn('"unofficial-codex-core"', workspace.read_text())
                self.assertIn('"unofficial-codex-tui"', workspace.read_text())
                self.assertIn('unicode-width = "=0.2.0"', workspace.read_text())
                for selected in packages:
                    self.assertNotIn("dev-dependencies", Path(selected["manifest_path"]).read_text())
                lock.write_text("changed lock")
                return subprocess.CompletedProcess(command, 101)
            with patch.object(MODULE, "MANIFEST", workspace), patch.object(MODULE, "LOCKFILE", lock), patch.object(MODULE.subprocess, "run", side_effect=failed_run):
                self.assertEqual(MODULE.verify_packages(packages), 101)
            self.assertEqual(lock.read_text(), "original lock")
            self.assertEqual(workspace.read_text(), root_original)
            self.assertEqual([Path(p["manifest_path"]).read_text() for p in packages], [original, original])

    def test_grouped_dry_run_restores_after_launch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "Cargo.toml"
            workspace.write_text('[workspace]\nmembers = []\n')
            manifest = Path(directory) / "core/Cargo.toml"
            manifest.parent.mkdir()
            original = '[dev-dependencies]\nhelper = "1"\n'
            manifest.write_text(original)
            selected = package("unofficial-codex-core")
            selected["manifest_path"] = str(manifest)
            lock = Path(directory) / "Cargo.lock"
            def failed_run(command):
                lock.write_text("new lock")
                raise OSError("launch failed")
            with patch.object(MODULE, "MANIFEST", workspace), patch.object(MODULE, "LOCKFILE", lock), patch.object(MODULE.subprocess, "run", side_effect=failed_run):
                with self.assertRaisesRegex(OSError, "launch failed"):
                    MODULE.verify_packages([selected])
            self.assertEqual(manifest.read_text(), original)
            self.assertFalse(lock.exists())

    def test_retries_failed_packages_without_republishing_successes(self):
        with patch.object(MODULE, "publish", side_effect=[False, True, True]) as publish:
            with patch.object(MODULE.time, "sleep"):
                failed = MODULE.publish_in_passes(
                    [package("parent"), package("dependency")],
                    dry_run=False, max_passes=3, retry_delay=0,
                )
        self.assertEqual(failed, [])
        self.assertEqual([c.args[0]["name"] for c in publish.call_args_list], ["parent", "dependency", "parent"])

    def test_permanent_failure_is_bounded_and_reported(self):
        with patch.object(MODULE, "publish", return_value=False) as publish:
            with patch.object(MODULE.time, "sleep"):
                failed = MODULE.publish_in_passes([package("blocked")], dry_run=True, max_passes=2, retry_delay=0)
        self.assertEqual(failed, ["blocked"])
        self.assertEqual(publish.call_count, 2)
        self.assertTrue(all(c.kwargs["dry_run"] for c in publish.call_args_list))

    def test_bootstrap_restores_manifest_and_lock_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "Cargo.toml"
            text = '[dependencies]\nnormal = "1"\n[dev-dependencies.helper]\nversion = "1"\n[target.\'cfg(unix)\'.dev-dependencies]\nhelper = "1"\n[lib]\nname = "core"\n'
            manifest.write_text(text)
            lock = root / "Cargo.lock"
            lock.write_text("original lock")
            def failed_run(*args, **kwargs):
                self.assertNotIn("dev-dependencies", manifest.read_text())
                self.assertIn('normal = "1"', manifest.read_text())
                self.assertIn('[lib]', manifest.read_text())
                lock.write_text("changed lock")
                raise OSError("cargo unavailable")
            selected = package("unofficial-codex-core")
            selected["manifest_path"] = str(manifest)
            with patch.object(MODULE, "LOCKFILE", lock), patch.object(MODULE.subprocess, "run", side_effect=failed_run):
                with self.assertRaisesRegex(OSError, "cargo unavailable"):
                    MODULE.publish(selected, dry_run=True)
            self.assertEqual(manifest.read_text(), text)
            self.assertEqual(lock.read_text(), "original lock")


if __name__ == "__main__":
    unittest.main()
