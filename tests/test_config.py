from __future__ import annotations

import unittest

from muxer.config import prefix_bytes, prefix_label, resolve_prefix_binding


class ConfigTests(unittest.TestCase):
    def test_default_prefix(self) -> None:
        self.assertEqual(resolve_prefix_binding(None), "c-a")

    def test_prefix_aliases_normalize(self) -> None:
        self.assertEqual(resolve_prefix_binding("^b"), "c-b")
        self.assertEqual(resolve_prefix_binding("Ctrl-C"), "c-c")
        self.assertEqual(resolve_prefix_binding("control-d"), "c-d")

    def test_prefix_helpers(self) -> None:
        self.assertEqual(prefix_label("c-a"), "Ctrl+A")
        self.assertEqual(prefix_bytes("c-a"), b"\x01")

    def test_invalid_prefix_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_prefix_binding("alt-a")


if __name__ == "__main__":
    unittest.main()
