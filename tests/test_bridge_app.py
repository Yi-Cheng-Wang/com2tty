"""Tests for com2tty.windows.bridge_app (serial bridge session orchestration)."""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
import os
import threading
import queue

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.bridge_app import (
    read_com_port,
    read_wsl_stdout,
    run_bridge,
    run_with_respawn,
)


class TestReadWslStdout(unittest.TestCase):

    def test_com_write_failure_drops_data_and_keeps_running(self):
        # A dead COM handle (device replugging) must not tear the bridge
        # down; the data is dropped and the thread keeps relaying.
        proc, ser = MagicMock(), MagicMock()
        ser.port = "COM3"
        sd = threading.Event()
        proc.stdout.read.side_effect = [b"a", b"b", b"c", b""]
        # Two consecutive failures (warning logged once), then recovery.
        ser.write.side_effect = [OSError("gone"), OSError("gone"), None]

        read_wsl_stdout(proc, ser, sd, threading.Event(), queue.Queue(),
                        threading.Event(), queue.Queue())

        # All three chunks were attempted; the loop survived the failures
        # and only ended at EOF.
        self.assertEqual(ser.write.call_count, 3)
        self.assertTrue(sd.is_set())

    def test_normal_writes_to_com(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        proc.stdout.read.side_effect = [b"hi", b""]

        uf2_evt = threading.Event()
        uf2_q = queue.Queue()
        read_wsl_stdout(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q)

        ser.write.assert_called_with(b"hi")
        self.assertTrue(sd.is_set())

    def test_rfc2217_routes_to_queue(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        rfc_evt.set()
        q = queue.Queue()
        proc.stdout.read.side_effect = [b"data", b""]
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        read_wsl_stdout(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q)

        ser.write.assert_not_called()
        self.assertEqual(q.get_nowait(), b"data")

    def test_uf2_routes_to_uf2_queue(self):
        """Line 81: uf2_active_event.is_set() routes data to uf2_data_queue."""
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_evt.set()
        uf2_q = queue.Queue()
        proc.stdout.read.side_effect = [b"uf2data", b""]

        read_wsl_stdout(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q)

        ser.write.assert_not_called()
        self.assertEqual(uf2_q.get_nowait(), b"uf2data")

    def test_exception(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        proc.stdout.read.side_effect = Exception("fail")

        read_wsl_stdout(proc, ser, sd, threading.Event(), queue.Queue(), threading.Event(), queue.Queue())
        self.assertTrue(sd.is_set())

    def test_exception_after_shutdown_is_silent(self):
        # If the error surfaces because we are already shutting down, the error
        # branch is skipped (covers the "shutdown already set" guard).
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()

        def boom(*a):
            sd.set()
            raise Exception("fail during shutdown")
        proc.stdout.read.side_effect = boom

        read_wsl_stdout(proc, ser, sd, threading.Event(), queue.Queue(),
                        threading.Event(), queue.Queue())
        self.assertTrue(sd.is_set())



class TestReadComPort(unittest.TestCase):

    def test_normal(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()

        def fake_read(*a):
            if not getattr(fake_read, "done", False):
                fake_read.done = True
                return b"com"
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, rfc_evt, threading.Event())
        proc.stdin.write.assert_called_with(b"com")

    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_pauses_during_rfc2217(self, mock_sleep):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        rfc_evt.set()

        call = [0]
        def fake_read(*a):
            call[0] += 1
            if call[0] <= 2:
                # First 2 reads: rfc2217 active ??sleep path
                return b""
            sd.set()
            return b""

        ser.read.side_effect = fake_read

        # After 2 pauses, clear rfc2217 so loop can read and exit
        def clear_on_third(*a):
            if mock_sleep.call_count >= 2:
                rfc_evt.clear()
        mock_sleep.side_effect = clear_on_third

        read_com_port(ser, proc, sd, rfc_evt, threading.Event())
        mock_sleep.assert_called()

    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_permission_error_retry(self, mock_sleep):
        """Lines 110-112: PermissionError/OSError during ser.read ??retry with sleep."""
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        uf2_evt = threading.Event()

        read_count = [0]
        def fake_read(*a):
            read_count[0] += 1
            if read_count[0] == 1:
                raise PermissionError("port busy")
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, rfc_evt, uf2_evt)
        mock_sleep.assert_called_with(0.5)

    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_os_error_retry(self, mock_sleep):
        """Lines 110-112: OSError during ser.read ??retry with sleep."""
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        uf2_evt = threading.Event()

        read_count = [0]
        def fake_read(*a):
            read_count[0] += 1
            if read_count[0] == 1:
                raise OSError("device gone")
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, rfc_evt, uf2_evt)
        mock_sleep.assert_called_with(0.5)

    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_permission_error_during_shutdown(self, mock_sleep):
        """Lines 110-111: PermissionError with shutdown already set skips sleep."""
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        uf2_evt = threading.Event()

        read_count = [0]
        def fake_read(*a):
            read_count[0] += 1
            if read_count[0] == 1:
                sd.set()  # Set shutdown before the PermissionError
                raise PermissionError("port busy")
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, rfc_evt, uf2_evt)
        # sleep should NOT be called since shutdown was set
        mock_sleep.assert_not_called()

    def test_exception(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        ser.read.side_effect = Exception("err")

        read_com_port(ser, proc, sd, threading.Event(), threading.Event())
        self.assertTrue(sd.is_set())

    def test_outer_exception_after_shutdown_is_silent(self):
        # A non-PermissionError/OSError raised while shutdown is already set
        # reaches the outer handler and skips the error log.
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()

        def boom(*a):
            sd.set()
            raise RuntimeError("fail during shutdown")
        ser.read.side_effect = boom

        read_com_port(ser, proc, sd, threading.Event(), threading.Event())
        self.assertTrue(sd.is_set())



class TestReadComPortReconnect(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.reopen_serial_port", return_value=True)
    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_repeated_errors_trigger_reopen(self, mock_sleep, mock_reopen):
        proc, ser = MagicMock(), MagicMock()
        ser.port = "COM3"
        sd = threading.Event()

        count = [0]
        def fake_read(*a):
            count[0] += 1
            if count[0] <= 4:
                raise OSError("device gone")
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, threading.Event(), threading.Event(),
                      usb_serial="SER1")
        mock_reopen.assert_called_once()
        self.assertEqual(mock_reopen.call_args[0][1], "SER1")

    @patch("com2tty.windows.bridge_app.reopen_serial_port", return_value=False)
    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_reopen_failure_keeps_loop_alive(self, mock_sleep, mock_reopen):
        proc, ser = MagicMock(), MagicMock()
        ser.port = "COM3"
        sd = threading.Event()

        count = [0]
        def fake_read(*a):
            count[0] += 1
            if count[0] <= 4:
                raise OSError("device gone")
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, threading.Event(), threading.Event())
        mock_reopen.assert_called_once()

    @patch("com2tty.windows.bridge_app.reopen_serial_port")
    @patch("com2tty.windows.bridge_app.time.sleep")
    def test_successful_read_resets_error_count(self, mock_sleep, mock_reopen):
        proc, ser = MagicMock(), MagicMock()
        ser.port = "COM3"
        ser.in_waiting = 0  # one ser.read call per loop iteration
        sd = threading.Event()

        # 3 errors, one good read, 3 errors: never reaches the threshold.
        sequence = ([OSError("x")] * 3 + [b"ok"] + [OSError("x")] * 3)
        def fake_read(*a):
            if not sequence:
                sd.set()
                return b""
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, threading.Event(), threading.Event())
        mock_reopen.assert_not_called()

    def test_drains_in_waiting_backlog(self):
        # A read that leaves more bytes pending must drain them in the same
        # iteration (the low-latency read path reads what is pending, then
        # whatever arrived during that read).
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()

        waiting = [4, 4, 0]
        type(ser).in_waiting = PropertyMock(side_effect=lambda: waiting[0])

        reads = [b"h", b"ello"]
        def fake_read(*a):
            waiting.pop(0)
            if reads:
                return reads.pop(0)
            sd.set()
            return b""
        ser.read.side_effect = fake_read

        read_com_port(ser, proc, sd, threading.Event(), threading.Event())
        proc.stdin.write.assert_called_with(b"hello")



