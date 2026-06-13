"""Tests for com2tty.windows.os_hacks.device_watcher (WM_DEVICECHANGE wake-ups).

The ctypes message pump is exercised against a fake user32 injected by
patching ``ctypes.windll``/``ctypes.WINFUNCTYPE`` (create=True so the
patches also apply on POSIX, where those attributes do not exist), so the
full pump path is covered on every platform without creating real windows.
"""
import ctypes
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import com2tty.windows.os_hacks.device_watcher as dn
from com2tty.windows.os_hacks.device_watcher import (
    DBT_DEVICEARRIVAL,
    DBT_DEVICEREMOVECOMPLETE,
    WM_CLOSE,
    WM_DEVICECHANGE,
    DeviceChangeWatcher,
    get_watcher,
    start_device_watcher,
)


class TestHandleMessage(unittest.TestCase):

    def test_arrival_sets_signal(self):
        w = DeviceChangeWatcher()
        self.assertFalse(w._handle_message(WM_DEVICECHANGE,
                                           DBT_DEVICEARRIVAL))
        self.assertTrue(w._signal.is_set())

    def test_removal_sets_signal(self):
        w = DeviceChangeWatcher()
        w._handle_message(WM_DEVICECHANGE, DBT_DEVICEREMOVECOMPLETE)
        self.assertTrue(w._signal.is_set())

    def test_other_devicechange_ignored(self):
        w = DeviceChangeWatcher()
        w._handle_message(WM_DEVICECHANGE, 0x0007)  # DBT_DEVNODES_CHANGED
        self.assertFalse(w._signal.is_set())

    def test_close_requests_quit(self):
        w = DeviceChangeWatcher()
        self.assertTrue(w._handle_message(WM_CLOSE, 0))
        self.assertFalse(w._signal.is_set())


class TestStart(unittest.TestCase):

    def test_non_windows_is_inactive(self):
        w = DeviceChangeWatcher()
        self.assertFalse(w.start(_os_name="posix"))
        self.assertFalse(w.active)
        self.assertIsNone(w._thread)

    def test_start_waits_for_pump_readiness(self):
        w = DeviceChangeWatcher()

        def fake_pump():
            w.active = True
            w._ready.set()

        with patch.object(w, "_pump", fake_pump):
            self.assertTrue(w.start(_os_name="nt"))
        self.assertTrue(w.active)

    def test_start_reports_pump_failure(self):
        w = DeviceChangeWatcher()

        def fake_pump():
            w._ready.set()  # pump came up but could not register

        with patch.object(w, "_pump", fake_pump):
            self.assertFalse(w.start(_os_name="nt"))


class TestWait(unittest.TestCase):

    @patch("com2tty.windows.os_hacks.device_watcher.time.sleep")
    def test_inactive_falls_back_to_sleep(self, mock_sleep):
        w = DeviceChangeWatcher()
        self.assertFalse(w.wait(0.25))
        mock_sleep.assert_called_once_with(0.25)

    @patch("com2tty.windows.os_hacks.device_watcher.time.sleep")
    def test_active_signal_fires_and_clears(self, mock_sleep):
        w = DeviceChangeWatcher()
        w.active = True
        w._signal.set()
        self.assertTrue(w.wait(0.25))
        self.assertFalse(w._signal.is_set())
        # The post-event grace sleep ran instead of the full interval.
        mock_sleep.assert_called_once_with(dn._POST_EVENT_GRACE)

    def test_active_timeout_returns_false(self):
        w = DeviceChangeWatcher()
        w.active = True
        self.assertFalse(w.wait(0.01))


class TestStop(unittest.TestCase):

    def test_stop_posts_close_and_joins(self):
        w = DeviceChangeWatcher()
        w.active = True
        w._hwnd = 1234
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join()
        w._thread = finished
        fake_windll = MagicMock()
        with patch.object(ctypes, "windll", fake_windll, create=True):
            w.stop()
        fake_windll.user32.PostMessageW.assert_called_once()
        self.assertFalse(w.active)

    def test_stop_tolerates_post_failure(self):
        w = DeviceChangeWatcher()
        w._hwnd = 1234
        fake_windll = MagicMock()
        fake_windll.user32.PostMessageW.side_effect = OSError("gone")
        with patch.object(ctypes, "windll", fake_windll, create=True):
            w.stop()  # must not raise
        self.assertFalse(w.active)

    def test_stop_without_window_or_thread(self):
        w = DeviceChangeWatcher()
        w.stop()
        self.assertFalse(w.active)


