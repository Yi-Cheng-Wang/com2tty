"""Tests for com2tty.windows.os_hacks.autoplay (AutoPlay suppression and crash recovery)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.os_hacks.autoplay import AutoplaySuppressor


class TestAutoplaySuppressor(unittest.TestCase):

    def setUp(self):
        # Keep the marker file out of the real temp dir during these tests.
        import com2tty.windows.os_hacks.autoplay as uf2
        self._marker = os.path.join(
            os.path.dirname(__file__), "_test_autoplay_marker.json")
        self._patcher = patch.object(
            uf2, "_autoplay_marker_path", return_value=self._marker)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._marker):
            os.remove(self._marker)

    def test_init(self):
        """Lines 208-212: __init__ sets attributes."""
        sup = AutoplaySuppressor()
        self.assertIsNone(sup.original_value)
        self.assertFalse(sup.existed)
        self.assertFalse(sup.modified)

    @patch("com2tty.windows.os_hacks.autoplay.winreg", None)
    def test_enter_no_winreg(self):
        """Line 215-216: winreg is None ??early return."""
        sup = AutoplaySuppressor()
        result = sup.__enter__()
        self.assertIs(result, sup)
        self.assertFalse(sup.modified)

    @patch("com2tty.windows.os_hacks.autoplay.winreg", None)
    def test_exit_no_winreg(self):
        """Line 234: winreg is None ??exit does nothing."""
        sup = AutoplaySuppressor()
        sup.__exit__(None, None, None)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_enter_existing_value(self, mock_winreg):
        """Lines 217-228: OpenKey succeeds, QueryValueEx returns existing value."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.return_value = (0, 1)  # value, type
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        sup = AutoplaySuppressor()
        result = sup.__enter__()
        self.assertIs(result, sup)
        self.assertTrue(sup.existed)
        self.assertEqual(sup.original_value, 0)
        self.assertTrue(sup.modified)
        mock_winreg.SetValueEx.assert_called_once()
        mock_winreg.CloseKey.assert_called_once_with(mock_key)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_enter_file_not_found(self, mock_winreg):
        """Lines 222-224: QueryValueEx raises FileNotFoundError ??existed=False."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.side_effect = FileNotFoundError("no val")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        sup = AutoplaySuppressor()
        sup.__enter__()
        self.assertFalse(sup.existed)
        self.assertEqual(sup.original_value, 0)
        self.assertTrue(sup.modified)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_enter_open_key_exception(self, mock_winreg):
        """Lines 229-230: OpenKey raises exception ??modified stays False."""
        mock_winreg.OpenKey.side_effect = OSError("access denied")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.__enter__()
        self.assertFalse(sup.modified)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_exit_restore_existing(self, mock_winreg):
        """Lines 236-242: Exit restores existing value."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        sup = AutoplaySuppressor()
        sup.modified = True
        sup.existed = True
        sup.original_value = 0
        sup.__exit__(None, None, None)

        mock_winreg.SetValueEx.assert_called_once()
        mock_winreg.CloseKey.assert_called_once_with(mock_key)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_exit_delete_value(self, mock_winreg):
        """Lines 240-241: Exit deletes value when it didn't exist before."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.modified = True
        sup.existed = False
        sup.__exit__(None, None, None)

        mock_winreg.DeleteValue.assert_called_once()
        mock_winreg.CloseKey.assert_called_once_with(mock_key)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_exit_not_modified(self, mock_winreg):
        """Line 234: modified is False ??exit early."""
        sup = AutoplaySuppressor()
        sup.modified = False
        sup.__exit__(None, None, None)
        mock_winreg.OpenKey.assert_not_called()

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_exit_exception(self, mock_winreg):
        """Lines 243-244: Exit exception is swallowed."""
        mock_winreg.OpenKey.side_effect = OSError("fail")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.modified = True
        sup.__exit__(None, None, None)  # should not raise

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_enter_writes_recovery_marker(self, mock_winreg):
        """__enter__ persists prior state to the marker file before modifying."""
        import json
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.return_value = (3, 1)
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        AutoplaySuppressor().__enter__()
        self.assertTrue(os.path.exists(self._marker))
        with open(self._marker) as f:
            state = json.load(f)
        self.assertTrue(state["existed"])
        self.assertEqual(state["original_value"], 3)

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_enter_marker_write_failure_is_tolerated(self, mock_winreg):
        """A failure writing the marker does not abort suppression."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.return_value = (0, 1)
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        with patch("com2tty.windows.os_hacks.autoplay.open", side_effect=OSError("disk full")):
            sup = AutoplaySuppressor()
            sup.__enter__()
        self.assertTrue(sup.modified)



class TestRestoreOrphanedAutoplay(unittest.TestCase):

    def setUp(self):
        import com2tty.windows.os_hacks.autoplay as uf2
        self._marker = os.path.join(
            os.path.dirname(__file__), "_test_orphan_marker.json")
        self._patcher = patch.object(
            uf2, "_autoplay_marker_path", return_value=self._marker)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._marker):
            os.remove(self._marker)

    def _write_marker(self, state):
        import json
        with open(self._marker, "w") as f:
            json.dump(state, f)

    def test_no_marker_is_noop(self):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        restore_orphaned_autoplay()  # should not raise

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_restores_existing_value_and_removes_marker(self, mock_winreg):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        self._write_marker({"existed": True, "original_value": 7})
        restore_orphaned_autoplay()

        mock_winreg.SetValueEx.assert_called_once()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_restores_deleted_value(self, mock_winreg):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        self._write_marker({"existed": False, "original_value": 0})
        restore_orphaned_autoplay()

        mock_winreg.DeleteValue.assert_called_once()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_restore_delete_value_not_found(self, mock_winreg):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.DeleteValue.side_effect = FileNotFoundError()
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        self._write_marker({"existed": False, "original_value": 0})
        restore_orphaned_autoplay()  # should not raise
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.windows.os_hacks.autoplay.winreg", None)
    def test_restore_no_winreg(self):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        self._write_marker({"existed": True, "original_value": 1})
        restore_orphaned_autoplay()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.windows.os_hacks.autoplay.winreg")
    def test_restore_corrupt_marker(self, mock_winreg):
        from com2tty.windows.os_hacks.autoplay import restore_orphaned_autoplay
        with open(self._marker, "w") as f:
            f.write("not json{")
        restore_orphaned_autoplay()  # should not raise
        self.assertFalse(os.path.exists(self._marker))