class TestRunBridge(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.get_system_baudrate", return_value=115200)
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_auto_baud(self, mock_check, mock_pr, mock_sl, mock_thr, mock_ex,
                        mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        self.assertEqual(mock_ser.call_args[1]["baudrate"], 115200)

    @patch("com2tty.windows.bridge_app.get_system_baudrate", return_value=None)
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_auto_baud_fallback(self, mock_check, mock_pr, mock_sl, mock_thr,
                                 mock_ex, mock_pop, mock_ser, mock_wsl,
                                 mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        self.assertEqual(mock_ser.call_args[1]["baudrate"], 9600)

    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_keyboard_interrupt(self, mock_check, mock_pr, mock_sl, mock_ex,
                                 mock_pop, mock_ser, mock_wsl):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_pop.return_value = proc
        mock_sl.side_effect = KeyboardInterrupt()

        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        mock_ser.return_value.close.assert_called()
        proc.wait.assert_called()  # graceful shutdown waits for helper exit

    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_cleanup_exceptions(self, mock_check, mock_pr, mock_sl, mock_ex,
                                 mock_pop, mock_ser, mock_wsl):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = __import__("subprocess").TimeoutExpired(
            cmd="wsl", timeout=3.0
        )
        mock_pop.return_value = proc
        mock_sl.side_effect = KeyboardInterrupt()

        ser_inst = MagicMock()
        ser_inst.close.side_effect = Exception("close err")
        mock_ser.return_value = ser_inst

        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        proc.kill.assert_called()

    @patch("serial.Serial")
    @patch("os.path.exists", return_value=False)
    def test_file_not_found(self, mock_ex, mock_ser):
        with self.assertRaises(FileNotFoundError):
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                       False, 4000)

    @patch("com2tty.windows.bridge_app.get_usb_serial_number", return_value=None)
    @patch("com2tty.windows.bridge_app.detect_board_type", return_value="unknown")
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_usb_serial_warning(self, mock_check, mock_pr, mock_sl, mock_thr,
                                 mock_ex, mock_pop, mock_ser, mock_wsl,
                                 mock_detect, mock_usb_serial):
        """Line 813: usb_serial is None ??warning logged."""
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)



class TestRunBridgeEdgeCases(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.get_system_baudrate", return_value=115200)
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.get_usb_serial_number", return_value=None)
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_run_bridge_no_usb_serial_813(self, mock_check, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        # Force the thread loop to terminate by returning 0
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False, 4000)
        mock_usb.assert_called_with("COM1")

    @patch("com2tty.windows.bridge_app.get_system_baudrate", return_value=115200)
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.get_usb_serial_number", return_value="123456")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_run_bridge_success_813(self, mock_check, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False, 4000)
        mock_usb.assert_called_with("COM1")



class TestDeriveIndexedPath(unittest.TestCase):

    def test_index_zero_is_base(self):
        from com2tty.windows.bridge_app import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 0),
                         "/tmp/ttyUSB0")

    def test_trailing_number_incremented(self):
        from com2tty.windows.bridge_app import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 1),
                         "/tmp/ttyUSB1")
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 2),
                         "/tmp/ttyUSB2")

    def test_no_trailing_number_appends_index(self):
        from com2tty.windows.bridge_app import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/serial", 1),
                         "/tmp/serial1")



