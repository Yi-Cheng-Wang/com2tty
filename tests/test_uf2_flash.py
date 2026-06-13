"""Tests for com2tty.windows.uf2_flash (drive discovery and USB-serial mapping)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.uf2_flash import (
    get_drive_by_serial,
    list_removable_drives,
    md5_hexdigest,
)


class TestGetDriveBySerial(unittest.TestCase):

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_single_dict(self, mock_run):
        """Lines 738-740: JSON output is a single dict."""
        mock_run.return_value = MagicMock(
            stdout='{"DriveLetter":"E:\\\\","PNPDeviceID":"USB\\\\..."}',
        )
        self.assertEqual(get_drive_by_serial("ABC123"), "E:\\")

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_list_result(self, mock_run):
        """Lines 741-742: JSON output is a list."""
        mock_run.return_value = MagicMock(
            stdout='[{"DriveLetter":"F:\\\\","PNPDeviceID":"USB\\\\..."}]',
        )
        self.assertEqual(get_drive_by_serial("ABC123"), "F:\\")

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_empty_output(self, mock_run):
        """Lines 735-736: Empty output returns None."""
        mock_run.return_value = MagicMock(stdout="")
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_exception(self, mock_run):
        """Lines 743-745: Exception returns None."""
        mock_run.side_effect = Exception("powershell fail")
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_empty_list(self, mock_run):
        """Lines 741-742: JSON list is empty."""
        mock_run.return_value = MagicMock(stdout="[]")
        # An empty list ??len(data) == 0, so the elif is skipped ??returns None at end
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.windows.uf2_flash.subprocess.run")
    def test_serial_passed_via_env_not_interpolated(self, mock_run):
        """Security: a hostile serial must reach PowerShell via the environment,
        never spliced into the script text."""
        mock_run.return_value = MagicMock(stdout="")
        malicious = '"; Remove-Item C:\\ -Recurse; $("'
        get_drive_by_serial(malicious)

        args, kwargs = mock_run.call_args
        ps_script = args[0][-1]
        # The raw serial must NOT appear anywhere in the command text.
        self.assertNotIn(malicious, ps_script)
        self.assertNotIn("Remove-Item", ps_script)
        # It must be delivered through the environment variable instead.
        self.assertEqual(kwargs["env"]["COM2TTY_TARGET_SERIAL"], malicious)
        # And the script must regex-escape the value before matching.
        self.assertIn("[regex]::Escape", ps_script)



class TestListRemovableDrives(unittest.TestCase):

    def test_filters_removable(self):
        m = MagicMock()
        m.windll.kernel32.GetDriveTypeW.side_effect = (
            lambda root: 2 if root == "E:\\" else 3)
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertEqual(list_removable_drives(), ["E:\\"])

    @patch("com2tty.windows.uf2_flash.os.path.exists",
           side_effect=lambda p: p == "F:\\")
    def test_fallback_on_error(self, mock_exists):
        m = MagicMock()
        m.windll.kernel32.GetDriveTypeW.side_effect = OSError("boom")
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertEqual(list_removable_drives(), ["F:\\"])



class TestHostMd5Hexdigest(unittest.TestCase):

    def test_matches_hashlib(self):
        import hashlib
        self.assertEqual(md5_hexdigest(b"x"), hashlib.md5(b"x").hexdigest())

    def test_fallback_without_usedforsecurity(self):
        import hashlib
        real_md5 = hashlib.md5

        def legacy_md5(data, **kwargs):
            if kwargs:
                raise TypeError("usedforsecurity not supported")
            return real_md5(data)

        with patch("hashlib.md5", side_effect=legacy_md5):
            self.assertEqual(md5_hexdigest(b"x"), real_md5(b"x").hexdigest())
