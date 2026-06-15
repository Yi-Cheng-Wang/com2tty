"""Tests for com2tty.cli.profiles (@profile expansion from com2tty.ini)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.cli.profiles import (
    ProfileError,
    default_search_paths,
    expand_profiles,
    load_profile_args,
)


def _ini(content):
    f = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestLoadProfileArgs(unittest.TestCase):

    def setUp(self):
        self._files = []

    def tearDown(self):
        for path in self._files:
            os.unlink(path)

    def _write(self, content):
        path = _ini(content)
        self._files.append(path)
        return path

    def test_basic_serial_profile(self):
        path = self._write(
            "[myboard]\n"
            "port = COM5\n"
            "baud = 115200\n"
            "wsl-tty = /tmp/my_device\n"
            "board = pico\n")
        args = load_profile_args("myboard", [path])
        self.assertEqual(args[0], "COM5")
        self.assertIn("--baud", args)
        self.assertEqual(args[args.index("--baud") + 1], "115200")
        self.assertIn("--wsl-tty", args)
        self.assertIn("--board", args)

    def test_underscores_accepted(self):
        path = self._write("[p]\nwsl_tty = /tmp/x\n")
        args = load_profile_args("p", [path])
        self.assertEqual(args, ["--wsl-tty", "/tmp/x"])

    def test_multiple_ports_drive_multi_port_mode(self):
        # A whitespace-separated port list expands to several positionals, in
        # order, so a profile can drive multi-port mode like the command line.
        path = self._write("[multi]\nport = COM3 COM5\nbaud = 115200\n")
        args = load_profile_args("multi", [path])
        self.assertEqual(args[0], "COM3")
        self.assertEqual(args[1], "COM5")
        self.assertIn("--baud", args)

    def test_true_flag_included(self):
        path = self._write("[pad]\ngamepad = true\nuinput = yes\n")
        args = load_profile_args("pad", [path])
        self.assertIn("--gamepad", args)
        self.assertIn("--uinput", args)

    def test_false_flag_omitted(self):
        path = self._write("[pad]\ngamepad = true\nuinput = false\n")
        args = load_profile_args("pad", [path])
        self.assertNotIn("--uinput", args)

    def test_invalid_flag_value_rejected(self):
        path = self._write("[pad]\ngamepad = maybe\n")
        with self.assertRaises(ProfileError):
            load_profile_args("pad", [path])

    def test_missing_file(self):
        with self.assertRaises(ProfileError):
            load_profile_args("x", [os.path.join(tempfile.gettempdir(),
                                                 "com2tty_does_not_exist.ini")])

    def test_missing_section_lists_available(self):
        path = self._write("[other]\nbaud = 9600\n")
        with self.assertRaises(ProfileError) as ctx:
            load_profile_args("missing", [path])
        self.assertIn("other", str(ctx.exception))

    def test_malformed_file(self):
        path = self._write("not an ini at all\n[broken\n")
        with self.assertRaises(ProfileError):
            load_profile_args("x", [path])

    def test_default_search_paths(self):
        paths = default_search_paths()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith("com2tty.ini"))
        self.assertTrue(paths[1].endswith(".com2tty.ini"))

    def test_utf8_file_with_bom_and_non_ascii_comment(self):
        # The file is read as UTF-8 regardless of the Windows ANSI codepage.
        f = tempfile.NamedTemporaryFile("wb", suffix=".ini", delete=False)
        f.write("﻿# 開發板設定\n[b]\nport = COM9\n".encode("utf-8"))
        f.close()
        self._files.append(f.name)
        args = load_profile_args("b", [f.name])
        self.assertEqual(args, ["COM9"])

    def test_legacy_encoding_falls_back_to_latin1(self):
        # Bytes that are not valid UTF-8 (a legacy ANSI-codepage file) must
        # still parse; ASCII keys/values are unaffected by the fallback.
        f = tempfile.NamedTemporaryFile("wb", suffix=".ini", delete=False)
        f.write(b"# legacy comment \xb0\xea\xbb\xd8\n[b]\nport = COM9\n")
        f.close()
        self._files.append(f.name)
        args = load_profile_args("b", [f.name])
        self.assertEqual(args, ["COM9"])

    def test_new_flag_options_accepted(self):
        path = self._write("[w]\nport = COM4\nwait = true\njson = false\n")
        args = load_profile_args("w", [path])
        self.assertIn("--wait", args)
        self.assertNotIn("--json", args)


class TestExpandProfiles(unittest.TestCase):

    def test_expands_at_token_in_place(self):
        path = _ini("[b]\nport = COM7\nbaud = 9600\n")
        try:
            argv = expand_profiles(["@b", "--debug"], [path])
            self.assertEqual(argv[0], "COM7")
            self.assertIn("--baud", argv)
            self.assertEqual(argv[-1], "--debug")
        finally:
            os.unlink(path)

    def test_non_profile_tokens_untouched(self):
        self.assertEqual(expand_profiles(["COM3", "--baud", "9600"]),
                         ["COM3", "--baud", "9600"])

    def test_bare_at_passes_through(self):
        self.assertEqual(expand_profiles(["@"]), ["@"])

    def test_doubled_at_is_escaped_to_literal(self):
        # ``@@value`` is a literal ``@value`` argument, not a profile ref, so
        # it is never looked up on disk.
        self.assertEqual(
            expand_profiles(["--name", "@@home"]), ["--name", "@home"])


class TestCliProfileIntegration(unittest.TestCase):

    @patch("com2tty.cli.run_bridge")
    def test_profile_used_by_cli(self, mock_run):
        from com2tty.cli import main
        path = _ini("[b]\nport = COM7\nbaud = 57600\n")
        try:
            with patch("com2tty.cli.profiles.default_search_paths",
                       return_value=[path]), \
                 patch("sys.argv", ["com2tty", "@b"]):
                main()
        finally:
            os.unlink(path)
        self.assertEqual(mock_run.call_args[1]["port"], "COM7")
        self.assertEqual(mock_run.call_args[1]["baud"], "57600")

    @patch("com2tty.cli.run_bridge")
    def test_cli_args_override_profile(self, mock_run):
        from com2tty.cli import main
        path = _ini("[b]\nport = COM7\nbaud = 57600\n")
        try:
            with patch("com2tty.cli.profiles.default_search_paths",
                       return_value=[path]), \
                 patch("sys.argv", ["com2tty", "@b", "--baud", "115200"]):
                main()
        finally:
            os.unlink(path)
        self.assertEqual(mock_run.call_args[1]["baud"], "115200")

    def test_unknown_profile_is_cli_error(self):
        from com2tty.cli import main
        path = _ini("[b]\nbaud = 9600\n")
        try:
            with patch("com2tty.cli.profiles.default_search_paths",
                       return_value=[path]), \
                 patch("sys.argv", ["com2tty", "@nope"]):
                with self.assertRaises(SystemExit):
                    main()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