class TestRunMultiBridge(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.run_bridge")
    def test_spawns_one_bridge_per_port(self, mock_run, mock_sleep):
        from com2tty.windows.bridge_app import run_multi_bridge
        run_multi_bridge(["COM3", "COM5"], "auto", "/tmp/ttyUSB0", 8, "N", 1,
                         False, False, False, 4000)

        self.assertEqual(mock_run.call_count, 2)
        by_port = {c.kwargs["port"]: c.kwargs
                   for c in mock_run.call_args_list}
        self.assertEqual(by_port["COM3"]["wsl_tty"], "/tmp/ttyUSB0")
        self.assertEqual(by_port["COM3"]["rfc2217_port"], 4000)
        self.assertTrue(by_port["COM3"]["env_setup"])
        self.assertEqual(by_port["COM5"]["wsl_tty"], "/tmp/ttyUSB1")
        self.assertEqual(by_port["COM5"]["rfc2217_port"], 4002)
        self.assertFalse(by_port["COM5"]["env_setup"])
        # All bridges share one stop event.
        self.assertIs(by_port["COM3"]["stop_event"],
                      by_port["COM5"]["stop_event"])

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.run_bridge", side_effect=Exception("open failed"))
    def test_bridge_failure_is_logged_not_raised(self, mock_run, mock_sleep):
        from com2tty.windows.bridge_app import run_multi_bridge
        run_multi_bridge(["COM3", "COM5"], "auto", "/tmp/ttyUSB0", 8, "N", 1,
                         False, False, False, 4000)  # should not raise
        self.assertEqual(mock_run.call_count, 2)

    @patch("com2tty.windows.bridge_app.time.sleep", side_effect=KeyboardInterrupt())
    @patch("com2tty.windows.bridge_app.run_bridge")
    def test_keyboard_interrupt_sets_stop_event(self, mock_run, mock_sleep):
        from com2tty.windows.bridge_app import run_multi_bridge

        def block_until_stopped(**kwargs):
            kwargs["stop_event"].wait(timeout=5.0)
        mock_run.side_effect = block_until_stopped

        run_multi_bridge(["COM3", "COM5"], "auto", "/tmp/ttyUSB0", 8, "N", 1,
                         False, False, False, 4000)
        # Both bridges were released by the shared stop event (no timeout).
        for c in mock_run.call_args_list:
            self.assertTrue(c.kwargs["stop_event"].is_set())



class TestRunBridgeSecondary(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.get_system_baudrate", return_value=115200)
    @patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.windows.bridge_app.get_usb_serial_number", return_value=None)
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_stop_event_and_no_env_setup(self, mock_check, mock_usb, mock_pr,
                                         mock_sl, mock_thr, mock_ex,
                                         mock_pop, mock_ser, mock_wsl,
                                         mock_baud):
        proc = MagicMock()
        proc.poll.return_value = None  # would run forever without stop_event
        mock_pop.return_value = proc

        stop = threading.Event()
        stop.set()
        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False,
                   4002, env_setup=False, stop_event=stop)

        cmd = mock_pop.call_args.args[0]
        self.assertIn("--no-env-setup", cmd)
        proc.wait.assert_called()  # poll() None -> graceful shutdown wait
        # Secondary banner replaces the env-var warning.
        printed = "\n".join(str(c.args[0]) for c in mock_pr.call_args_list
                            if c.args)
        self.assertIn("Secondary bridge", printed)
        self.assertNotIn("Environment variables injected", printed)



class TestRunBridgeOverrides(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.windows.bridge_app.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.windows.bridge_app.check_wsl_environment"),
            patch("com2tty.windows.bridge_app.detect_board_type", return_value="esp32"),
        ]

    def _run(self, **kwargs):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            proc = MagicMock()
            proc.poll.return_value = 0
            mocks[4].return_value = proc  # subprocess.Popen
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                       False, 4000, **kwargs)
            return mocks
        finally:
            for p in patchers:
                p.stop()

    def test_manual_board_skips_detection(self):
        mocks = self._run(board="esp32")
        mocks[8].assert_not_called()  # detect_board_type

    def test_board_none_means_unknown(self):
        mocks = self._run(board="none")
        mocks[8].assert_not_called()

    def test_auto_board_detects(self):
        mocks = self._run(board="auto")
        mocks[8].assert_called_once_with("COM1")

    def test_distro_passed_to_wsl_command(self):
        mocks = self._run(distro="Ubuntu-22.04")
        spawned_cmd = mocks[4].call_args[0][0]
        self.assertEqual(spawned_cmd[:3], ["wsl", "-d", "Ubuntu-22.04"])
        self.assertIn("--exec", spawned_cmd)



