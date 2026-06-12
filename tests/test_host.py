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
    get_commstate_baudrate,
    read_wsl_stdout,
    read_com_port,
    read_wsl_stderr,
    run_bridge,
    run_gamepad_bridge,
    run_multi_gamepad_bridge,
    run_with_respawn,
    _poll_wait,
    QueuePipeConnection,
    ResetProofSerial,
    esp32_manual_reset,
    pico_manual_reset,
    detect_board_type,
    AutoplaySuppressor,
    get_usb_serial_number,
    get_drive_by_serial,
    wsl_command,
    check_wsl_environment,
    enable_vt_mode,
    get_banner_colors,
    md5_hexdigest,
    list_removable_drives,
)


# ?? get_wsl_path ?????????????????????????????????????????????????????????

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


# ?? get_serial_settings ??????????????????????????????????????????????????

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


# ?? get_system_baudrate ??????????????????????????????????????????????????

class TestGetSystemBaudrate(unittest.TestCase):

    def setUp(self):
        # These tests exercise the mode.com fallback parser; keep the
        # GetCommState fast path out of the way (and off any real COM port
        # that may exist on the developer machine).
        patcher = patch("com2tty.host.get_commstate_baudrate",
                        return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("subprocess.run")
    def test_commstate_short_circuits_mode_com(self, m):
        with patch("com2tty.host.get_commstate_baudrate",
                   return_value=230400):
            self.assertEqual(get_system_baudrate("COM1"), 230400)
        m.assert_not_called()

    @patch("subprocess.run")
    def test_success(self, m):
        res = MagicMock(returncode=0, stdout="\n  Baud:  115200\n")
        m.return_value = res
        self.assertEqual(get_system_baudrate("COM1"), 115200)

    @patch("subprocess.run")
    def test_prefers_baud_line_over_earlier_number(self, m):
        # A localized layout where another number precedes the baud field;
        # anchoring on "Baud" must still pick the real rate, not the 2024.
        m.return_value = MagicMock(
            returncode=0,
            stdout="Status for device COM1:\n  Date 2024\n  Baud rate: 9600\n")
        self.assertEqual(get_system_baudrate("COM1"), 9600)

    @patch("subprocess.run")
    def test_fallback_first_number_without_baud_keyword(self, m):
        # No "Baud" keyword (fully localized): fall back to first large number.
        m.return_value = MagicMock(returncode=0, stdout="   57600   xyz\n")
        self.assertEqual(get_system_baudrate("COM1"), 57600)

    def test_winreg_import_error(self):
        import com2tty.uf2
        code = compile(open(com2tty.uf2.__file__, encoding='utf-8').read(), com2tty.uf2.__file__, 'exec')
        ns = {'__name__': 'com2tty.uf2'}
        with patch.dict('sys.modules', {'winreg': None}):
            exec(code, ns)
        self.assertIsNone(ns.get('winreg'))

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


# ?? read_wsl_stdout ??????????????????????????????????????????????????????

class TestGetCommstateBaudrate(unittest.TestCase):
    """Win32 GetCommState path, driven through an injected fake kernel32."""

    INVALID_HANDLE = __import__("ctypes").c_void_p(-1).value

    def _kernel32(self, handle=42, baud=115200, ok=1):
        k32 = MagicMock()
        k32.CreateFileW.return_value = handle

        def fake_get_comm_state(h, dcb_ref):
            dcb_ref._obj.BaudRate = baud
            return ok
        k32.GetCommState.side_effect = fake_get_comm_state
        return k32

    def test_reads_baudrate(self):
        k32 = self._kernel32(baud=115200)
        self.assertEqual(get_commstate_baudrate("COM3", _kernel32=k32), 115200)
        k32.CloseHandle.assert_called_once_with(42)
        # The \\.\ device-path prefix is required for COM10 and above.
        self.assertEqual(k32.CreateFileW.call_args[0][0], "\\\\.\\COM3")

    def test_invalid_handle_returns_none(self):
        k32 = self._kernel32(handle=self.INVALID_HANDLE)
        self.assertIsNone(get_commstate_baudrate("COM3", _kernel32=k32))
        k32.CloseHandle.assert_not_called()

    def test_null_handle_returns_none(self):
        k32 = self._kernel32(handle=0)
        self.assertIsNone(get_commstate_baudrate("COM3", _kernel32=k32))

    def test_getcommstate_failure_returns_none(self):
        k32 = self._kernel32(ok=0)
        self.assertIsNone(get_commstate_baudrate("COM3", _kernel32=k32))
        k32.CloseHandle.assert_called_once()

    def test_zero_baudrate_returns_none(self):
        k32 = self._kernel32(baud=0)
        self.assertIsNone(get_commstate_baudrate("COM3", _kernel32=k32))

    def test_exception_returns_none(self):
        k32 = MagicMock()
        k32.CreateFileW.side_effect = Exception("no win32")
        self.assertIsNone(get_commstate_baudrate("COM3", _kernel32=k32))

    def test_real_windll_lookup_is_tolerated(self):
        # Without an injected kernel32 this touches ctypes.windll: absent on
        # POSIX (AttributeError -> None) and an invalid handle for a
        # nonexistent port on Windows -> None either way.
        self.assertIsNone(get_commstate_baudrate("COM2TTYNOSUCHPORT"))


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


# ?? read_com_port ????????????????????????????????????????????????????????

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

    @patch("com2tty.host.time.sleep")
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

    @patch("com2tty.host.time.sleep")
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

    @patch("com2tty.host.time.sleep")
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


# ?? QueuePipeConnection ?????????????????????????????????????????????????

class TestReopenSerialPort(unittest.TestCase):

    def _events(self):
        return threading.Event()

    @patch("com2tty.host.time.sleep")
    def test_reopen_same_port_first_try(self, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        self.assertTrue(reopen_serial_port(ser, None, self._events()))
        ser.close.assert_called_once()
        ser.open.assert_called_once()

    @patch("com2tty.host.time.sleep")
    def test_close_exception_tolerated(self, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.close.side_effect = Exception("already closed")
        self.assertTrue(reopen_serial_port(ser, None, self._events()))

    @patch("com2tty.host.time.sleep")
    def test_1200_baud_guard(self, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 1200
        reopen_serial_port(ser, None, self._events())
        self.assertEqual(ser.baudrate, 115200)

    @patch("com2tty.host.time.sleep")
    def test_shutdown_aborts(self, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        sd = threading.Event()
        sd.set()
        self.assertFalse(reopen_serial_port(ser, None, sd))
        ser.open.assert_not_called()

    @patch("com2tty.host.time.sleep")
    def test_abort_event_yields(self, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        uf2_evt = threading.Event()
        uf2_evt.set()
        self.assertFalse(reopen_serial_port(ser, None, self._events(),
                                            abort_events=(uf2_evt,)))
        ser.open.assert_not_called()

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports", return_value=[])
    def test_max_attempts_exhausted(self, mock_comports, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("gone")
        self.assertFalse(reopen_serial_port(ser, "SER1", self._events(),
                                            max_attempts=3))
        self.assertEqual(ser.open.call_count, 3)

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_follows_device_to_new_port(self, mock_comports, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        calls = [0]
        def fake_open():
            calls[0] += 1
            if calls[0] == 1:
                raise Exception("COM3 gone")
        ser.open.side_effect = fake_open
        new_port = MagicMock()
        new_port.serial_number = "SER1"
        new_port.device = "COM9"
        mock_comports.return_value = [new_port]
        self.assertTrue(reopen_serial_port(ser, "SER1", self._events()))
        self.assertEqual(ser.port, "COM9")

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_scan_skips_non_matching_serial(self, mock_comports, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("gone")
        other = MagicMock()
        other.serial_number = "OTHER"
        other.device = "COM9"
        mock_comports.return_value = [other]
        self.assertFalse(reopen_serial_port(ser, "SER1", self._events(),
                                            max_attempts=2))
        self.assertEqual(ser.port, "COM3")

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_new_port_open_failure_keeps_trying(self, mock_comports, mock_sleep):
        from com2tty.host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("gone")
        new_port = MagicMock()
        new_port.serial_number = "SER1"
        new_port.device = "COM9"
        mock_comports.return_value = [new_port]
        self.assertFalse(reopen_serial_port(ser, "SER1", self._events(),
                                            max_attempts=2))


class TestAcquireNewPort(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.snapshot_ports")
    def test_opens_newly_appeared_port(self, mock_snap, mock_sleep):
        from com2tty.host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 1200
        # First poll: nothing new yet. Second poll: COM9 appeared.
        mock_snap.side_effect = [{"COM3"}, {"COM3", "COM9"}]
        before = {"COM3"}
        self.assertEqual(acquire_new_port(ser, before, threading.Event()),
                         "COM9")
        self.assertEqual(ser.port, "COM9")
        # 1200-baud guard must have reset the rate before opening.
        self.assertEqual(ser.baudrate, 115200)

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.snapshot_ports")
    def test_returns_none_and_restores_when_nothing_appears(self, mock_snap,
                                                            mock_sleep):
        from com2tty.host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        mock_snap.return_value = {"COM3"}
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, threading.Event(),
                                           max_attempts=3))
        self.assertEqual(ser.port, "COM3")
        ser.open.assert_not_called()

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.snapshot_ports")
    def test_open_failure_restores_original_port(self, mock_snap, mock_sleep):
        from com2tty.host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("not ready")
        mock_snap.return_value = {"COM3", "COM9"}
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, threading.Event(),
                                           max_attempts=2))
        # Restored after each failed open attempt.
        self.assertEqual(ser.port, "COM3")

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.snapshot_ports")
    def test_shutdown_aborts(self, mock_snap, mock_sleep):
        from com2tty.host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        sd = threading.Event()
        sd.set()
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, sd))
        ser.open.assert_not_called()

    def test_snapshot_ports(self):
        from com2tty.host import snapshot_ports
        p1 = MagicMock(device="COM3")
        p2 = MagicMock(device="COM9")
        with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
            self.assertEqual(snapshot_ports(), {"COM3", "COM9"})


class TestSamdRfc2217Session(unittest.TestCase):
    """SAMD acquires the re-enumerated bootloader port on connect and
    restores the application port on disconnect."""

    def _run(self, ser, lines, usb_serial=None):
        proc = MagicMock()
        proc.stderr.readline.side_effect = [ln.encode() for ln in lines] + [b""]
        read_wsl_stderr(proc, ser, threading.Event(), threading.Event(),
                        queue.Queue(), threading.Event(), queue.Queue(),
                        usb_serial, "samd")

    @patch("com2tty.host.reopen_serial_port", return_value=True)
    @patch("com2tty.host.acquire_new_port", return_value="COM9")
    @patch("com2tty.host.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.host.samd_touch_reset")
    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    def test_connect_acquires_bootloader_then_disconnect_restores(
            self, mock_sleep, mock_redir, mock_touch, mock_snap,
            mock_acquire, mock_reopen):
        ser = MagicMock()
        ser.port = "COM3"
        ser.get_settings.return_value = {}
        self._run(ser, ["[CONTROL] RFC2217_CONNECT\n",
                        "[CONTROL] RFC2217_DISCONNECT\n"], usb_serial="SER1")

        mock_touch.assert_called_once_with(ser)
        mock_acquire.assert_called_once()
        # Disconnect restored the original application port and reopened it.
        self.assertEqual(ser.port, "COM3")
        mock_reopen.assert_called_once()

    @patch("com2tty.host.reopen_serial_port", return_value=False)
    @patch("com2tty.host.acquire_new_port", return_value=None)
    @patch("com2tty.host.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.host.samd_touch_reset")
    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    def test_connect_fallback_when_bootloader_absent(
            self, mock_sleep, mock_redir, mock_touch, mock_snap,
            mock_acquire, mock_reopen):
        ser = MagicMock()
        ser.port = "COM3"
        ser.get_settings.return_value = {}
        self._run(ser, ["[CONTROL] RFC2217_CONNECT\n",
                        "[CONTROL] RFC2217_DISCONNECT\n"])
        # Connect fell back to reopening the application port...
        ser.open.assert_called()
        # ...and disconnect's reopen returned False (warning path).
        mock_reopen.assert_called_once()

    @patch("com2tty.host.reopen_serial_port", return_value=True)
    @patch("com2tty.host.acquire_new_port", return_value=None)
    @patch("com2tty.host.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.host.samd_touch_reset")
    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    def test_connect_fallback_reopen_exception_tolerated(
            self, mock_sleep, mock_redir, mock_touch, mock_snap,
            mock_acquire, mock_reopen):
        ser = MagicMock()
        ser.port = "COM3"
        ser.get_settings.return_value = {}
        ser.open.side_effect = Exception("still gone")
        self._run(ser, ["[CONTROL] RFC2217_CONNECT\n",
                        "[CONTROL] RFC2217_DISCONNECT\n"])  # should not raise


class TestReadComPortReconnect(unittest.TestCase):

    @patch("com2tty.host.reopen_serial_port", return_value=True)
    @patch("com2tty.host.time.sleep")
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

    @patch("com2tty.host.reopen_serial_port", return_value=False)
    @patch("com2tty.host.time.sleep")
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

    @patch("com2tty.host.reopen_serial_port")
    @patch("com2tty.host.time.sleep")
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
        """Queue.get times out (Empty) ??except continues ??stop fires."""
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


# ?? ResetProofSerial ?????????????????????????????????????????????????????

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


# ?? esp32_manual_reset ???????????????????????????????????????????????????

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


# ?? pico_manual_reset ???????????????????????????????????????????????????

class TestPicoManualReset(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    def test_success(self, mock_sleep):
        """Lines 249-274: Normal 1200-baud touch sequence."""
        ser = MagicMock()
        ser.baudrate = 115200
        pico_manual_reset(ser)
        # Check DTR/baudrate assignments
        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.5)
        ser.close.assert_called_once()
        # Ensure it got restored
        # PropertyMock isn't used here, but we can verify ser.baudrate was assigned.
        # Since it's a MagicMock, the last set value is available as ser.baudrate.
        self.assertEqual(ser.baudrate, 115200)
        self.assertTrue(ser.dtr)

    @patch("com2tty.host.time.sleep")
    def test_old_baud_is_1200(self, mock_sleep):
        """Line 269: when old_baud == 1200, should use 115200 as fallback."""
        ser = MagicMock()
        ser.baudrate = 1200
        pico_manual_reset(ser)
        ser.close.assert_called_once()

    @patch("com2tty.host.time.sleep")
    def test_close_exception_swallowed(self, mock_sleep):
        """Lines 263-266: ser.close() exception is swallowed."""
        ser = MagicMock()
        ser.baudrate = 115200
        ser.close.side_effect = Exception("close fail")
        pico_manual_reset(ser)  # should not raise

    @patch("com2tty.host.time.sleep")
    def test_general_exception(self, mock_sleep):
        """Lines 273-274: general exception path."""
        ser = MagicMock()
        type(ser).dtr = PropertyMock(side_effect=Exception("hw fail"))
        pico_manual_reset(ser)  # should not raise


# ?? detect_board_type ????????????????????????????????????????????????????

class TestDetectBoardType(unittest.TestCase):

    @patch("serial.tools.list_ports.comports")
    def test_pico(self, mock_comports):
        """Line 281-282: Pico VID 0x2E8A."""
        port = MagicMock()
        port.device = "COM3"
        port.vid = 0x2E8A
        mock_comports.return_value = [port]
        self.assertEqual(detect_board_type("COM3"), "pico")

    @patch("serial.tools.list_ports.comports")
    def test_esp32(self, mock_comports):
        """Lines 283-285: ESP32 VID."""
        port = MagicMock()
        port.device = "COM4"
        port.vid = 0x10C4  # Silicon Labs
        mock_comports.return_value = [port]
        self.assertEqual(detect_board_type("COM4"), "esp32")

    @patch("serial.tools.list_ports.comports")
    def test_esp32_espressif(self, mock_comports):
        """Lines 283-285: Espressif VID 0x303A."""
        port = MagicMock()
        port.device = "COM4"
        port.vid = 0x303A
        mock_comports.return_value = [port]
        self.assertEqual(detect_board_type("COM4"), "esp32")

    @patch("serial.tools.list_ports.comports")
    def test_unknown(self, mock_comports):
        """Line 286: Unknown board type."""
        port = MagicMock()
        port.device = "COM5"
        port.vid = 0xFFFF
        mock_comports.return_value = [port]
        self.assertEqual(detect_board_type("COM5"), "unknown")

    @patch("serial.tools.list_ports.comports")
    def test_port_not_found(self, mock_comports):
        """Line 286: Port not in list."""
        mock_comports.return_value = []
        self.assertEqual(detect_board_type("COM99"), "unknown")


# ?? AutoplaySuppressor ??????????????????????????????????????????????????

class TestAutoplaySuppressor(unittest.TestCase):

    def setUp(self):
        # Keep the marker file out of the real temp dir during these tests.
        import com2tty.uf2 as uf2
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

    @patch("com2tty.uf2.winreg", None)
    def test_enter_no_winreg(self):
        """Line 215-216: winreg is None ??early return."""
        sup = AutoplaySuppressor()
        result = sup.__enter__()
        self.assertIs(result, sup)
        self.assertFalse(sup.modified)

    @patch("com2tty.uf2.winreg", None)
    def test_exit_no_winreg(self):
        """Line 234: winreg is None ??exit does nothing."""
        sup = AutoplaySuppressor()
        sup.__exit__(None, None, None)

    @patch("com2tty.uf2.winreg")
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

    @patch("com2tty.uf2.winreg")
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

    @patch("com2tty.uf2.winreg")
    def test_enter_open_key_exception(self, mock_winreg):
        """Lines 229-230: OpenKey raises exception ??modified stays False."""
        mock_winreg.OpenKey.side_effect = OSError("access denied")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.__enter__()
        self.assertFalse(sup.modified)

    @patch("com2tty.uf2.winreg")
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

    @patch("com2tty.uf2.winreg")
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

    @patch("com2tty.uf2.winreg")
    def test_exit_not_modified(self, mock_winreg):
        """Line 234: modified is False ??exit early."""
        sup = AutoplaySuppressor()
        sup.modified = False
        sup.__exit__(None, None, None)
        mock_winreg.OpenKey.assert_not_called()

    @patch("com2tty.uf2.winreg")
    def test_exit_exception(self, mock_winreg):
        """Lines 243-244: Exit exception is swallowed."""
        mock_winreg.OpenKey.side_effect = OSError("fail")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.modified = True
        sup.__exit__(None, None, None)  # should not raise

    @patch("com2tty.uf2.winreg")
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

    @patch("com2tty.uf2.winreg")
    def test_enter_marker_write_failure_is_tolerated(self, mock_winreg):
        """A failure writing the marker does not abort suppression."""
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.return_value = (0, 1)
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        with patch("com2tty.uf2.open", side_effect=OSError("disk full")):
            sup = AutoplaySuppressor()
            sup.__enter__()
        self.assertTrue(sup.modified)


class TestRestoreOrphanedAutoplay(unittest.TestCase):

    def setUp(self):
        import com2tty.uf2 as uf2
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
        from com2tty.host import restore_orphaned_autoplay
        restore_orphaned_autoplay()  # should not raise

    @patch("com2tty.uf2.winreg")
    def test_restores_existing_value_and_removes_marker(self, mock_winreg):
        from com2tty.host import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2
        mock_winreg.REG_DWORD = 4

        self._write_marker({"existed": True, "original_value": 7})
        restore_orphaned_autoplay()

        mock_winreg.SetValueEx.assert_called_once()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.uf2.winreg")
    def test_restores_deleted_value(self, mock_winreg):
        from com2tty.host import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        self._write_marker({"existed": False, "original_value": 0})
        restore_orphaned_autoplay()

        mock_winreg.DeleteValue.assert_called_once()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.uf2.winreg")
    def test_restore_delete_value_not_found(self, mock_winreg):
        from com2tty.host import restore_orphaned_autoplay
        mock_key = MagicMock()
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.DeleteValue.side_effect = FileNotFoundError()
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        self._write_marker({"existed": False, "original_value": 0})
        restore_orphaned_autoplay()  # should not raise
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.uf2.winreg", None)
    def test_restore_no_winreg(self):
        from com2tty.host import restore_orphaned_autoplay
        self._write_marker({"existed": True, "original_value": 1})
        restore_orphaned_autoplay()
        self.assertFalse(os.path.exists(self._marker))

    @patch("com2tty.uf2.winreg")
    def test_restore_corrupt_marker(self, mock_winreg):
        from com2tty.host import restore_orphaned_autoplay
        with open(self._marker, "w") as f:
            f.write("not json{")
        restore_orphaned_autoplay()  # should not raise
        self.assertFalse(os.path.exists(self._marker))


# ?? get_usb_serial_number ????????????????????????????????????????????????

class TestGetUsbSerialNumber(unittest.TestCase):

    @patch("serial.tools.list_ports.comports")
    def test_found(self, mock_comports):
        """Lines 707-711: SER= found in hwid."""
        port = MagicMock()
        port.device = "COM3"
        port.hwid = "USB VID:PID=2E8A:F00F SER=ABC123 LOCATION=1-6:x.0"
        mock_comports.return_value = [port]
        self.assertEqual(get_usb_serial_number("COM3"), "ABC123")

    @patch("serial.tools.list_ports.comports")
    def test_no_ser(self, mock_comports):
        """Port found but no SER= in hwid."""
        port = MagicMock()
        port.device = "COM3"
        port.hwid = "USB VID:PID=2E8A:F00F LOCATION=1-6:x.0"
        mock_comports.return_value = [port]
        self.assertIsNone(get_usb_serial_number("COM3"))

    @patch("serial.tools.list_ports.comports")
    def test_ser_substring_but_no_token(self, mock_comports):
        """hwid contains 'SER=' only as a substring (XSER=...), so no token
        actually starts with it: the inner scan finds nothing and returns None."""
        port = MagicMock()
        port.device = "COM3"
        port.hwid = "USB VID:PID=2E8A:F00F XSER=ABC LOCATION=1-6"
        mock_comports.return_value = [port]
        self.assertIsNone(get_usb_serial_number("COM3"))

    @patch("serial.tools.list_ports.comports")
    def test_port_not_found(self, mock_comports):
        """Port not in list."""
        mock_comports.return_value = []
        self.assertIsNone(get_usb_serial_number("COM99"))


# ?? get_drive_by_serial ??????????????????????????????????????????????????

class TestGetDriveBySerial(unittest.TestCase):

    @patch("com2tty.host.subprocess.run")
    def test_single_dict(self, mock_run):
        """Lines 738-740: JSON output is a single dict."""
        mock_run.return_value = MagicMock(
            stdout='{"DriveLetter":"E:\\\\","PNPDeviceID":"USB\\\\..."}',
        )
        self.assertEqual(get_drive_by_serial("ABC123"), "E:\\")

    @patch("com2tty.host.subprocess.run")
    def test_list_result(self, mock_run):
        """Lines 741-742: JSON output is a list."""
        mock_run.return_value = MagicMock(
            stdout='[{"DriveLetter":"F:\\\\","PNPDeviceID":"USB\\\\..."}]',
        )
        self.assertEqual(get_drive_by_serial("ABC123"), "F:\\")

    @patch("com2tty.host.subprocess.run")
    def test_empty_output(self, mock_run):
        """Lines 735-736: Empty output returns None."""
        mock_run.return_value = MagicMock(stdout="")
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.host.subprocess.run")
    def test_exception(self, mock_run):
        """Lines 743-745: Exception returns None."""
        mock_run.side_effect = Exception("powershell fail")
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.host.subprocess.run")
    def test_empty_list(self, mock_run):
        """Lines 741-742: JSON list is empty."""
        mock_run.return_value = MagicMock(stdout="[]")
        # An empty list ??len(data) == 0, so the elif is skipped ??returns None at end
        self.assertIsNone(get_drive_by_serial("ABC123"))

    @patch("com2tty.host.subprocess.run")
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


# ?? read_wsl_stderr ??????????????????????????????????????????????????????

class TestReadWslStderr(unittest.TestCase):

    def _run(self, lines, ser=None, usb_serial=None, board_type='unknown'):
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
            line.encode() if isinstance(line, str) else line for line in lines
        ] + [b""]
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()
        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, usb_serial, board_type)
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

        uf2_evt = threading.Event()
        uf2_q = queue.Queue()
        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'esp32')

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
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'esp32')
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
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'esp32')
        self.assertFalse(rfc_evt.is_set())

    def test_rfc2217_disconnect_without_connect_unknown_board(self):
        # DISCONNECT arriving with no prior CONNECT and a non-esp32/pico board:
        # redirector_stop/thread are None and no board reset path runs.
        ser = MagicMock()
        self._run(["[CONTROL] RFC2217_DISCONNECT\n"], ser=ser,
                  board_type='unknown')
        ser.get_settings.assert_not_called()

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
            threading.Event(), queue.Queue(),
            None, 'unknown',
        )

    def test_exception_after_shutdown_is_silent(self):
        # Error raised once shutdown is already set: the debug log is skipped.
        proc = MagicMock()
        sd = threading.Event()

        def boom(*a):
            sd.set()
            raise Exception("fail during shutdown")
        proc.stderr.readline.side_effect = boom
        read_wsl_stderr(
            proc, MagicMock(), sd,
            threading.Event(), queue.Queue(),
            threading.Event(), queue.Queue(),
            None, 'unknown',
        )
        self.assertTrue(sd.is_set())

    def test_uf2_error(self):
        """Line 692: UF2_ERROR control message."""
        self._run(["[CONTROL] UF2_ERROR: something bad\n"])

    def test_uf2_ready(self):
        """Lines 627-629: UF2_READY control message."""
        self._run(["[CONTROL] UF2_READY:5001\n"])

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_upload_success(self, mock_reset, mock_autoplay, mock_open, mock_exists, mock_drive, mock_sleep):
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        import hashlib
        data = b"test"
        md5_hash = hashlib.md5(data).hexdigest()

        proc.stderr.readline.side_effect = [
            f"[CONTROL] UF2_UPLOAD_START:4:{md5_hash}\n".encode(),
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        uf2_q.put(b"test")

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")

        import os
        proc.stdin.write.assert_called_with(b"[CONTROL] UF2_ACK\n")
        mock_reset.assert_called_once_with(ser)
        mock_open.assert_called_once_with(os.path.join("T:\\", "flash.uf2"), "wb")
        mock_open.return_value.__enter__.return_value.write.assert_called_once_with(bytearray(b"test"))
        self.assertFalse(uf2_evt.is_set())

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_upload_md5_mismatch(self, mock_reset, mock_autoplay, mock_open, mock_exists, mock_drive, mock_sleep):
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4:wrong_md5\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        uf2_q.put(b"test")

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")

        proc.stdin.write.assert_called_with(b"[CONTROL] UF2_ACK\n")
        mock_open.assert_not_called()

    @patch("com2tty.host.time.sleep")
    def test_uf2_upload_invalid_size(self, mock_sleep):
        """Lines 363-365: ValueError on invalid size string."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:not_a_number\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        # Should not crash; the ValueError is caught

    @patch("com2tty.host.time.sleep")
    def test_uf2_upload_ack_failure(self, mock_sleep):
        """Lines 374-377: proc.stdin.write raises ??return early."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stdin.write.side_effect = Exception("pipe broken")
        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        self.assertFalse(uf2_evt.is_set())

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    @patch("com2tty.host.os.path.exists", return_value=False)
    @patch("com2tty.host.get_drive_by_serial", return_value=None)
    def test_uf2_upload_timeout_waiting_for_data(self, mock_drive, mock_exists, mock_reset, mock_autoplay, mock_sleep):
        """Lines 387-391: Timeout waiting for UF2 data (10 consecutive timeouts)."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:100\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        # Queue is empty, so get(timeout=1.0) will raise queue.Empty each time
        # After 11 timeouts, it breaks

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "unknown")

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value=None)
    @patch("com2tty.host.os.path.exists", return_value=False)
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_flash_no_drive_found(self, mock_reset, mock_autoplay, mock_exists, mock_drive, mock_sleep):
        """Line 546: No valid RP2 drive found."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        # Should log error about no drive found

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=OSError("write fail"))
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_flash_write_exception(self, mock_reset, mock_autoplay, mock_open, mock_exists, mock_drive, mock_sleep):
        """Line 539: write exception during flash."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        # Should log error about flash failure

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value=None)
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_flash_fallback_scan(self, mock_reset, mock_autoplay, mock_drive, mock_sleep):
        """Lines 512-516, 521: Fallback scan for INFO_UF2.TXT when no serial match."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        # Simulate: first call for "T:\" exists ??True, then INFO_UF2.TXT exists ??True
        exists_calls = [0]
        def fake_exists(path):
            exists_calls[0] += 1
            # Return False for all to simulate no drive found ??exercises the sleep(0.5) on line 521
            return False
        
        with patch("com2tty.host.os.path.exists", side_effect=fake_exists):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        mock_sleep.assert_any_call(0.5)

    @patch("com2tty.host.os.name", "posix")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value=None)
    @patch("com2tty.host.list_removable_drives", return_value=["A:\\", "T:\\"])
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    def test_uf2_flash_fallback_scan_skips_drive_without_marker(
            self, mock_reset, mock_autoplay, mock_open, mock_drives,
            mock_drive, mock_sleep):
        """Two removable drives: the first lacks INFO_UF2.TXT so the scan loops
        past it and flashes the second (covers the drive-scan loop-back)."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        # Only the second drive (T:) carries the BOOTSEL marker.
        def fake_exists(path):
            return path == os.path.join("T:\\", "INFO_UF2.TXT")

        with patch("com2tty.host.os.path.exists", side_effect=fake_exists):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")

        mock_open.assert_called_once_with(os.path.join("T:\\", "flash.uf2"), "wb")

    @patch("com2tty.host.Redirector")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.pico_manual_reset")
    def test_rfc2217_disconnect_pico(self, mock_pico_reset, mock_sleep, mock_redir_cls):
        """Lines 349-350: _stop_rfc2217_session with board_type=='pico' calls pico_manual_reset."""
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
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'pico')
        mock_pico_reset.assert_called_once_with(ser)

    @patch("com2tty.host.time.sleep")
    def test_settings_permission_error_retry(self, mock_sleep):
        """Lines 595-609: PermissionError during settings ??close/reopen/retry."""
        proc = MagicMock()
        class MockSerial:
            def __init__(self):
                self.port = "COM3"
                self.bytesize = serial.EIGHTBITS
                self.parity = serial.PARITY_NONE
                self.stopbits = serial.STOPBITS_ONE
                self._baudrate = 9600
                self.set_count = 0
                self.close = MagicMock()
                self.open = MagicMock()
            
            @property
            def baudrate(self):
                return self._baudrate
                
            @baudrate.setter
            def baudrate(self, val):
                self.set_count += 1
                if self.set_count == 1:
                    raise PermissionError("port busy")
                self._baudrate = val
                
        ser = MockSerial()

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')
        mock_sleep.assert_any_call(1.0)

    @patch("com2tty.host.time.sleep")
    def test_settings_permission_error_all_retries_fail(self, mock_sleep):
        """Lines 595-609: All retries fail ??final error logged."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"

        type(ser).baudrate = PropertyMock(
            fget=lambda self: 9600,
            fset=MagicMock(side_effect=PermissionError("port busy"))
        )

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')

    @patch("com2tty.host.time.sleep")
    def test_settings_permission_error_reopen_fails(self, mock_sleep):
        """Lines 603-607: ser.open() fails during reopen."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"

        attempt = [0]
        def baudrate_setter(val):
            attempt[0] += 1
            if attempt[0] <= 2:
                raise PermissionError("port busy")

        type(ser).baudrate = PropertyMock(
            fget=lambda self: 9600,
            fset=baudrate_setter
        )
        ser.open.side_effect = Exception("cannot reopen")

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_reopen(self, mock_comports, mock_sleep):
        """Lines 628-688: UF2_UPLOAD_END pico reopen logic."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        # ser.close() succeeds, ser.open() succeeds on first try
        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'pico')
        ser.close.assert_called()
        ser.open.assert_called()

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_close_exception(self, mock_comports, mock_sleep):
        """Lines 646-647: ser.close() raises exception ??swallowed."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.close.side_effect = Exception("already closed")
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'pico')

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_baudrate_1200_guard(self, mock_comports, mock_sleep):
        """Line 651: baudrate == 1200 ??set to 115200."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 1200
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'pico')
        # After the guard, baudrate should have been set to 115200
        # (This is checked by getattr(ser, 'baudrate', 115200) == 1200)

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_port_not_reappear(self, mock_comports, mock_sleep):
        """Line 687: Port did not reappear after 30s."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("port gone")
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        mock_comports.return_value = []  # No ports found during scan

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'pico')

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_scan_skips_non_matching_port(self, mock_comports, mock_sleep):
        """The USB-serial rescan must skip ports whose serial does not match,
        looping past them (covers the non-match branch of the rescan loop)."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("port gone")
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        other = MagicMock()
        other.serial_number = "SOMETHING_ELSE"
        other.device = "COM9"
        mock_comports.return_value = [other]  # present but never matches

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "SER123", 'pico')

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_usb_serial_scan_fallback(self, mock_comports, mock_sleep):
        """Lines 666-684: USB serial scan fallback for port change."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        # ser.open() fails for original port, but succeeds for new port
        open_count = [0]
        def fake_open():
            open_count[0] += 1
            if open_count[0] <= 1:
                raise Exception("port gone")
            # Second call succeeds (for COM5)
        ser.open.side_effect = fake_open

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        # Mock a port with matching serial number but different device name
        new_port = MagicMock()
        new_port.serial_number = "SER123"
        new_port.device = "COM5"
        mock_comports.return_value = [new_port]

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "SER123", 'pico')
        # Should have changed port and opened successfully

    @patch("com2tty.host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_uf2_upload_end_pico_usb_serial_scan_open_fails(self, mock_comports, mock_sleep):
        """Lines 681-682: USB serial scan finds new port but open fails."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("port gone")

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        new_port = MagicMock()
        new_port.serial_number = "SER123"
        new_port.device = "COM5"
        mock_comports.return_value = [new_port]

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "SER123", 'pico')
        # All open attempts fail ??"port did not reappear" error

    @patch("com2tty.host.time.sleep")
    def test_uf2_upload_end_non_pico(self, mock_sleep):
        """Lines 635-688: UF2_UPLOAD_END with non-pico board ??just clear event."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_evt.set()  # Simulate it was set during upload
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'esp32')
        self.assertFalse(uf2_evt.is_set())

    @patch("com2tty.host.time.sleep")
    def test_settings_close_exception_during_reopen(self, mock_sleep):
        """Line 600: ser.close() raises during reopen attempt ??pass."""
        proc = MagicMock()
        ser = MagicMock()
        ser.port = "COM3"
        
        attempt = [0]
        def baudrate_setter(val):
            attempt[0] += 1
            if attempt[0] <= 2:
                raise PermissionError("port busy")

        type(ser).baudrate = PropertyMock(
            fget=lambda self: 9600,
            fset=baudrate_setter
        )
        ser.close.side_effect = Exception("already closed")

        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')


# ?? _window_closer and _close_explorer_for_drive ????????????????????????

class TestWindowCloserAndExplorer(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    @patch("com2tty.host.os.name", "nt")
    def test_window_closer_on_nt(self, mock_reset, mock_autoplay, mock_open,
                                  mock_exists, mock_drive, mock_sleep):
        """Lines 427-447: _window_closer runs on Windows (os.name=='nt')."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        # Mock ctypes entirely to avoid importing it while os.name is patched to 'nt'
        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32.EnumWindows.side_effect = lambda cb, _: None

        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")

    @patch("com2tty.host.os.name", "posix")
    def test_close_explorer_non_windows(self):
        """Line 469: Non-Windows ??early return."""
        # We call _close_explorer_for_drive indirectly through _flash_uf2
        # But since it's a nested function, we test it via the full path
        # The simplest test: on non-nt, just verify no ctypes import
        # This is already covered when os.name != 'nt'
        pass

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    @patch("com2tty.host.pico_manual_reset")
    @patch("com2tty.host.os.name", "posix")
    @patch("com2tty.host.list_removable_drives", return_value=["T:\\"])
    def test_window_closer_skipped_non_nt(self, mock_drives, mock_reset,
                                           mock_autoplay, mock_open,
                                           mock_exists, mock_drive, mock_sleep):
        """Lines 410, 468-469: Not Windows ??_window_closer and _close_explorer skipped."""
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] UF2_UPLOAD_START:4\n",
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")


