"""Tests for com2tty.windows.os_hacks.explorer (BOOTSEL window closing).

The ``BootselWindowCloser`` scan loop runs on a daemon thread in production.
Driving it through ``start()``/``stop()`` makes coverage of its exception
branch depend on thread scheduling, which differs between interpreter
versions (it flaked on the Windows + Python 3.9 CI). These tests call
``_run`` synchronously with a mocked ``ctypes`` so every branch is hit
deterministically.
"""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.os_hacks import explorer
from com2tty.windows.os_hacks.explorer import BootselWindowCloser


def _mock_ctypes():
    """A MagicMock standing in for the ctypes module the closer imports."""
    return MagicMock(name="ctypes")


class TestMatches(unittest.TestCase):

    def test_matches_volume_label(self):
        closer = BootselWindowCloser([])
        self.assertTrue(closer._matches("RPI-RP2"))
        self.assertTrue(closer._matches("SOMETHING RP2350 SOMETHING"))

    def test_matches_drive_letter_parenthesised(self):
        closer = BootselWindowCloser(["T"])
        self.assertTrue(closer._matches("USB DRIVE (T:)"))

    def test_no_match(self):
        closer = BootselWindowCloser(["T"])
        # A bare "T:" without the parenthesised form must not match.
        self.assertFalse(closer._matches("DOCUMENTS T: BACKUP"))
        self.assertFalse(closer._matches("UNRELATED WINDOW"))


class TestRunExceptionBranch(unittest.TestCase):

    def test_exception_in_loop_is_caught_and_logged(self):
        # Deterministically exercises the except branch: the first
        # EnumWindows call raises, so the loop body fails on iteration one
        # regardless of scheduling, and _run must swallow it.
        closer = BootselWindowCloser([])
        closer._stop_event = threading.Event()  # never set -> enters loop
        mock_ctypes = _mock_ctypes()
        mock_ctypes.windll.user32.EnumWindows.side_effect = RuntimeError("boom")

        with patch.dict(sys.modules, {"ctypes": mock_ctypes}), \
                patch.object(explorer.logging, "debug") as mock_debug:
            closer._run()  # must return, not raise

        mock_debug.assert_called_once()
        self.assertIn("window closer thread", mock_debug.call_args[0][0])


class TestRunCleanLoop(unittest.TestCase):

    def test_one_scan_then_stop(self):
        # is_set() returns False once (enter loop), then True (exit), so the
        # loop body runs exactly once without raising.
        closer = BootselWindowCloser([])
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        closer._stop_event = stop
        mock_ctypes = _mock_ctypes()

        with patch.dict(sys.modules, {"ctypes": mock_ctypes}), \
                patch.object(explorer.time, "sleep") as mock_sleep:
            closer._run()

        self.assertEqual(mock_ctypes.windll.user32.EnumWindows.call_count, 1)
        mock_sleep.assert_called_once_with(explorer.CLOSER_SCAN_INTERVAL)


class TestRunCallbackBody(unittest.TestCase):
    """Drive the closer's foreach_window callback directly so its match /
    hide / close body is covered without relying on a live thread."""

    def _run_one_scan(self, target_letters, hwnd, class_name, title):
        closer = BootselWindowCloser(target_letters)
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]  # one scan, then exit
        closer._stop_event = stop
        mock_ctypes = _mock_ctypes()
        user32 = mock_ctypes.windll.user32
        mock_ctypes.WINFUNCTYPE.return_value = lambda func: func

        def _enum(cb, _lparam):
            cb(hwnd, 0)
            return True

        user32.EnumWindows.side_effect = _enum
        user32.GetClassNameW.side_effect = \
            lambda h, b, s: setattr(b, "value", class_name)
        user32.GetWindowTextW.side_effect = \
            lambda h, b, s: setattr(b, "value", title)
        mock_ctypes.create_unicode_buffer.side_effect = \
            lambda n: MagicMock(value="")

        with patch.dict(sys.modules, {"ctypes": mock_ctypes}), \
                patch.object(explorer.time, "sleep"):
            closer._run()
        return user32

    def test_matching_window_hidden_and_closed(self):
        user32 = self._run_one_scan(
            ["T"], 0x111, "CabinetWClass", "RPI-RP2 (T:)")
        self.assertTrue(user32.ShowWindow.called)
        self.assertTrue(user32.PostMessageW.called)

    def test_non_explorer_window_ignored(self):
        user32 = self._run_one_scan(
            ["T"], 0x222, "NotCabinet", "RPI-RP2 (T:)")
        user32.ShowWindow.assert_not_called()
        user32.PostMessageW.assert_not_called()


