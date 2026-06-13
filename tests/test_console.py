"""Tests for com2tty.windows.os_hacks.console (banner colours and VT mode)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.os_hacks.console import (
    enable_vt_mode,
    get_banner_colors,
)


class TestBannerColors(unittest.TestCase):

    @patch.dict("os.environ", {"NO_COLOR": "1"})
    def test_no_color_env(self):
        self.assertEqual(get_banner_colors(), ("", "", "", ""))

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.windows.os_hacks.console.sys.stdout")
    def test_not_a_tty(self, mock_stdout):
        mock_stdout.isatty.return_value = False
        self.assertEqual(get_banner_colors(), ("", "", "", ""))

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.windows.os_hacks.console.os.name", "posix")
    @patch("com2tty.windows.os_hacks.console.sys.stdout")
    def test_posix_tty_colored(self, mock_stdout):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors()[0], "\033[93m")

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.windows.os_hacks.console.enable_vt_mode", return_value=True)
    @patch("com2tty.windows.os_hacks.console.os.name", "nt")
    @patch("com2tty.windows.os_hacks.console.sys.stdout")
    def test_windows_vt_enabled(self, mock_stdout, mock_vt):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors()[3], "\033[0m")
        mock_vt.assert_called_once()

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.windows.os_hacks.console.enable_vt_mode", return_value=False)
    @patch("com2tty.windows.os_hacks.console.os.name", "nt")
    @patch("com2tty.windows.os_hacks.console.sys.stdout")
    def test_windows_vt_unavailable(self, mock_stdout, mock_vt):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors(), ("", "", "", ""))



class TestEnableVtMode(unittest.TestCase):

    def test_success(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 1
        m.windll.kernel32.SetConsoleMode.return_value = 1
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertTrue(enable_vt_mode())

    def test_get_console_mode_fails(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 0
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())

    def test_set_console_mode_fails(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 1
        m.windll.kernel32.SetConsoleMode.return_value = 0
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())

    def test_exception_returns_false(self):
        m = MagicMock()
        m.windll.kernel32.GetStdHandle.side_effect = OSError("no console")
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())
