"""Tests for com2tty.core.util (shared md5 + indexed-path helpers)."""
import hashlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.core.util import indexed_path, md5_hexdigest


class TestMd5Hexdigest(unittest.TestCase):

    def test_matches_hashlib(self):
        self.assertEqual(md5_hexdigest(b"data"),
                         hashlib.md5(b"data").hexdigest())

    def test_fallback_without_usedforsecurity(self):
        real_md5 = hashlib.md5

        def legacy_md5(data, **kwargs):
            if kwargs:  # the usedforsecurity= keyword: pretend it is unsupported
                raise TypeError("usedforsecurity not supported")
            return real_md5(data)

        with patch("hashlib.md5", side_effect=legacy_md5):
            self.assertEqual(md5_hexdigest(b"data"),
                             real_md5(b"data").hexdigest())


class TestIndexedPath(unittest.TestCase):

    def test_index_zero_returns_base_verbatim(self):
        self.assertEqual(indexed_path("/tmp/ttyUSB0", 0), "/tmp/ttyUSB0")

    def test_increments_trailing_number(self):
        self.assertEqual(indexed_path("/tmp/ttyUSB0", 1), "/tmp/ttyUSB1")
        self.assertEqual(indexed_path("/tmp/ttyUSB0", 3), "/tmp/ttyUSB3")
        self.assertEqual(indexed_path("/tmp/com2pad0", 2), "/tmp/com2pad2")

    def test_increments_nonzero_base_number(self):
        # The trailing number need not be zero; the index is added to it.
        self.assertEqual(indexed_path("/tmp/ttyUSB5", 2), "/tmp/ttyUSB7")

    def test_appends_index_when_no_trailing_number(self):
        self.assertEqual(indexed_path("/tmp/my_device", 2), "/tmp/my_device2")


if __name__ == "__main__":
    unittest.main()
