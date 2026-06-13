"""Tests for com2tty.wsl.servers.base (leftover-listener reclamation)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


from com2tty.wsl.servers.base import (
    kill_leftover_listener,
)


class TestKillLeftoverListener(unittest.TestCase):

    # signal.SIGKILL is absent on Windows (this runs only inside WSL/Linux in
    # production); create it for the tests so the kill path is exercised here.
    @patch("com2tty.wsl.servers.base.signal.SIGKILL", 9, create=True)
    @patch("time.sleep")
    @patch("com2tty.wsl.servers.base.os.kill")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_kills_only_com2tty_process(self, mock_sp, mock_open, mock_kill,
                                        mock_sleep):
        """A PID whose cmdline references our bridge is killed."""
        mock_sp.return_value = MagicMock(stdout=b" 1234\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"python3\x00bridge.py\x00--symlink"
        kill_leftover_listener(4000)
        mock_kill.assert_called_once()
        self.assertEqual(mock_kill.call_args[0][0], 1234)
        mock_sleep.assert_called_once_with(0.3)

    @patch("com2tty.wsl.servers.base.sys.stderr")
    @patch("time.sleep")
    @patch("com2tty.wsl.servers.base.os.kill")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_spares_unrelated_process(self, mock_sp, mock_open, mock_kill,
                                      mock_sleep, mock_stderr):
        """A PID owned by an unrelated process is NOT killed."""
        mock_sp.return_value = MagicMock(stdout=b" 5678\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"/usr/bin/nginx\x00-g\x00daemon off;"
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()
        mock_sleep.assert_not_called()
        written = "".join(c.args[0] for c in mock_stderr.write.call_args_list)
        self.assertIn("unrelated process", written)

    @patch("time.sleep")
    @patch("com2tty.wsl.servers.base.os.kill")
    @patch("builtins.open", side_effect=FileNotFoundError("no proc"))
    @patch("subprocess.run")
    def test_skips_pid_with_unreadable_cmdline(self, mock_sp, mock_open,
                                               mock_kill, mock_sleep):
        """A PID whose /proc cmdline cannot be read is skipped."""
        mock_sp.return_value = MagicMock(stdout=b"9999\n")
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()

    @patch("com2tty.wsl.servers.base.signal.SIGKILL", 9, create=True)
    @patch("time.sleep")
    @patch("com2tty.wsl.servers.base.os.kill", side_effect=ProcessLookupError())
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_kill_exception_swallowed(self, mock_sp, mock_open, mock_kill,
                                      mock_sleep):
        """os.kill raising (process already gone) is tolerated."""
        mock_sp.return_value = MagicMock(stdout=b"1234\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"bridge.py"
        kill_leftover_listener(4000)  # should not raise

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_pids_no_sleep(self, mock_sp, mock_sleep):
        """Empty fuser output means nothing to kill and no sleep."""
        mock_sp.return_value = MagicMock(stdout=b"")
        kill_leftover_listener(4000)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("com2tty.wsl.servers.base.os.kill")
    @patch("subprocess.run")
    def test_non_numeric_pid_skipped(self, mock_sp, mock_kill, mock_sleep):
        """Non-numeric tokens in fuser output are ignored."""
        mock_sp.return_value = MagicMock(stdout=b"notapid\n")
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()

    @patch("com2tty.wsl.servers.base.sys.stderr")
    @patch("subprocess.run", side_effect=FileNotFoundError("no fuser"))
    def test_fuser_missing_prints_note(self, mock_sp, mock_stderr):
        kill_leftover_listener(4000)
        written = "".join(c.args[0] for c in mock_stderr.write.call_args_list)
        self.assertIn("psmisc", written)

    @patch("subprocess.run", side_effect=Exception("boom"))
    def test_generic_exception_ignored(self, mock_sp):
        kill_leftover_listener(4000)  # should not raise
