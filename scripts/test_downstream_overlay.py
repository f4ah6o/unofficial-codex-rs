import importlib.util
from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()
