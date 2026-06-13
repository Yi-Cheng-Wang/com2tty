"""Tests for com2tty.wsl.liveness (heartbeat files and PID markers)."""
import unittest
from unittest.mock import patch
import sys
import os
import tempfile
import threading


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


from com2tty.core.constants import PICOTOOL_OWNER_FILE  # noqa: F401
from com2tty.wsl.integrations.shell_env import _marker_pid, marker_start
from com2tty.wsl.servers.base import (
    _wait_until_clear,
    kill_leftover_listener,
)
from com2tty.wsl.liveness import (
    alive_file_path,
    is_port_session_alive,
    pid_alive,
    read_pid_file,
    remove_alive_files,
    touch_alive_files,
)


class TestSessionLiveness(unittest.TestCase):

    def test_alive_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("com2tty.wsl.liveness.alive_file_path",
                       side_effect=lambda p: os.path.join(d, f"alive_{p}")):
                touch_alive_files([4000, 4001])
                path = os.path.join(d, "alive_4000")
                self.assertTrue(os.path.exists(path))
                with open(path) as f:
                    self.assertEqual(int(f.read()), os.getpid())
                self.assertTrue(is_port_session_alive(4000))
                remove_alive_files([4000, 4001])
                self.assertFalse(os.path.exists(path))
                self.assertFalse(is_port_session_alive(4000))
                remove_alive_files([4000])  # removing again is a no-op

    def test_stale_alive_file_is_not_alive(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("com2tty.wsl.liveness.alive_file_path",
                       side_effect=lambda p: os.path.join(d, f"alive_{p}")):
                touch_alive_files([4000])
                old = __import__("time").time() - 120
                os.utime(os.path.join(d, "alive_4000"), (old, old))
                self.assertFalse(is_port_session_alive(4000))

    def test_touch_failure_is_tolerated(self):
        with patch("com2tty.wsl.liveness.alive_file_path",
                   return_value="/no/such/dir/at/all/alive"):
            touch_alive_files([4000])  # should not raise

    def test_alive_file_path_layout(self):
        self.assertEqual(alive_file_path(4000), "/tmp/com2tty_alive_4000")

    @patch("com2tty.wsl.liveness.os.path.isdir")
    def test_pid_alive_consults_proc(self, mock_isdir):
        mock_isdir.return_value = True
        self.assertTrue(pid_alive(123))
        mock_isdir.assert_called_once_with("/proc/123")
        mock_isdir.return_value = False
        self.assertFalse(pid_alive(123))

    def test_pid_alive_invalid_pid(self):
        self.assertFalse(pid_alive("abc"))
        self.assertFalse(pid_alive(None))

    def test_read_pid_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "owner")
            with open(path, "w") as f:
                f.write(" 123 \n")
            self.assertEqual(read_pid_file(path), 123)
            with open(path, "w") as f:
                f.write("junk")
            self.assertIsNone(read_pid_file(path))
            self.assertIsNone(read_pid_file(os.path.join(d, "missing")))

    def test_marker_pid_parsing(self):
        self.assertEqual(_marker_pid(marker_start(123)), 123)
        self.assertEqual(_marker_pid(marker_start()), os.getpid())
        self.assertIsNone(_marker_pid("# === COM2TTY INJECTION START ==="))
        self.assertIsNone(_marker_pid("# leftover [pid=12"))
        self.assertIsNone(_marker_pid("# === START [pid=xx] ==="))

    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.is_port_session_alive", return_value=True)
    def test_kill_leftover_skips_live_session(self, mock_alive, mock_run):
        # A fresh heartbeat means the port belongs to a *live* session
        # (typically a second invocation with default ports); it must never
        # be killed.
        kill_leftover_listener(4000)
        mock_run.assert_not_called()

    def test_wait_until_clear(self):
        _wait_until_clear(None)  # no event: immediate
        evt = threading.Event()
        _wait_until_clear(evt)  # already clear: immediate
        evt.set()
        with patch("com2tty.wsl.liveness.time.sleep",
                   side_effect=lambda *_: evt.clear()):
            _wait_until_clear(evt, timeout=5)
        self.assertFalse(evt.is_set())

    def test_wait_until_clear_times_out(self):
        evt = threading.Event()
        evt.set()
        with patch("com2tty.wsl.liveness.time.sleep"):
            _wait_until_clear(evt, timeout=0.01)
        self.assertTrue(evt.is_set())  # gave up, event still set
