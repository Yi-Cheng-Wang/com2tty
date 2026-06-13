"""Tests for com2tty.wsl.pty_manager (pty primitives and termios polling)."""
import unittest
from unittest.mock import patch
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import termios  # real on Linux, mock on Windows via conftest

from com2tty.wsl.pty_manager import (
    cleanup_symlink,
    get_pty_settings,
)


class TestGetPtySettings(unittest.TestCase):

    def test_8n1_9600(self):
        attrs = [0, 0, termios.CS8, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 9600)
        self.assertEqual(bs, 8)
        self.assertEqual(par, "N")
        self.assertEqual(sb, "1")

    def test_7e2_115200(self):
        cflag = termios.CS7 | termios.PARENB | termios.CSTOPB
        attrs = [0, 0, cflag, 0, 0, termios.B115200, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 115200)
        self.assertEqual(bs, 7)
        self.assertEqual(par, "E")
        self.assertEqual(sb, "2")

    def test_odd_parity(self):
        cflag = termios.CS8 | termios.PARENB | termios.PARODD
        attrs = [0, 0, cflag, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            _, _, par, _ = get_pty_settings(3)
        self.assertEqual(par, "O")

    def test_returns_none_on_exception(self):
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr",
                   side_effect=Exception("bad fd")):
            self.assertEqual(get_pty_settings(99),
                             (None, None, None, None))



class TestCleanupSymlink(unittest.TestCase):

    @patch("os.path.lexists")
    @patch("os.unlink", create=True)
    def test_removes_existing(self, mock_unlink, mock_lex):
        mock_lex.return_value = True
        cleanup_symlink("/tmp/tty")
        mock_unlink.assert_called_once_with("/tmp/tty")

    @patch("os.path.lexists")
    @patch("os.unlink", create=True)
    def test_noop_when_absent(self, mock_unlink, mock_lex):
        mock_lex.return_value = False
        cleanup_symlink("/tmp/tty")
        mock_unlink.assert_not_called()

    @patch("os.path.lexists", return_value=True)
    @patch("os.unlink", create=True, side_effect=Exception("fail"))
    def test_handles_exception(self, mock_unlink, mock_lex):
        cleanup_symlink("/tmp/tty")  # should not raise
