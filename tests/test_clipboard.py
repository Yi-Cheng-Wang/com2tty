"""Tests for com2tty.windows.os_hacks.clipboard (Windows clipboard via Win32).

The clipboard is set with the Win32 API through ``ctypes`` (no ``clip.exe``
subprocess, which could disturb the console mode and crash the dashboard on
Ctrl+C). These tests drive that API with a fake ``ctypes`` so they run on any
platform.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.os_hacks.clipboard import set_windows_clipboard


def _fake_ctypes(*, open_ok=True, alloc=0x1000, lock=0x2000, set_ok=True):
    """A MagicMock standing in for the ``ctypes`` module, with Win32 stubs."""
    fake = MagicMock(name="ctypes")
    user32 = fake.windll.user32
    kernel32 = fake.windll.kernel32
    user32.OpenClipboard.return_value = 1 if open_ok else 0
    kernel32.GlobalAlloc.return_value = alloc
    kernel32.GlobalLock.return_value = lock
    user32.SetClipboardData.return_value = (alloc if set_ok else 0)
    return fake


class TestSetWindowsClipboard(unittest.TestCase):

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_success_stores_unicode_text(self):
        fake = _fake_ctypes()
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertTrue(set_windows_clipboard("sudo ln -sf a b"))
        # CF_UNICODETEXT (13) was used and the data was moved into the handle.
        fmt, handle = fake.windll.user32.SetClipboardData.call_args[0]
        self.assertEqual(fmt, 13)
        self.assertEqual(handle, 0x1000)
        fake.windll.user32.OpenClipboard.assert_called_once()
        fake.windll.user32.CloseClipboard.assert_called_once()
        fake.memmove.assert_called_once()
        # On success the system owns the handle; we must not free it.
        fake.windll.kernel32.GlobalFree.assert_not_called()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_unicode_is_encoded_utf16(self):
        fake = _fake_ctypes()
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertTrue(set_windows_clipboard("café"))
        data = fake.memmove.call_args[0][1]
        self.assertEqual(data, "café".encode("utf-16-le") + b"\x00\x00")

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "posix")
    def test_returns_false_off_windows(self):
        # No ctypes.windll access at all off Windows.
        with patch("com2tty.windows.os_hacks.clipboard.ctypes") as fake:
            self.assertFalse(set_windows_clipboard("hello"))
            fake.windll.user32.OpenClipboard.assert_not_called()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_returns_false_when_open_clipboard_fails(self):
        fake = _fake_ctypes(open_ok=False)
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertFalse(set_windows_clipboard("hello"))
        fake.windll.user32.CloseClipboard.assert_not_called()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_returns_false_when_alloc_fails(self):
        fake = _fake_ctypes(alloc=0)  # GlobalAlloc returns NULL
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertFalse(set_windows_clipboard("hello"))
        fake.windll.user32.CloseClipboard.assert_called_once()
        fake.windll.kernel32.GlobalLock.assert_not_called()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_frees_handle_when_lock_fails(self):
        fake = _fake_ctypes(lock=0)  # GlobalLock returns NULL
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertFalse(set_windows_clipboard("hello"))
        # The allocated handle is freed (ownership never transferred) and the
        # clipboard is still closed.
        fake.windll.kernel32.GlobalFree.assert_called_once_with(0x1000)
        fake.windll.user32.CloseClipboard.assert_called_once()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_frees_handle_and_closes_when_setdata_fails(self):
        fake = _fake_ctypes(set_ok=False)
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertFalse(set_windows_clipboard("hello"))
        # The handle ownership never transferred, so we free it, and still close.
        fake.windll.kernel32.GlobalFree.assert_called_once_with(0x1000)
        fake.windll.user32.CloseClipboard.assert_called_once()

    @patch("com2tty.windows.os_hacks.clipboard.os.name", "nt")
    def test_returns_false_on_exception(self):
        fake = _fake_ctypes()
        fake.windll.user32.OpenClipboard.side_effect = OSError("boom")
        with patch("com2tty.windows.os_hacks.clipboard.ctypes", fake):
            self.assertFalse(set_windows_clipboard("hello"))


if __name__ == "__main__":
    unittest.main()