class TestRunBridgeWait(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.windows.bridge_app.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.windows.bridge_app.check_wsl_environment"),
            patch("com2tty.windows.bridge_app.snapshot_ports"),
        ]

    def test_wait_polls_until_port_appears(self):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            proc = MagicMock()
            proc.poll.return_value = 0
            mocks[4].return_value = proc
            # Absent on the first two polls, present afterwards.
            mocks[8].side_effect = [set(), set(), {"COM1"}]
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                       False, 4000, wait=True)
            self.assertEqual(mocks[8].call_count, 3)
            mocks[5].assert_called_once()  # serial.Serial opened in the end
        finally:
            for p in patchers:
                p.stop()

    def test_wait_aborts_on_stop_event(self):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            mocks[8].return_value = set()  # port never appears
            stop = threading.Event()
            stop.set()
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                       False, 4000, wait=True, stop_event=stop)
            mocks[5].assert_not_called()  # never reached the serial open
        finally:
            for p in patchers:
                p.stop()

    def test_wait_with_port_present_skips_polling_loop(self):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            proc = MagicMock()
            proc.poll.return_value = 0
            mocks[4].return_value = proc
            mocks[8].return_value = {"COM1"}
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                       False, 4000, wait=True)
            self.assertEqual(mocks[8].call_count, 1)
        finally:
            for p in patchers:
                p.stop()



