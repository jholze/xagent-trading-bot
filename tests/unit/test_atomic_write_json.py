"""Concurrent atomic_write_json must not collide on a shared .tmp name."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from data_manager import atomic_write_json


class TestAtomicWriteJson(unittest.TestCase):
    def test_concurrent_writers_all_succeed(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "watchlist_quality_scores.demo.json")
            errors: list[BaseException] = []

            def _write(i: int) -> None:
                try:
                    atomic_write_json(path, {"n": i, "coins": [i]})
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_write, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("n", data)
            leftover = [n for n in os.listdir(td) if n.endswith(".tmp")]
            self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
