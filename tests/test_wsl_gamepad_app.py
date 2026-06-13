"""Tests for com2tty.wsl.gamepad_app (the WSL gamepad helper entry point).

The ``main`` select loop is an integration entry point (``# pragma: no
cover``); this exercises the importable, pure surface: the module imports
and the argument parser, whose defaults are part of the helper's CLI
contract with the Windows host that spawns it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.core.constants import DEFAULT_PAD_FIFO, DEFAULT_PAD_NAME
from com2tty.wsl import gamepad_app


class TestBuildArgParser(unittest.TestCase):

    def test_defaults(self):
        args = gamepad_app.build_arg_parser().parse_args([])
        self.assertEqual(args.pad_index, 0)
        self.assertEqual(args.name, DEFAULT_PAD_NAME)
        self.assertFalse(args.uinput)
        self.assertEqual(args.tmp_path, DEFAULT_PAD_FIFO)

    def test_tmp_path_default_matches_shared_constant(self):
        # The helper's --tmp-path default must equal the path the Windows
        # host passes by default, or the FIFO endpoints would diverge.
        self.assertEqual(gamepad_app.DEFAULT_TMP_PAD, DEFAULT_PAD_FIFO)

    def test_parses_all_options(self):
        args = gamepad_app.build_arg_parser().parse_args(
            ["--pad-index", "2", "--name", "Pad", "--uinput",
             "--tmp-path", "/tmp/custompad"])
        self.assertEqual(args.pad_index, 2)
        self.assertEqual(args.name, "Pad")
        self.assertTrue(args.uinput)
        self.assertEqual(args.tmp_path, "/tmp/custompad")

    def test_short_flags(self):
        args = gamepad_app.build_arg_parser().parse_args(
            ["-i", "3", "-n", "X", "-u", "-p", "/tmp/p"])
        self.assertEqual(args.pad_index, 3)
        self.assertEqual(args.name, "X")
        self.assertTrue(args.uinput)
        self.assertEqual(args.tmp_path, "/tmp/p")


if __name__ == "__main__":
    unittest.main()