class TestRunBridgeExitReasons(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.windows.bridge_app.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.windows.bridge_app.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.windows.bridge_app.check_wsl_environment"),
        ]

    def _run(self, proc_poll=0, stop=None, interrupt=False):
        patchers = self._patches()
        mocks = [p.start() for p in patchers]
        try:
            proc = MagicMock()
            proc.poll.return_value = proc_poll
            mocks[4].return_value = proc
            if interrupt:
                mocks[1].side_effect = KeyboardInterrupt()
            return run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False,
                              False, False, 4000, stop_event=stop)
        finally:
            for p in patchers:
                p.stop()

    def test_wsl_exit_reason(self):
        self.assertEqual(self._run(proc_poll=0), "wsl-exited")

    def test_stop_reason(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self._run(proc_poll=None, stop=stop), "stop")

    def test_interrupt_reason(self):
        self.assertEqual(self._run(proc_poll=None, interrupt=True),
                         "interrupt")



class TestRunWithRespawn(unittest.TestCase):

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_respawns_after_wsl_exit_until_interrupt(self, mock_check,
                                                     mock_sleep):
        target = MagicMock(side_effect=["wsl-exited", "interrupt"])
        self.assertEqual(run_with_respawn(target, port="COM1"), "interrupt")
        self.assertEqual(target.call_count, 2)
        mock_check.assert_called_once_with(None, None)

    def test_stop_reason_ends_immediately(self):
        target = MagicMock(return_value="stop")
        self.assertEqual(run_with_respawn(target), "stop")
        target.assert_called_once()

    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_stop_event_set_during_session_ends_loop(self, mock_check):
        stop = threading.Event()

        def fake_target(stop_event=None, **kw):
            stop_event.set()
            return "wsl-exited"

        self.assertEqual(run_with_respawn(fake_target, stop_event=stop),
                         "stop")
        mock_check.assert_not_called()

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.check_wsl_environment")
    def test_waits_until_wsl_answers_again(self, mock_check, mock_sleep):
        mock_check.side_effect = [RuntimeError("wsl is down"), None]
        target = MagicMock(side_effect=["shutdown", "stop"])
        self.assertEqual(run_with_respawn(target, distro="Ubuntu"), "stop")
        self.assertEqual(mock_check.call_count, 2)
        mock_check.assert_called_with(None, "Ubuntu")
        mock_sleep.assert_called_once_with(2.0)

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.check_wsl_environment",
           side_effect=RuntimeError("down"))
    def test_stop_event_aborts_wsl_wait(self, mock_check, mock_sleep):
        stop = threading.Event()
        mock_sleep.side_effect = lambda *_: stop.set()
        target = MagicMock(return_value="wsl-exited")
        self.assertEqual(run_with_respawn(target, stop_event=stop), "stop")
        target.assert_called_once()



class TestRunMultiBridgeAutoRespawn(unittest.TestCase):

    def _kwargs(self):
        return dict(baud=9600, wsl_tty="/tmp/t", bytesize=8, parity="N",
                    stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False,
                    rfc2217_port=4000)

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.run_with_respawn")
    def test_auto_respawn_uses_wrapper(self, mock_resp, mock_sleep):
        from com2tty.windows.bridge_app import run_multi_bridge
        run_multi_bridge(ports=["COM1"], auto_respawn=True, **self._kwargs())
        mock_resp.assert_called_once()
        self.assertIs(mock_resp.call_args[0][0], run_bridge)
        self.assertEqual(mock_resp.call_args[1]["port"], "COM1")

    @patch("com2tty.windows.bridge_app.time.sleep")
    @patch("com2tty.windows.bridge_app.run_with_respawn")
    @patch("com2tty.windows.bridge_app.run_bridge")
    def test_default_does_not_use_wrapper(self, mock_run, mock_resp,
                                          mock_sleep):
        from com2tty.windows.bridge_app import run_multi_bridge
        run_multi_bridge(ports=["COM1"], **self._kwargs())
        mock_run.assert_called_once()
        mock_resp.assert_not_called()