# ?? run_bridge ???????????????????????????????????????????????????????????

class TestRunBridge(unittest.TestCase):

    @patch("com2tty.host.get_system_baudrate", return_value=115200)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.host.check_wsl_environment")
    def test_auto_baud(self, mock_check, mock_pr, mock_sl, mock_thr, mock_ex,
                        mock_pop, mock_ser, mock_wsl, mock_baud):
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
    @patch("com2tty.host.check_wsl_environment")
    def test_auto_baud_fallback(self, mock_check, mock_pr, mock_sl, mock_thr,
                                 mock_ex, mock_pop, mock_ser, mock_wsl,
                                 mock_baud):
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
    @patch("com2tty.host.check_wsl_environment")
    def test_keyboard_interrupt(self, mock_check, mock_pr, mock_sl, mock_ex,
                                 mock_pop, mock_ser, mock_wsl):
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
    @patch("com2tty.host.check_wsl_environment")
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

    @patch("com2tty.host.get_usb_serial_number", return_value=None)
    @patch("com2tty.host.detect_board_type", return_value="unknown")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.host.check_wsl_environment")
    def test_usb_serial_warning(self, mock_check, mock_pr, mock_sl, mock_thr,
                                 mock_ex, mock_pop, mock_ser, mock_wsl,
                                 mock_detect, mock_usb_serial):
        """Line 813: usb_serial is None ??warning logged."""
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False,
                   False, 4000)


