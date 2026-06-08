import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call
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
    pico_manual_reset,
    detect_board_type,
    AutoplaySuppressor,
    get_usb_serial_number,
    get_drive_by_serial,
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

    @patch("subprocess.run")
    def test_success(self, m):
        res = MagicMock(returncode=0, stdout="\n  Baud:  115200\n")
        m.return_value = res
        self.assertEqual(get_system_baudrate("COM1"), 115200)

    def test_winreg_import_error(self):
        import com2tty.host
        code = compile(open(com2tty.host.__file__, encoding='utf-8').read(), com2tty.host.__file__, 'exec')
        ns = {'__name__': 'com2tty.host'}
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

class TestReadWslStdout(unittest.TestCase):

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


# ?? QueuePipeConnection ?????????????????????????????????????????????????

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

    def test_init(self):
        """Lines 208-212: __init__ sets attributes."""
        sup = AutoplaySuppressor()
        self.assertIsNone(sup.original_value)
        self.assertFalse(sup.existed)
        self.assertFalse(sup.modified)

    @patch("com2tty.host.winreg", None)
    def test_enter_no_winreg(self):
        """Line 215-216: winreg is None ??early return."""
        sup = AutoplaySuppressor()
        result = sup.__enter__()
        self.assertIs(result, sup)
        self.assertFalse(sup.modified)

    @patch("com2tty.host.winreg", None)
    def test_exit_no_winreg(self):
        """Line 234: winreg is None ??exit does nothing."""
        sup = AutoplaySuppressor()
        sup.__exit__(None, None, None)

    @patch("com2tty.host.winreg")
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

    @patch("com2tty.host.winreg")
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
        result = sup.__enter__()
        self.assertFalse(sup.existed)
        self.assertEqual(sup.original_value, 0)
        self.assertTrue(sup.modified)

    @patch("com2tty.host.winreg")
    def test_enter_open_key_exception(self, mock_winreg):
        """Lines 229-230: OpenKey raises exception ??modified stays False."""
        mock_winreg.OpenKey.side_effect = OSError("access denied")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_READ = 1
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        result = sup.__enter__()
        self.assertFalse(sup.modified)

    @patch("com2tty.host.winreg")
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

    @patch("com2tty.host.winreg")
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

    @patch("com2tty.host.winreg")
    def test_exit_not_modified(self, mock_winreg):
        """Line 234: modified is False ??exit early."""
        sup = AutoplaySuppressor()
        sup.modified = False
        sup.__exit__(None, None, None)
        mock_winreg.OpenKey.assert_not_called()

    @patch("com2tty.host.winreg")
    def test_exit_exception(self, mock_winreg):
        """Lines 243-244: Exit exception is swallowed."""
        mock_winreg.OpenKey.side_effect = OSError("fail")
        mock_winreg.HKEY_CURRENT_USER = "HKCU"
        mock_winreg.KEY_WRITE = 2

        sup = AutoplaySuppressor()
        sup.modified = True
        sup.__exit__(None, None, None)  # should not raise


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
            l.encode() if isinstance(l, str) else l for l in lines
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

        proc.stdin.write.assert_called_with(b"[CONTROL] UF2_ACK\n")
        mock_reset.assert_called_once_with(ser)
        mock_open.assert_called_once_with("T:\\flash.uf2", "wb")
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

        # We need ctypes to be available; mock it at import level
        import ctypes
        mock_enum = MagicMock()
        mock_enum.side_effect = lambda cb, _: None  # Don't actually enumerate

        with patch.dict('sys.modules', {'ctypes': ctypes}):
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
    def test_window_closer_skipped_non_nt(self, mock_reset, mock_autoplay, mock_open,
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

    @patch("com2tty.host.get_usb_serial_number", return_value=None)
    @patch("com2tty.host.detect_board_type", return_value="unknown")
    @patch("com2tty.host.get_wsl_path", return_value="/wsl/bridge.py")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists", return_value=True)
    @patch("threading.Thread")
    @patch("com2tty.host.time.sleep")
    @patch("builtins.print")
    def test_usb_serial_warning(self, mock_pr, mock_sl, mock_thr, mock_ex,
                                 mock_pop, mock_ser, mock_wsl, mock_detect,
                                 mock_usb_serial):
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
    def test_run_bridge_no_usb_serial_813(self, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
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
    def test_run_bridge_success_813(self, mock_usb, mock_pr, mock_sl, mock_thr, mock_ex, mock_pop, mock_ser, mock_wsl, mock_baud):
        proc = MagicMock()
        proc.poll.return_value = 0
        mock_pop.return_value = proc

        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False, 4000)
        mock_usb.assert_called_with("COM1")

