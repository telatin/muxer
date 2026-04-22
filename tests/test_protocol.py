from __future__ import annotations

import unittest

from muxer.cli import parse_args
from muxer.protocol import decode_bytes, encode_bytes


class ProtocolTests(unittest.TestCase):
    def test_base64_roundtrip(self) -> None:
        raw = b"\x1b[Ahello\x00world"
        self.assertEqual(decode_bytes(encode_bytes(raw)), raw)

    def test_cli_defaults_to_new_main(self) -> None:
        args = parse_args([])
        self.assertEqual(args.command, "new")
        self.assertEqual(args.session, "main")
        self.assertIsNone(args.restore)
        self.assertIsNone(args.prefix)

    def test_cli_accepts_prefix_after_subcommand(self) -> None:
        args = parse_args(["attach", "work", "--prefix", "c-b"])
        self.assertEqual(args.command, "attach")
        self.assertEqual(args.session, "work")
        self.assertEqual(args.prefix, "c-b")


if __name__ == "__main__":
    unittest.main()