# ?? winreg import fallback ??????????????????????????????????????????????



class TestHostEdgeCases(unittest.TestCase):

    @patch("com2tty.host.os.name", "nt")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    def test_uf2_upload_window_closer_and_explorer(self, mock_autoplay, mock_open, mock_exists, mock_drive, mock_sleep):
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        import hashlib
        data = b"test"
        md5_hash = hashlib.md5(data).hexdigest()

        proc.stderr.readline.side_effect = [
            f"[CONTROL] UF2_UPLOAD_START:4:{md5_hash}\n".encode(),
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        # Mock fileno so fsync succeeds and covers line 539
        mock_open.return_value.__enter__.return_value.fileno.return_value = 1

        import sys
        mock_ctypes = MagicMock()
        sys.modules['ctypes'] = mock_ctypes

        mock_user32 = mock_ctypes.windll.user32
        
        # This makes ctypes.WINFUNCTYPE(...)(func) return func directly
        mock_ctypes.WINFUNCTYPE.return_value = lambda func: func

        def fake_enum_windows(cb, param):
            # Now cb is the actual Python function enum_windows_proc
            cb(123, 0)
            cb(456, 0)
            cb(789, 0)
            return True

        mock_user32.EnumWindows.side_effect = fake_enum_windows

        def fake_get_class_name(hwnd, buf, size):
            buf.value = "CabinetWClass"

        mock_user32.GetClassNameW.side_effect = fake_get_class_name

        def fake_get_window_text(hwnd, buf, size):
            if hwnd == 123:
                buf.value = "RPI-RP2"
            elif hwnd == 456:
                buf.value = "USB Drive (T:)"
            else:
                buf.value = "Other"

        mock_user32.GetWindowTextW.side_effect = fake_get_window_text

        try:
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")
            
            self.assertTrue(mock_user32.ShowWindow.call_count >= 1)
            self.assertTrue(mock_user32.PostMessageW.call_count >= 1)
        finally:
            sys.modules.pop('ctypes', None)

    @patch("com2tty.host.os.name", "nt")
    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.get_drive_by_serial", return_value="T:\\\\")
    @patch("com2tty.host.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.host.AutoplaySuppressor")
    def test_uf2_upload_window_closer_exception(self, mock_autoplay, mock_open, mock_exists, mock_drive, mock_sleep):
        proc = MagicMock()
        ser = MagicMock()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        import hashlib
        data = b"test"
        md5_hash = hashlib.md5(data).hexdigest()

        proc.stderr.readline.side_effect = [
            f"[CONTROL] UF2_UPLOAD_START:4:{md5_hash}\n".encode(),
            b"[CONTROL] UF2_UPLOAD_END\n",
            b""
        ]
        uf2_q.put(b"test")

        import sys
        mock_ctypes = MagicMock()
        sys.modules['ctypes'] = mock_ctypes

        mock_user32 = mock_ctypes.windll.user32
        mock_user32.EnumWindows.side_effect = Exception("enum error")
        
        try:
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")
        finally:
            sys.modules.pop('ctypes', None)

    @patch("com2tty.host.time.sleep")
    def test_settings_permission_error_close_open_exceptions(self, mock_sleep):
        proc = MagicMock()
        
        class MockSerialCloseOpenFail:
            def __init__(self):
                self.port = "COM3"
                self.bytesize = serial.EIGHTBITS
                self.parity = serial.PARITY_NONE
                self.stopbits = serial.STOPBITS_ONE
                self._baudrate = 9600
                self.close = MagicMock(side_effect=Exception("close fail"))
                self.open = MagicMock(side_effect=Exception("open fail"))
                self.set_count = 0
            @property
            def baudrate(self):
                return self._baudrate
            @baudrate.setter
            def baudrate(self, val):
                self.set_count += 1
                if self.set_count == 1:
                    raise PermissionError("port busy")
                self._baudrate = val
                
        ser = MockSerialCloseOpenFail()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')
        ser.close.assert_called()
        ser.open.assert_called()

    @patch("com2tty.host.time.sleep")
    def test_settings_permission_error_all_retries_fail_609(self, mock_sleep):
        proc = MagicMock()
        
        class MockSerialAllFail:
            def __init__(self):
                self.port = "COM3"
                self.bytesize = serial.EIGHTBITS
                self.parity = serial.PARITY_NONE
                self.stopbits = serial.STOPBITS_ONE
                self._baudrate = 9600
                self.close = MagicMock()
                self.open = MagicMock()
            @property
            def baudrate(self):
                return self._baudrate
            @baudrate.setter
            def baudrate(self, val):
                raise PermissionError("always busy")
                
        ser = MockSerialAllFail()
        sd = threading.Event()
        rfc_evt = threading.Event()
        q = queue.Queue()
        uf2_evt = threading.Event()
        uf2_q = queue.Queue()

        proc.stderr.readline.side_effect = [
            b"[CONTROL] SETTINGS: baud=115200\n",
            b""
        ]

        read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, 'unknown')
        self.assertEqual(ser.close.call_count, 2)

    @patch("com2tty.host.get_system_baudrate", return_value=115200)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.host.get_usb_serial_number", return_value=None)
    @patch("com2tty.host.check_wsl_environment")
    def test_run_bridge_no_usb_serial_813(self, mock_check, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        # Force the thread loop to terminate by returning 0
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False, 4000)
        mock_usb.assert_called_with("COM1")

    @patch("com2tty.host.get_system_baudrate", return_value=115200)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.host.get_usb_serial_number", return_value="123456")
    @patch("com2tty.host.check_wsl_environment")
    def test_run_bridge_success_813(self, mock_check, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False, 4000)
        mock_usb.assert_called_with("COM1")