class TestStartStopLifecycle(unittest.TestCase):

    def test_start_then_stop_joins_thread(self):
        closer = BootselWindowCloser([])
        mock_ctypes = _mock_ctypes()
        # Keep the loop tight and exit immediately on stop.
        with patch.dict(sys.modules, {"ctypes": mock_ctypes}), \
                patch.object(explorer.time, "sleep"):
            closer.start()
            self.assertIsNotNone(closer._thread)
            closer.stop(timeout=2.0)
        self.assertIsNone(closer._thread)

    def test_stop_without_start_is_noop(self):
        closer = BootselWindowCloser([])
        closer.stop()  # _stop_event and _thread are None; must not raise


class TestCloseExplorerForDrive(unittest.TestCase):

    def _enum_invoking(self, hwnd):
        """EnumWindows side effect that calls the registered callback once."""
        def _enum(cb, _lparam):
            cb(hwnd, 0)
            return True
        return _enum

    def test_matching_window_is_hidden_and_closed(self):
        mock_ctypes = _mock_ctypes()
        user32 = mock_ctypes.windll.user32
        # WINFUNCTYPE(...)(func) -> return func so EnumWindows can call it.
        mock_ctypes.WINFUNCTYPE.return_value = lambda func: func
        user32.EnumWindows.side_effect = self._enum_invoking(0xABC)

        def fake_classname(hwnd, buf, size):
            buf.value = "CabinetWClass"

        def fake_title(hwnd, buf, size):
            buf.value = "RPI-RP2 (T:)"

        user32.GetClassNameW.side_effect = fake_classname
        user32.GetWindowTextW.side_effect = fake_title
        # create_unicode_buffer returns an object with a settable .value
        mock_ctypes.create_unicode_buffer.side_effect = \
            lambda n: MagicMock(value="")

        with patch.dict(sys.modules, {"ctypes": mock_ctypes}):
            explorer.close_explorer_for_drive("T:\\")

        self.assertTrue(user32.ShowWindow.called)
        self.assertTrue(user32.PostMessageW.called)

    def test_non_explorer_window_is_left_alone(self):
        mock_ctypes = _mock_ctypes()
        user32 = mock_ctypes.windll.user32
        mock_ctypes.WINFUNCTYPE.return_value = lambda func: func
        user32.EnumWindows.side_effect = self._enum_invoking(0xDEF)
        user32.GetClassNameW.side_effect = \
            lambda h, b, s: setattr(b, "value", "NotCabinet")
        mock_ctypes.create_unicode_buffer.side_effect = \
            lambda n: MagicMock(value="")

        with patch.dict(sys.modules, {"ctypes": mock_ctypes}):
            explorer.close_explorer_for_drive("T:\\")

        user32.ShowWindow.assert_not_called()
        user32.PostMessageW.assert_not_called()

    def test_exception_is_swallowed(self):
        mock_ctypes = _mock_ctypes()
        mock_ctypes.windll.user32.EnumWindows.side_effect = OSError("nope")
        with patch.dict(sys.modules, {"ctypes": mock_ctypes}), \
                patch.object(explorer.logging, "debug") as mock_debug:
            explorer.close_explorer_for_drive("T:\\")  # must not raise
        mock_debug.assert_called_once()


if __name__ == "__main__":
    unittest.main()
