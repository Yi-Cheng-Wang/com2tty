"""Tests for com2tty.windows.serial_host (settings, baud detection, hot-plug recovery)."""
import unittest
from unittest.mock import MagicMock, patch
import serial
import sys
import os
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.serial_host import (
    ResetProofSerial,
    _poll_wait,
    detect_board_type,
    get_commstate_baudrate,
    get_serial_settings,
    get_system_baudrate,
    get_usb_serial_number,
)


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



class TestGetSystemBaudrate(unittest.TestCase):

    def setUp(self):
        # These tests exercise the mode.com fallback parser; keep the
        # GetCommState fast path out of the way (and off any real COM port
        # that may exist on the developer machine).
        patcher = patch("com2tty.windows.serial_host.get_commstate_baudrate",
                        return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("subprocess.run")
    def test_commstate_short_circuits_mode_com(self, m):
        with patch("com2tty.windows.serial_host.get_commstate_baudrate",
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
        import com2tty.windows.os_hacks.autoplay
        code = compile(open(com2tty.windows.os_hacks.autoplay.__file__, encoding='utf-8').read(), com2tty.windows.os_hacks.autoplay.__file__, 'exec')
        ns = {'__name__': 'com2tty.windows.os_hacks.autoplay',
              '__package__': 'com2tty.windows.os_hacks'}
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



class TestReopenSerialPort(unittest.TestCase):

    def _events(self):
        return threading.Event()

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_reopen_same_port_first_try(self, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        self.assertTrue(reopen_serial_port(ser, None, self._events()))
        ser.close.assert_called_once()
        ser.open.assert_called_once()

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_close_exception_tolerated(self, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.close.side_effect = Exception("already closed")
        self.assertTrue(reopen_serial_port(ser, None, self._events()))

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_1200_baud_guard(self, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 1200
        reopen_serial_port(ser, None, self._events())
        self.assertEqual(ser.baudrate, 115200)

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_shutdown_aborts(self, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        sd = threading.Event()
        sd.set()
        self.assertFalse(reopen_serial_port(ser, None, sd))
        ser.open.assert_not_called()

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_abort_event_yields(self, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        uf2_evt = threading.Event()
        uf2_evt.set()
        self.assertFalse(reopen_serial_port(ser, None, self._events(),
                                            abort_events=(uf2_evt,)))
        ser.open.assert_not_called()

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("serial.tools.list_ports.comports", return_value=[])
    def test_max_attempts_exhausted(self, mock_comports, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("gone")
        self.assertFalse(reopen_serial_port(ser, "SER1", self._events(),
                                            max_attempts=3))
        self.assertEqual(ser.open.call_count, 3)

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_follows_device_to_new_port(self, mock_comports, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
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

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_scan_skips_non_matching_serial(self, mock_comports, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
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

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("serial.tools.list_ports.comports")
    def test_new_port_open_failure_keeps_trying(self, mock_comports, mock_sleep):
        from com2tty.windows.serial_host import reopen_serial_port
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

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("com2tty.windows.serial_host.snapshot_ports")
    def test_opens_newly_appeared_port(self, mock_snap, mock_sleep):
        from com2tty.windows.serial_host import acquire_new_port
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

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("com2tty.windows.serial_host.snapshot_ports")
    def test_returns_none_and_restores_when_nothing_appears(self, mock_snap,
                                                            mock_sleep):
        from com2tty.windows.serial_host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        mock_snap.return_value = {"COM3"}
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, threading.Event(),
                                           max_attempts=3))
        self.assertEqual(ser.port, "COM3")
        ser.open.assert_not_called()

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("com2tty.windows.serial_host.snapshot_ports")
    def test_open_failure_restores_original_port(self, mock_snap, mock_sleep):
        from com2tty.windows.serial_host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        ser.open.side_effect = Exception("not ready")
        mock_snap.return_value = {"COM3", "COM9"}
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, threading.Event(),
                                           max_attempts=2))
        # Restored after each failed open attempt.
        self.assertEqual(ser.port, "COM3")

    @patch("com2tty.windows.serial_host.time.sleep")
    @patch("com2tty.windows.serial_host.snapshot_ports")
    def test_shutdown_aborts(self, mock_snap, mock_sleep):
        from com2tty.windows.serial_host import acquire_new_port
        ser = MagicMock()
        ser.port = "COM3"
        ser.baudrate = 115200
        sd = threading.Event()
        sd.set()
        self.assertIsNone(acquire_new_port(ser, {"COM3"}, sd))
        ser.open.assert_not_called()

    def test_snapshot_ports(self):
        from com2tty.windows.serial_host import snapshot_ports
        p1 = MagicMock(device="COM3")
        p2 = MagicMock(device="COM9")
        with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
            self.assertEqual(snapshot_ports(), {"COM3", "COM9"})



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



class TestPollWait(unittest.TestCase):

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_falls_back_to_plain_sleep_without_watcher(self, mock_sleep):
        # The conftest stub returns no watcher.
        _poll_wait(0.25)
        mock_sleep.assert_called_once_with(0.25)

    @patch("com2tty.windows.serial_host.time.sleep")
    def test_uses_watcher_when_active(self, mock_sleep):
        import com2tty.windows.serial_host as host_mod
        fake_watcher = MagicMock()
        host_mod.devnotify.get_watcher = lambda: fake_watcher
        _poll_wait(0.25)
        fake_watcher.wait.assert_called_once_with(0.25)
        mock_sleep.assert_not_called()