class TestPump(unittest.TestCase):
    """Drive _pump against a fake user32 (no real window on any platform)."""

    def _fake_user32(self, hwnd=111, hnotify=1, messages=(0,)):
        user32 = MagicMock()
        user32.CreateWindowExW.return_value = hwnd
        user32.RegisterDeviceNotificationW.return_value = hnotify
        user32.GetMessageW.side_effect = list(messages)
        return user32

    def _run_pump(self, user32):
        w = DeviceChangeWatcher()
        windll = MagicMock()
        windll.user32 = user32
        identity_factory = lambda *a, **kw: (lambda fn: fn)  # noqa: E731
        with patch.object(ctypes, "windll", windll, create=True), \
             patch.object(ctypes, "WINFUNCTYPE", identity_factory,
                          create=True):
            w._pump()
        return w

    def test_successful_pump_lifecycle(self):
        user32 = self._fake_user32(messages=(1, 0))  # one message, then quit
        w = self._run_pump(user32)
        # The pump came up (ready + hwnd) and then exited cleanly.
        self.assertTrue(w._ready.is_set())
        self.assertEqual(w._hwnd, 111)
        self.assertFalse(w.active)  # pump exited -> fallback mode
        user32.TranslateMessage.assert_called_once()
        user32.DispatchMessageW.assert_called_once()
        user32.UnregisterDeviceNotification.assert_called_once()
        user32.DestroyWindow.assert_called_once()

    def test_wndproc_signals_device_change(self):
        user32 = self._fake_user32(messages=(0,))
        w = self._run_pump(user32)
        # The stored window procedure is the raw closure (identity factory).
        w._wndproc(0, WM_DEVICECHANGE, DBT_DEVICEARRIVAL, 0)
        self.assertTrue(w._signal.is_set())
        user32.DefWindowProcW.assert_called_once()

    def test_wndproc_close_posts_quit(self):
        user32 = self._fake_user32(messages=(0,))
        w = self._run_pump(user32)
        self.assertEqual(w._wndproc(0, WM_CLOSE, 0, 0), 0)
        user32.PostQuitMessage.assert_called_once_with(0)

    def test_window_creation_failure_is_inactive(self):
        user32 = self._fake_user32(hwnd=0)
        w = self._run_pump(user32)
        self.assertFalse(w.active)
        self.assertTrue(w._ready.is_set())

    def test_registration_failure_destroys_window(self):
        user32 = self._fake_user32(hnotify=0)
        w = self._run_pump(user32)
        self.assertFalse(w.active)
        user32.DestroyWindow.assert_called_once()


class TestSingleton(unittest.TestCase):

    def setUp(self):
        self._saved = dn._watcher
        dn._watcher = None

    def tearDown(self):
        dn._watcher = self._saved

    def test_start_creates_once_and_reuses(self):
        fake = MagicMock()
        fake.active = True
        with patch("com2tty.windows.os_hacks.device_watcher.DeviceChangeWatcher",
                   return_value=fake) as cls:
            self.assertIs(start_device_watcher(), fake)
            self.assertIs(start_device_watcher(), fake)
            cls.assert_called_once()
        fake.start.assert_called_once()

    def test_inactive_watcher_yields_none(self):
        fake = MagicMock()
        fake.active = False
        with patch("com2tty.windows.os_hacks.device_watcher.DeviceChangeWatcher",
                   return_value=fake):
            self.assertIsNone(start_device_watcher())
        self.assertIsNone(get_watcher())

    def test_start_exception_is_swallowed(self):
        fake = MagicMock()
        fake.active = False
        fake.start.side_effect = RuntimeError("boom")
        with patch("com2tty.windows.os_hacks.device_watcher.DeviceChangeWatcher",
                   return_value=fake):
            self.assertIsNone(start_device_watcher())

    def test_get_watcher_without_start(self):
        self.assertIsNone(get_watcher())


if __name__ == "__main__":
    unittest.main()
