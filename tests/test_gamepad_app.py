"""Tests for com2tty.windows.gamepad_app (XInput gamepad bridge orchestration)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import threading
import time as _time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.gamepad_app import (
    run_gamepad_bridge,
    run_multi_gamepad_bridge,
)


class TestRunGamepadBridge(unittest.TestCase):

    def _fake_proc(self, poll=None):
        proc = MagicMock()
        proc.poll.return_value = poll
        return proc

    def _fake_src(self, changed=True, frame=b"\xab\xcd" + b"\x00" * 14):
        src = MagicMock()
        src.poll.return_value = (changed, frame)
        return src

    # --- real threads: cover read_wsl_logs / drain_wsl_stdout closures ---
    #
    # The daemon threads race against shutdown_event (set in the main finally).
    # To deterministically execute the drain thread's body, the log thread is
    # held open (~40ms) before signalling EOF, keeping shutdown_event clear
    # while the drain thread runs.

    @staticmethod
    def _slow_eof_readline(lines, hold=0.04):
        """Return the given lines in order, then sleep + EOF.

        ``lines`` is a list of byte strings yielded before EOF; the hold keeps
        the main loop alive long enough for the drain thread to run.
        """
        queue_lines = list(lines)
        state = {"i": 0}

        def _readline():
            i = state["i"]
            state["i"] += 1
            if i < len(queue_lines):
                return queue_lines[i]
            _time.sleep(hold)  # keep main loop alive so drain thread runs
            return b""

        return _readline

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_threads_log_and_drain_happy(self, mock_check, mock_src_cls,
                                         mock_pop, mock_ex, mock_wsl, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=None)
        # A real line (logged) and a whitespace-only line (skipped: msg falsy).
        proc.stderr.readline.side_effect = self._slow_eof_readline(
            [b"[WSL] PAD_READY\n", b"   \n"])
        # drain: one chunk (loop continues), then EOF (break) -> covers 819-820
        proc.stdout.read.side_effect = [b"x", b""]
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)
        _time.sleep(0.05)  # let daemon threads finish for coverage

        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_drain_forwards_rumble_to_xinput(self, mock_check, mock_src_cls,
                                             mock_pop, mock_ex, mock_wsl,
                                             mock_pr):
        from com2tty.core.frames import pack_rumble
        src = self._fake_src()
        mock_src_cls.return_value = src
        proc = self._fake_proc(poll=None)
        proc.stderr.readline.side_effect = self._slow_eof_readline([b"hi\n"])
        proc.stdout.read.side_effect = [pack_rumble(0x8000, 0x4000), b""]
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)
        _time.sleep(0.05)  # let the drain thread process the frame

        src.set_rumble.assert_called_with(0x8000, 0x4000)

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_log_thread_exception_path(self, mock_check, mock_src_cls,
                                       mock_pop, mock_ex, mock_wsl, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=None)
        # log: one line, then raise -> covers the except branch (809-811)
        proc.stderr.readline.side_effect = [b"hi\n", Exception("read fail")]
        proc.stdout.read.side_effect = [b""]
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)
        _time.sleep(0.05)

        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_drain_thread_exception_path(self, mock_check, mock_src_cls,
                                         mock_pop, mock_ex, mock_wsl, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=None)
        # Hold the log thread open so the drain thread runs and raises,
        # covering drain's except branch (821-822).
        proc.stderr.readline.side_effect = self._slow_eof_readline([b"hi\n"])
        proc.stdout.read.side_effect = Exception("drain fail")
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)
        _time.sleep(0.05)

        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.time.sleep", side_effect=KeyboardInterrupt())
    @patch("threading.Thread")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_cleanup_does_not_close_read_pipes(self, mock_check, mock_src_cls,
                                               mock_pop, mock_ex, mock_wsl,
                                               mock_thr, mock_sleep, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=None)
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)

        # The read pipes must NOT be closed from the main thread: a daemon
        # reader is blocked inside read() and closing the pipe would deadlock
        # on Windows. terminate_wsl_helper closes stdin and lets the helper
        # exit on EOF instead.
        proc.stdout.close.assert_not_called()
        proc.stderr.close.assert_not_called()
        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    # --- mocked threads: deterministic main-loop branches ---

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.time.sleep", side_effect=KeyboardInterrupt())
    @patch("threading.Thread")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_send_then_keyboard_interrupt_terminates(self, mock_check,
                                                     mock_src_cls,
                                                     mock_pop, mock_ex,
                                                     mock_wsl, mock_thr,
                                                     mock_sleep, mock_pr):
        src = self._fake_src(changed=True)
        mock_src_cls.return_value = src
        proc = self._fake_proc(poll=None)
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)

        proc.stdin.write.assert_called()   # send path executed
        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.time.sleep")
    @patch("threading.Thread")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_broken_pipe_breaks_and_kills(self, mock_check, mock_src_cls,
                                          mock_pop, mock_ex, mock_wsl,
                                          mock_thr, mock_sleep, mock_pr):
        mock_src_cls.return_value = self._fake_src(changed=True)
        proc = self._fake_proc(poll=None)
        proc.stdin.write.side_effect = BrokenPipeError()
        # cleanup: still running -> terminate -> wait times out -> kill
        proc.wait.side_effect = __import__("subprocess").TimeoutExpired(
            cmd="wsl", timeout=3.0)
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)

        proc.kill.assert_called()

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.time.sleep")
    @patch("threading.Thread")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_subprocess_exited_skips_terminate(self, mock_check, mock_src_cls,
                                               mock_pop, mock_ex, mock_wsl,
                                               mock_thr, mock_sleep, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=1)  # already exited
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)

        proc.terminate.assert_not_called()

    @patch("builtins.print")
    @patch("com2tty.windows.gamepad_app.time.time", return_value=100.0)
    @patch("com2tty.windows.gamepad_app.time.sleep", side_effect=[None, KeyboardInterrupt()])
    @patch("threading.Thread")
    @patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    @patch("com2tty.windows.gamepad_app.check_wsl_environment")
    def test_uinput_banner_and_heartbeat_skip(self, mock_check, mock_src_cls,
                                              mock_pop, mock_ex, mock_wsl,
                                              mock_thr, mock_sleep, mock_time,
                                              mock_pr):
        # changed=False so the only send is the first heartbeat; the second
        # iteration (same time) skips sending, exercising the False branch.
        mock_src_cls.return_value = self._fake_src(changed=False)
        proc = self._fake_proc(poll=None)
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0, use_uinput=True)

        self.assertEqual(proc.stdin.write.call_count, 1)  # heartbeat only

    @patch("os.path.exists", return_value=False)
    @patch("com2tty.windows.gamepad_host.GamepadSource")
    def test_pad_script_missing_raises(self, mock_src_cls, mock_ex):
        mock_src_cls.return_value = self._fake_src()
        with self.assertRaises(FileNotFoundError):
            run_gamepad_bridge(pad_index=0)



class TestRunMultiGamepadBridge(unittest.TestCase):

    @patch("com2tty.windows.gamepad_app.time.sleep")
    @patch("com2tty.windows.gamepad_app.run_gamepad_bridge")
    def test_spawns_one_bridge_per_slot_with_distinct_paths(self, mock_pad,
                                                            mock_sleep):
        run_multi_gamepad_bridge([0, 1], poll_hz=100, name="Pad",
                                 use_uinput=True, tmp_path="/tmp/com2pad0",
                                 distro="Ubuntu")
        self.assertEqual(mock_pad.call_count, 2)
        calls = {c.kwargs["pad_index"]: c.kwargs
                 for c in mock_pad.call_args_list}
        self.assertEqual(calls[0]["tmp_path"], "/tmp/com2pad0")
        self.assertEqual(calls[1]["tmp_path"], "/tmp/com2pad1")
        for kwargs in calls.values():
            self.assertEqual(kwargs["poll_hz"], 100)
            self.assertEqual(kwargs["name"], "Pad")
            self.assertTrue(kwargs["use_uinput"])
            self.assertEqual(kwargs["distro"], "Ubuntu")

    @patch("com2tty.windows.gamepad_app.time.sleep")
    @patch("com2tty.windows.gamepad_app.run_with_respawn")
    def test_auto_respawn_wraps_each_bridge(self, mock_resp, mock_sleep):
        run_multi_gamepad_bridge([0, 1], auto_respawn=True)
        self.assertEqual(mock_resp.call_count, 2)
        self.assertIs(mock_resp.call_args[0][0], run_gamepad_bridge)

    @patch("com2tty.windows.gamepad_app.time.sleep")
    @patch("com2tty.windows.gamepad_app.run_gamepad_bridge", side_effect=Exception("boom"))
    def test_bridge_exception_is_logged_not_raised(self, mock_pad,
                                                   mock_sleep):
        run_multi_gamepad_bridge([0])  # must not raise

    @patch("com2tty.windows.gamepad_app.run_gamepad_bridge")
    def test_keyboard_interrupt_stops_all_bridges(self, mock_pad):
        captured = {}

        def fake_pad(stop_event=None, **kw):
            captured["evt"] = stop_event
            stop_event.wait(5)

        mock_pad.side_effect = fake_pad
        with patch("com2tty.windows.gamepad_app.time.sleep",
                   side_effect=KeyboardInterrupt()):
            run_multi_gamepad_bridge([0])
        self.assertTrue(captured["evt"].is_set())



class TestRunGamepadBridgeReasons(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.windows.gamepad_app.time.sleep"),
            patch("threading.Thread"),
            patch("com2tty.windows.gamepad_app.get_wsl_path", return_value="/wsl/pad.py"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("com2tty.windows.gamepad_host.GamepadSource"),
            patch("com2tty.windows.gamepad_app.check_wsl_environment"),
        ]

    def _run(self, proc_poll=None, stop=None, write_error=None):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            src = MagicMock()
            src.poll.return_value = (True, b"\xab\xcd" + b"\x00" * 14)
            mocks[6].return_value = src
            proc = MagicMock()
            proc.poll.return_value = proc_poll
            if write_error is not None:
                proc.stdin.write.side_effect = write_error
            mocks[5].return_value = proc
            return run_gamepad_bridge(pad_index=0, stop_event=stop)
        finally:
            for p in patchers:
                p.stop()

    def test_stop_event_returns_stop(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self._run(proc_poll=None, stop=stop), "stop")

    def test_wsl_exit_returns_wsl_exited(self):
        self.assertEqual(self._run(proc_poll=1), "wsl-exited")

    def test_broken_pipe_returns_wsl_exited(self):
        self.assertEqual(
            self._run(proc_poll=None, write_error=BrokenPipeError()),
            "wsl-exited")
