import importlib.util
from pathlib import Path
import unittest
import tempfile

SPEC = importlib.util.spec_from_file_location("overlay", Path(__file__).with_name("apply-downstream-overlay.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RegistryFeaturesTest(unittest.TestCase):
    def test_removes_first_inline_feature_and_preserves_other_features(self):
        source = 'tungstenite = { version = "0.27", features = ["deflate", "proxy", "handshake"] }'
        self.assertEqual(
            MODULE.remove_registry_incompatible_features(source, {"tungstenite": {"deflate", "proxy"}}),
            'tungstenite = { version = "0.27", features = ["handshake"] }',
        )

    def test_inline_and_multiline_removal_is_order_independent(self):
        for source in [
            'tungstenite = { version = "0.27", features = ["deflate", "proxy"] }',
            'tungstenite = { version = "0.27", features = ["proxy", "deflate"] }',
            'tungstenite = { version = "0.27", features = [\n    "deflate",\n    "proxy",\n] }',
        ]:
            self.assertEqual(
                MODULE.remove_registry_incompatible_features(source, {"tungstenite": {"deflate", "proxy"}}),
                'tungstenite = { version = "0.27", features = [] }',
            )


class BundledBuildInputsTest(unittest.TestCase):
    def test_copies_native_inputs_and_preserves_license_link_across_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "workspace-vendor"
            target = Path(directory) / "packaged-vendor"
            source.mkdir()
            for name, content in {
                "bubblewrap.c": "original C source",
                "utils.h": "header",
                "COPYING": "license text",
                "README.md": "origin",
                "meson.build": "workspace-only tooling",
            }.items():
                (source / name).write_text(content)
            (source / "LICENSE").symlink_to("COPYING")
            MODULE.copy_bwrap_sources(source, target)
            self.assertEqual(
                {path.name: path.read_text() for path in target.iterdir()},
                {
                    "bubblewrap.c": "original C source",
                    "utils.h": "header",
                    "COPYING": "license text",
                    "LICENSE": "license text",
                    "README.md": "origin",
                },
            )
            self.assertEqual((target / "LICENSE").readlink(), Path("COPYING"))
            MODULE.copy_bwrap_sources(source, target)
            (source / "bubblewrap.c").write_text("updated C source")
            MODULE.copy_bwrap_sources(source, target)
            self.assertEqual((target / "bubblewrap.c").read_text(), "updated C source")
            self.assertEqual((target / "LICENSE").readlink(), Path("COPYING"))


if __name__ == "__main__":
    unittest.main()
