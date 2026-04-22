from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from muxer.session_io import dump_session, load_session


class SessionIORoundTripTests(unittest.TestCase):
    def test_dump_and_load_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "session.yaml"
            payload = {
                "name": "demo",
                "terminals": [
                    {
                        "id": 0,
                        "name": "term-1",
                        "cwd": "/tmp",
                        "env": {"HELLO": "world"},
                        "history_tail": ["a", "b"],
                    }
                ],
            }
            dump_session(path, payload)
            self.assertEqual(load_session(path), payload)


if __name__ == "__main__":
    unittest.main()