# == multi-port bridging ====================================================

class TestDeriveIndexedPath(unittest.TestCase):

    def test_index_zero_is_base(self):
        from com2tty.host import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 0),
                         "/tmp/ttyUSB0")

    def test_trailing_number_incremented(self):
        from com2tty.host import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 1),
                         "/tmp/ttyUSB1")
        self.assertEqual(_derive_indexed_path("/tmp/ttyUSB0", 2),
                         "/tmp/ttyUSB2")

    def test_no_trailing_number_appends_index(self):
        from com2tty.host import _derive_indexed_path
        self.assertEqual(_derive_indexed_path("/tmp/serial", 1),
                         "/tmp/serial1")


class TestRunMultiBridge(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_bridge")
    def test_spawns_one_bridge_per_port(self, mock_run, mock_sleep):
        from com2tty.host import run_multi_bridge
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

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_bridge", side_effect=Exception("open failed"))
    def test_bridge_failure_is_logged_not_raised(self, mock_run, mock_sleep):
        from com2tty.host import run_multi_bridge
        run_multi_bridge(["COM3", "COM5"], "auto", "/tmp/ttyUSB0", 8, "N", 1,
                         False, False, False, 4000)  # should not raise
        self.assertEqual(mock_run.call_count, 2)

    @patch("com2tty.host.time.sleep", side_effect=KeyboardInterrupt())
    @patch("com2tty.host.run_bridge")
    def test_keyboard_interrupt_sets_stop_event(self, mock_run, mock_sleep):
        from com2tty.host import run_multi_bridge

        def block_until_stopped(**kwargs):
            kwargs["stop_event"].wait(timeout=5.0)
        mock_run.side_effect = block_until_stopped

        run_multi_bridge(["COM3", "COM5"], "auto", "/tmp/ttyUSB0", 8, "N", 1,
                         False, False, False, 4000)
        # Both bridges were released by the shared stop event (no timeout).
        for c in mock_run.call_args_list:
            self.assertTrue(c.kwargs["stop_event"].is_set())


class TestRunBridgeSecondary(unittest.TestCase):

    @patch("com2tty.host.get_system_baudrate", return_value=115200)
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    @patch("com2tty.host.get_usb_serial_number", return_value=None)
    @patch("com2tty.host.check_wsl_environment")
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
        proc.terminate.assert_called()  # poll() None -> terminated in finally
        # Secondary banner replaces the env-var warning.
        printed = "\n".join(str(c.args[0]) for c in mock_pr.call_args_list
                            if c.args)
        self.assertIn("Secondary bridge", printed)
        self.assertNotIn("Environment variables injected", printed)


# == run_gamepad_bridge =====================================================

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
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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

        proc.terminate.assert_called()  # poll None in finally -> terminate

    @patch("builtins.print")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
    def test_drain_forwards_rumble_to_xinput(self, mock_check, mock_src_cls,
                                             mock_pop, mock_ex, mock_wsl,
                                             mock_pr):
        from com2tty.pad_bridge import pack_rumble
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
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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

        proc.terminate.assert_called()

    @patch("builtins.print")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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

        proc.terminate.assert_called()

    # --- mocked threads: deterministic main-loop branches ---

    @patch("builtins.print")
    @patch("com2tty.host.time.sleep", side_effect=KeyboardInterrupt())
    @patch("threading.Thread")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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
        proc.terminate.assert_called()

    @patch("builtins.print")
    @patch("com2tty.host.time.sleep")
    @patch("threading.Thread")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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
    @patch("com2tty.host.time.sleep")
    @patch("threading.Thread")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
    def test_subprocess_exited_skips_terminate(self, mock_check, mock_src_cls,
                                               mock_pop, mock_ex, mock_wsl,
                                               mock_thr, mock_sleep, mock_pr):
        mock_src_cls.return_value = self._fake_src()
        proc = self._fake_proc(poll=1)  # already exited
        mock_pop.return_value = proc

        run_gamepad_bridge(pad_index=0)

        proc.terminate.assert_not_called()

    @patch("builtins.print")
    @patch("com2tty.host.time.time", return_value=100.0)
    @patch("com2tty.host.time.sleep", side_effect=[None, KeyboardInterrupt()])
    @patch("threading.Thread")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py")
    @patch("os.path.exists", return_value=True)
    @patch("subprocess.Popen")
    @patch("com2tty.xinput.GamepadSource")
    @patch("com2tty.host.check_wsl_environment")
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
    @patch("com2tty.xinput.GamepadSource")
    def test_pad_script_missing_raises(self, mock_src_cls, mock_ex):
        mock_src_cls.return_value = self._fake_src()
        with self.assertRaises(FileNotFoundError):
            run_gamepad_bridge(pad_index=0)


# == wsl_command ============================================================

class TestWslCommand(unittest.TestCase):

    def test_no_distro(self):
        self.assertEqual(
            wsl_command(None, "python3", "-u", "/mnt/c/x.py"),
            ["wsl", "--exec", "python3", "-u", "/mnt/c/x.py"],
        )

    def test_with_distro(self):
        self.assertEqual(
            wsl_command("Ubuntu-22.04", "wslpath", "-u", r"C:\x"),
            ["wsl", "-d", "Ubuntu-22.04", "--exec", "wslpath", "-u", r"C:\x"],
        )

    def test_path_with_spaces_stays_single_argument(self):
        cmd = wsl_command(None, "python3", "-u", "/mnt/c/Program Files/x.py")
        self.assertIn("/mnt/c/Program Files/x.py", cmd)


# == check_wsl_environment ==================================================

class TestCheckWslEnvironment(unittest.TestCase):

    @patch("com2tty.host.shutil.which", return_value=None)
    def test_wsl_missing(self, mock_which):
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("wsl.exe", str(ctx.exception))

    @patch("com2tty.host.subprocess.run", side_effect=OSError("cannot start"))
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_distro_start_failure(self, mock_which, mock_run):
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment(distro="Ubuntu")
        self.assertIn("Ubuntu", str(ctx.exception))

    @patch("com2tty.host.subprocess.run")
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_python3_missing(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="python3: command not found", stdout="")
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("python3", str(ctx.exception))

    @patch("com2tty.host.subprocess.run")
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_python3_missing_no_output(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=127, stderr="", stdout="")
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("no output", str(ctx.exception))

    @patch("com2tty.host.subprocess.run")
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_script_not_readable(self, mock_which, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment("/mnt/c/x/bridge.py")
        self.assertIn("automount", str(ctx.exception))

    @patch("com2tty.host.subprocess.run")
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_success_with_script(self, mock_which, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]
        check_wsl_environment("/mnt/c/x/bridge.py")  # should not raise
        self.assertEqual(mock_run.call_count, 2)

    @patch("com2tty.host.subprocess.run")
    @patch("com2tty.host.shutil.which", return_value="C:\\wsl.exe")
    def test_success_without_script(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        check_wsl_environment()  # should not raise
        self.assertEqual(mock_run.call_count, 1)


# == banner colors / VT mode ================================================

class TestBannerColors(unittest.TestCase):

    @patch.dict("os.environ", {"NO_COLOR": "1"})
    def test_no_color_env(self):
        self.assertEqual(get_banner_colors(), ("", "", "", ""))

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.banner.sys.stdout")
    def test_not_a_tty(self, mock_stdout):
        mock_stdout.isatty.return_value = False
        self.assertEqual(get_banner_colors(), ("", "", "", ""))

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.banner.os.name", "posix")
    @patch("com2tty.banner.sys.stdout")
    def test_posix_tty_colored(self, mock_stdout):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors()[0], "\033[93m")

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.banner.enable_vt_mode", return_value=True)
    @patch("com2tty.banner.os.name", "nt")
    @patch("com2tty.banner.sys.stdout")
    def test_windows_vt_enabled(self, mock_stdout, mock_vt):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors()[3], "\033[0m")
        mock_vt.assert_called_once()

    @patch.dict("os.environ", {"NO_COLOR": ""})
    @patch("com2tty.banner.enable_vt_mode", return_value=False)
    @patch("com2tty.banner.os.name", "nt")
    @patch("com2tty.banner.sys.stdout")
    def test_windows_vt_unavailable(self, mock_stdout, mock_vt):
        mock_stdout.isatty.return_value = True
        self.assertEqual(get_banner_colors(), ("", "", "", ""))


class TestEnableVtMode(unittest.TestCase):

    def test_success(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 1
        m.windll.kernel32.SetConsoleMode.return_value = 1
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertTrue(enable_vt_mode())

    def test_get_console_mode_fails(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 0
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())

    def test_set_console_mode_fails(self):
        m = MagicMock()
        m.windll.kernel32.GetConsoleMode.return_value = 1
        m.windll.kernel32.SetConsoleMode.return_value = 0
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())

    def test_exception_returns_false(self):
        m = MagicMock()
        m.windll.kernel32.GetStdHandle.side_effect = OSError("no console")
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertFalse(enable_vt_mode())


# == list_removable_drives ==================================================

class TestListRemovableDrives(unittest.TestCase):

    def test_filters_removable(self):
        m = MagicMock()
        m.windll.kernel32.GetDriveTypeW.side_effect = (
            lambda root: 2 if root == "E:\\" else 3)
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertEqual(list_removable_drives(), ["E:\\"])

    @patch("com2tty.host.os.path.exists",
           side_effect=lambda p: p == "F:\\")
    def test_fallback_on_error(self, mock_exists):
        m = MagicMock()
        m.windll.kernel32.GetDriveTypeW.side_effect = OSError("boom")
        with patch.dict("sys.modules", {"ctypes": m}):
            self.assertEqual(list_removable_drives(), ["F:\\"])


# == md5_hexdigest ==========================================================

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


# == bind-failure hints in read_wsl_stderr ==================================

class TestBindFailureHints(unittest.TestCase):

    def _run(self, lines):
        proc = MagicMock()
        ser = MagicMock()
        proc.stderr.readline.side_effect = [
            line.encode() for line in lines] + [b""]
        read_wsl_stderr(proc, ser, threading.Event(), threading.Event(),
                        queue.Queue(), threading.Event(), queue.Queue(),
                        None, "unknown")

    def test_rfc2217_bind_failed_hint(self):
        self._run(["[CONTROL] RFC2217_ERROR: bind failed: in use\n"])

    def test_uf2_bind_failed_hint(self):
        self._run(["[CONTROL] UF2_ERROR: bind failed on port 4001: in use\n"])


# == run_bridge board override / distro =====================================

class TestRunBridgeOverrides(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.host.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.host.check_wsl_environment"),
            patch("com2tty.host.detect_board_type", return_value="esp32"),
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


# == run_bridge --wait ======================================================

class TestRunBridgeWait(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.host.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.host.check_wsl_environment"),
            patch("com2tty.host.snapshot_ports"),
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


# == _poll_wait =============================================================

class TestPollWait(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    def test_falls_back_to_plain_sleep_without_watcher(self, mock_sleep):
        # The conftest stub returns no watcher.
        _poll_wait(0.25)
        mock_sleep.assert_called_once_with(0.25)

    @patch("com2tty.host.time.sleep")
    def test_uses_watcher_when_active(self, mock_sleep):
        import com2tty.host as host_mod
        fake_watcher = MagicMock()
        host_mod.devnotify.get_watcher = lambda: fake_watcher
        _poll_wait(0.25)
        fake_watcher.wait.assert_called_once_with(0.25)
        mock_sleep.assert_not_called()


# == run_bridge exit reasons ================================================

class TestRunBridgeExitReasons(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.host.time.sleep"),
            patch("threading.Thread"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("serial.Serial"),
            patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py"),
            patch("com2tty.host.check_wsl_environment"),
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


# == run_with_respawn =======================================================

class TestRunWithRespawn(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.check_wsl_environment")
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

    @patch("com2tty.host.check_wsl_environment")
    def test_stop_event_set_during_session_ends_loop(self, mock_check):
        stop = threading.Event()

        def fake_target(stop_event=None, **kw):
            stop_event.set()
            return "wsl-exited"

        self.assertEqual(run_with_respawn(fake_target, stop_event=stop),
                         "stop")
        mock_check.assert_not_called()

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.check_wsl_environment")
    def test_waits_until_wsl_answers_again(self, mock_check, mock_sleep):
        mock_check.side_effect = [RuntimeError("wsl is down"), None]
        target = MagicMock(side_effect=["shutdown", "stop"])
        self.assertEqual(run_with_respawn(target, distro="Ubuntu"), "stop")
        self.assertEqual(mock_check.call_count, 2)
        mock_check.assert_called_with(None, "Ubuntu")
        mock_sleep.assert_called_once_with(2.0)

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.check_wsl_environment",
           side_effect=RuntimeError("down"))
    def test_stop_event_aborts_wsl_wait(self, mock_check, mock_sleep):
        stop = threading.Event()
        mock_sleep.side_effect = lambda *_: stop.set()
        target = MagicMock(return_value="wsl-exited")
        self.assertEqual(run_with_respawn(target, stop_event=stop), "stop")
        target.assert_called_once()


# == run_multi_gamepad_bridge ===============================================

class TestRunMultiGamepadBridge(unittest.TestCase):

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_gamepad_bridge")
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

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_with_respawn")
    def test_auto_respawn_wraps_each_bridge(self, mock_resp, mock_sleep):
        run_multi_gamepad_bridge([0, 1], auto_respawn=True)
        self.assertEqual(mock_resp.call_count, 2)
        self.assertIs(mock_resp.call_args[0][0], run_gamepad_bridge)

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_gamepad_bridge", side_effect=Exception("boom"))
    def test_bridge_exception_is_logged_not_raised(self, mock_pad,
                                                   mock_sleep):
        run_multi_gamepad_bridge([0])  # must not raise

    @patch("com2tty.host.run_gamepad_bridge")
    def test_keyboard_interrupt_stops_all_bridges(self, mock_pad):
        captured = {}

        def fake_pad(stop_event=None, **kw):
            captured["evt"] = stop_event
            stop_event.wait(5)

        mock_pad.side_effect = fake_pad
        with patch("com2tty.host.time.sleep",
                   side_effect=KeyboardInterrupt()):
            run_multi_gamepad_bridge([0])
        self.assertTrue(captured["evt"].is_set())


# == run_multi_bridge auto-respawn wiring ===================================

class TestRunMultiBridgeAutoRespawn(unittest.TestCase):

    def _kwargs(self):
        return dict(baud=9600, wsl_tty="/tmp/t", bytesize=8, parity="N",
                    stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False,
                    rfc2217_port=4000)

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_with_respawn")
    def test_auto_respawn_uses_wrapper(self, mock_resp, mock_sleep):
        from com2tty.host import run_multi_bridge
        run_multi_bridge(ports=["COM1"], auto_respawn=True, **self._kwargs())
        mock_resp.assert_called_once()
        self.assertIs(mock_resp.call_args[0][0], run_bridge)
        self.assertEqual(mock_resp.call_args[1]["port"], "COM1")

    @patch("com2tty.host.time.sleep")
    @patch("com2tty.host.run_with_respawn")
    @patch("com2tty.host.run_bridge")
    def test_default_does_not_use_wrapper(self, mock_run, mock_resp,
                                          mock_sleep):
        from com2tty.host import run_multi_bridge
        run_multi_bridge(ports=["COM1"], **self._kwargs())
        mock_run.assert_called_once()
        mock_resp.assert_not_called()


# == run_gamepad_bridge stop_event / exit reasons ===========================

class TestRunGamepadBridgeReasons(unittest.TestCase):

    def _patches(self):
        return [
            patch("builtins.print"),
            patch("com2tty.host.time.sleep"),
            patch("threading.Thread"),
            patch("com2tty.host.get_wsl_path", return_value="/wsl/pad.py"),
            patch("os.path.exists", return_value=True),
            patch("subprocess.Popen"),
            patch("com2tty.xinput.GamepadSource"),
            patch("com2tty.host.check_wsl_environment"),
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

