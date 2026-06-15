"""Tests for com2tty.wsl.secure_io (symlink-safe writes to fixed /tmp paths)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.wsl.secure_io import secure_write


class TestSecureWrite(unittest.TestCase):

    def test_creates_file_from_str(self):
        # Fresh path: the pre-emptive unlink raises FileNotFoundError (swallowed)
        # and the file is created with the requested mode.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "marker")
            secure_write(path, "12345", mode=0o600)
            with open(path) as f:
                self.assertEqual(f.read(), "12345")

    def test_creates_file_from_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "marker.bin")
            secure_write(path, b"\x00\x01\x02")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"\x00\x01\x02")

    def test_overwrites_existing_regular_file(self):
        # An existing regular file we own is unlinked then recreated fresh.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "marker")
            with open(path, "w") as f:
                f.write("stale-and-longer")
            secure_write(path, "new")
            with open(path) as f:
                self.assertEqual(f.read(), "new")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and os.name == "posix",
                         "O_NOFOLLOW symlink rejection is POSIX-only")
    def test_rejects_symlink_target(self):
        # A symlink planted at the path is unlinked (not followed); the real
        # write lands on a fresh regular file, leaving the link's old target
        # untouched.
        with tempfile.TemporaryDirectory() as d:
            victim = os.path.join(d, "victim")
            with open(victim, "w") as f:
                f.write("precious")
            path = os.path.join(d, "marker")
            os.symlink(victim, path)
            secure_write(path, "owned")
            self.assertFalse(os.path.islink(path))
            with open(path) as f:
                self.assertEqual(f.read(), "owned")
            with open(victim) as f:
                self.assertEqual(f.read(), "precious")  # never followed


if __name__ == "__main__":
    unittest.main()
