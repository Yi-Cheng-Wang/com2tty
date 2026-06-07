import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import serial
import sys
import os
import threading
import queue
import time as _time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.host import (
    get_wsl_path,
    get_serial_settings,
    get_system_baudrate,
    read_wsl_stdout,
    read_com_port,
    read_wsl_stderr,
    run_bridge,
    QueuePipeConnection,
    ResetProofSerial,
    esp32_manual_reset,
)


# ── get_wsl_path ─────────────────────────────────────────────────────────

class TestGetWslPath(unittest.TestCase):

    def test_success(self):
        with patch("subprocess.run") as m:
            res = MagicMock()
            res.stdout = "/mnt/d/success\n"
            m.return_value = res
            self.assertEqual(get_wsl_path(r"D:\success"), "/mnt/d/success")

    def test_fallback(self):
        with patch("subprocess.run", side_effect=Exception("missing")):
            path = get_wsl_path(r"C:\Users\u\file.py")
            self.assertEqual(path, "/mnt/c/Users/u/file.py")


# ── get_serial_settings ──────────────────────────────────────────────────

class TestSerialSettings(unittest.TestCase):

    def test_8n1(self):
        bs, p, sb = get_serial_settings(8, "N", 1)
        self.assertEqual(bs, serial.EIGHTBITS)
        self.assertEqual(p, serial.PARITY_NONE)
        self.assertEqual(sb, serial.STOPBITS_ONE)

    def test_7e2(self):
        bs, p, sb = get_serial_settings(7, "E", 2)
        self.assertEqual(bs, serial.SEVENBITS)
        self.assertEqual(p, serial.PARITY_EVEN)
        self.assertEqual(sb, serial.STOPBITS_TWO)

    def test_6o1_5(self):
        bs, p, sb = get_serial_settings(6, "O", 1.5)
        self.assertEqual(bs, serial.SIXBITS)
        self.assertEqual(p, serial.PARITY_ODD)
        self.assertEqual(sb, serial.STOPBITS_ONE_POINT_FIVE)

    def test_5_space_mark(self):
        bs, p, _ = get_serial_settings(5, "S", 1)
        self.assertEqual(bs, serial.FIVEBITS)
        self.assertEqual(p, serial.PARITY_SPACE)
        _, p2, _ = get_serial_settings(8, "M", 1)
        self.assertEqual(p2, serial.PARITY_MARK)

    def test_defaults_for_unknown(self):
        bs, p, sb = get_serial_settings(99, "Z", 7)
        self.assertEqual(bs, serial.EIGHTBITS)
        self.assertEqual(p, serial.PARITY_NONE)
        self.assertEqual(sb, serial.STOPBITS_ONE)


# ── get_system_baudrate ──────────────────────────────────────────────────

class TestGetSystemBaudrate(unittest.TestCase):

    @patch("subprocess.run")
    def test_success(self, m):
        res = MagicMock(returncode=0, stdout="\n  Baud:  115200\n")
        m.return_value = res
        self.assertEqual(get_system_baudrate("COM1"), 115200)

    @patch("subprocess.run")
    def test_no_number(self, m):
        m.return_value = MagicMock(returncode=0, stdout="No data")
        self.assertIsNone(get_system_baudrate("COM1"))

    @patch("subprocess.run")
    def test_failure(self, m):
        m.return_value = MagicMock(returncode=1)
        self.assertIsNone(get_system_baudrate("COM1"))

    @patch("subprocess.run", side_effect=Exception("no mode.com"))
    def test_exception(self, m):
        self.assertIsNone(get_system_baudrate("COM1"))


# ── read_wsl_stdout ──────────────────────────────────────────────────────

class TestReadWslStdout(unittest.TestCase):

    def test_normal_writes_to_com(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        proc.stdout.read.side_effect = [b"hi", b""]

        read_wsl_stdout(proc, ser, sd, rfc_evt, q)

        ser.write.assert_called_with(b"hi")
        self.assertTrue(sd.is_set())

    def test_rfc2217_routes_to_queue(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        rfc_evt.set()
        q = queue.Queue()
        proc.stdout.read.side_effect = [b"data", b""]

        read_wsl_stdout(proc, ser, sd, rfc_evt, q)

        ser.write.assert_not_called()
        self.assertEqual(q.get_nowait(), b"data")

    def test_exception(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        proc.stdout.read.side_effect = Exception("fail")

        read_wsl_stdout(proc, ser, sd, threading.Event(), queue.Queue())
        self.assertTrue(sd.is_set())


# ── read_com_port ────────────────────────────────────────────────────────

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

        read_com_port(ser, proc, sd, rfc_evt)
        proc.stdin.write.assert_called_with(b"com")

    @patch("com2tty.host.time.sleep")
    def test_pauses_during_rfc2217(self, mock_sleep):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        rfc_evt.set()

        call = [0]
        def fake_read(*a):
            call[0] += 1
            if call[0] <= 2:
                # First 2 reads: rfc2217 active → sleep path
                return b""
            sd.set()
            return b""

        ser.read.side_effect = fake_read

        # After 2 pauses, clear rfc2217 so loop can read and exit
        def clear_on_third(*a):
            if mock_sleep.call_count >= 2:
                rfc_evt.clear()
        mock_sleep.side_effect = clear_on_third

        read_com_port(ser, proc, sd, rfc_evt)
        mock_sleep.assert_called()

    def test_exception(self):
        proc, ser = MagicMock(), MagicMock()
        sd = threading.Event()
        ser.read.side_effect = Exception("err")

        read_com_port(ser, proc, sd, threading.Event())
        self.assertTrue(sd.is_set())


# ── QueuePipeConnection ─────────────────────────────────────────────────

class TestQueuePipeConnection(unittest.TestCase):

    def test_recv_returns_data(self):
        proc = MagicMock()
        q = queue.Queue()
        stop = threading.Event()
        qpc = QueuePipeConnection(proc, q, stop)
        q.put(b"payload")
        self.assertEqual(qpc.recv(1024), b"payload")

    def test_recv_returns_empty_on_stop(self):
        proc = MagicMock()
        q = queue.Queue()
        stop = threading.Event()
        stop.set()
        qpc = QueuePipeConnection(proc, q, stop)
        self.assertEqual(qpc.recv(1024), b"")

    def test_recv_timeout_then_stop(self):
        """Queue.get times out (Empty) → except continues → stop fires."""
        proc = MagicMock()
        q = queue.Queue()  # empty queue
        stop = threading.Event()
        qpc = QueuePipeConnection(proc, q, stop)

        # Set stop after a short delay so recv loops once through the timeout
        def _set_stop():
            _time.sleep(0.3)
            stop.set()
        t = threading.Thread(target=_set_stop, daemon=True)
        t.start()

        result = qpc.recv(1024)
        self.assertEqual(result, b"")
        t.join(timeout=1)

    def test_sendall(self):
        proc = MagicMock()
        qpc = QueuePipeConnection(proc, queue.Queue(), threading.Event())
        qpc.sendall(b"data")
        proc.stdin.write.assert_called_with(b"data")

    def test_sendall_exception(self):
        proc = MagicMock()
        proc.stdin.write.side_effect = Exception("broken")
        qpc = QueuePipeConnection(proc, queue.Queue(), threading.Event())
        qpc.sendall(b"x")  # should not raise

    def test_close(self):
        stop = threading.Event()
        qpc = QueuePipeConnection(MagicMock(), queue.Queue(), stop)
        qpc.close()
        self.assertTrue(stop.is_set())


# ── ResetProofSerial ─────────────────────────────────────────────────────

class TestResetProofSerial(unittest.TestCase):

    def test_dtr_rts_blocked(self):
        ser = MagicMock()
        original_dtr = ser.dtr
        original_rts = ser.rts
        rps = ResetProofSerial(ser)
        rps.dtr = True
        rps.rts = True
        # Real serial's dtr/rts should remain unchanged
        self.assertEqual(ser.dtr, original_dtr)
        self.assertEqual(ser.rts, original_rts)
        # Cached values readable
        self.assertTrue(rps.dtr)
        self.assertTrue(rps.rts)

    def test_other_attrs_forwarded(self):
        ser = MagicMock()
        ser.baudrate = 9600
        rps = ResetProofSerial(ser)
        self.assertEqual(rps.baudrate, 9600)
        rps.baudrate = 115200
        self.assertEqual(ser.baudrate, 115200)

    def test_private_attrs(self):
        ser = MagicMock()
        rps = ResetProofSerial(ser)
        rps._custom = "ok"
        self.assertEqual(rps._custom, "ok")


# ── esp32_manual_reset ───────────────────────────────────────────────────

class TestEsp32ManualReset(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    def test_success(self, mock_sleep):
        ser = MagicMock()
        esp32_manual_reset(ser)
        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.05)

    @patch("com2tty.host.time.sleep")
    def test_exception(self, mock_sleep):
        ser = MagicMock()
        type(ser).dtr = PropertyMock(side_effect=Exception("err"))
        esp32_manual_reset(ser)  # should not raise


# ── read_wsl_stderr ──────────────────────────────────────────────────────

class TestReadWslStderr(unittest.TestCase):

    def _run(self, lines, ser=None):
        """Helper: feed lines through read_wsl_stderr and return mocks."""
        proc = MagicMock()
        if ser is None:
            ser = MagicMock()
            ser.baudrate = 9600
            ser.bytesize = serial.EIGHTBITS
            ser.parity = serial.PARITY_NONE
            ser.stopbits = serial.STOPBITS_ONE
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        proc.stderr.readline.side_effect = [
            l.encode() if isinstance(l, str) else l for l in lines
        ] + [b""]
        read_wsl_stderr(proc, ser, sd, rfc_evt, q)
        return ser, rfc_evt

    def test_settings_change(self):
        ser, _ = self._run([
            "[CONTROL] SETTINGS: baud=115200 bytesize=7 parity=E stopbits=2\n"
        ])
        self.assertEqual(ser.baudrate, 115200)
        self.assertEqual(ser.bytesize, serial.SEVENBITS)
        self.assertEqual(ser.parity, serial.PARITY_EVEN)
        self.assertEqual(ser.stopbits, serial.STOPBITS_TWO)

    def test_settings_no_change(self):
        ser = MagicMock()
        ser.baudrate = 115200
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        self._run(
            ["[CONTROL] SETTINGS: baud=115200 bytesize=8 parity=N stopbits=1\n"],
            ser=ser,
        )
        self.assertEqual(ser.baudrate, 115200)

    def test_settings_baud_none(self):
        self._run(
            ["[CONTROL] SETTINGS: baud=None bytesize=8 parity=N stopbits=1\n"]
        )

    def test_settings_parse_error(self):
        self._run(["[CONTROL] SETTINGS: garbage!!!\n"])  # should not raise

    def test_rfc2217_ready(self):
        self._run(["[CONTROL] RFC2217_READY:4000\n"])

    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.esp32_manual_reset")
    def test_rfc2217_connect_disconnect(self, mock_reset, mock_sleep,
                                         mock_redir_cls):
        proc = MagicMock()
        ser = MagicMock()
        ser.get_settings.return_value = {"baudrate": 9600}
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] RFC2217_CONNECT\n",
            b"[CONTROL] RFC2217_DISCONNECT\n",
            b"",
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q)

        mock_reset.assert_called_once_with(ser)
        mock_redir_cls.assert_called_once()
        self.assertFalse(rfc_evt.is_set())

    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.esp32_manual_reset")
    def test_rfc2217_redirector_exception(self, mock_reset, mock_sleep,
                                           mock_redir_cls):
        mock_redir = MagicMock()
        mock_redir.shortcircuit.side_effect = Exception("redir err")
        mock_redir_cls.return_value = mock_redir

        proc = MagicMock()
        ser = MagicMock()
        ser.get_settings.return_value = {}
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] RFC2217_CONNECT\n",
            b"[CONTROL] RFC2217_DISCONNECT\n",
            b"",
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q)
        mock_redir.stop.assert_called()

    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.esp32_manual_reset")
    def test_disconnect_reset_exception(self, mock_reset, mock_sleep,
                                         mock_redir_cls):
        proc = MagicMock()
        ser = MagicMock()
        ser.get_settings.return_value = {}
        type(ser).rts = PropertyMock(side_effect=Exception("rts err"))
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] RFC2217_CONNECT\n",
            b"[CONTROL] RFC2217_DISCONNECT\n",
            b"",
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q)
        self.assertFalse(rfc_evt.is_set())

    def test_rfc2217_error(self):
        self._run(["[CONTROL] RFC2217_ERROR: something\n"])

    def test_normal_log(self):
        self._run(["some normal log message\n"])

    def test_empty_line_skipped(self):
        self._run(["\n"])

    def test_exception(self):
        proc = MagicMock()
        proc.stderr.readline.side_effect = Exception("fail")
        read_wsl_stderr(
            proc, MagicMock(), threading.Event(),
            threading.Event(), queue.Queue(),
        )


# ── run_bridge ───────────────────────────────────────────────────────────

class TestRunBridge(unittest.TestCase):

    @patch("com2tty.host.get_system_baudrate", return_value=115200)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    def test_auto_baud(self, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop,
                        mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        self.assertEqual(mock_ser.call_args[1]["baudrate"], 115200)

    @patch("com2tty.host.get_system_baudrate", return_value=None)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    def test_auto_baud_fallback(self, mock_pr, mock_sl, mock_thr, mock_ex,
                                 mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        self.assertEqual(mock_ser.call_args[1]["baudrate"], 9600)

    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    def test_keyboard_interrupt(self, mock_pr, mock_sl, mock_ex, mock_pop,
                                 mock_ser, mock_wsl):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_pop.return_value = proc
        mock_sl.side_effect = KeyboardInterrupt()

        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)

        mock_ser.return_value.close.assert_called()
        proc.terminate.assert_called()

    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    def test_cleanup_exceptions(self, mock_pr, mock_sl, mock_ex, mock_pop,
                                 mock_ser, mock_wsl):
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


if __name__ == "__main__":
    unittest.main()
