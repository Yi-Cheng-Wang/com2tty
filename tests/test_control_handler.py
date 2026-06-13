"""Tests for com2tty.windows.control_handler (the [CONTROL] stderr protocol handlers)."""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import serial
import sys
import os
import threading
import queue

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.control_handler import read_wsl_stderr


class TestSamdRfc2217Session(unittest.TestCase):
    """SAMD acquires the re-enumerated bootloader port on connect and
    restores the application port on disconnect."""

    def _run(self, ser, lines, usb_serial=None):
        proc = MagicMock()
        proc.stderr.readline.side_effect = [ln.encode() for ln in lines] + [b""]
        read_wsl_stderr(proc, ser, threading.Event(), threading.Event(),
                        queue.Queue(), threading.Event(), queue.Queue(),
                        usb_serial, "samd")

    @patch("com2tty.windows.control_handler.reopen_serial_port", return_value=True)
    @patch("com2tty.windows.control_handler.acquire_new_port", return_value="COM9")
    @patch("com2tty.windows.control_handler.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.windows.control_handler.samd_touch_reset")
    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.reopen_serial_port", return_value=False)
    @patch("com2tty.windows.control_handler.acquire_new_port", return_value=None)
    @patch("com2tty.windows.control_handler.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.windows.control_handler.samd_touch_reset")
    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.reopen_serial_port", return_value=True)
    @patch("com2tty.windows.control_handler.acquire_new_port", return_value=None)
    @patch("com2tty.windows.control_handler.snapshot_ports", return_value={"COM3"})
    @patch("com2tty.windows.control_handler.samd_touch_reset")
    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    def test_connect_fallback_reopen_exception_tolerated(
            self, mock_sleep, mock_redir, mock_touch, mock_snap,
            mock_acquire, mock_reopen):
        ser = MagicMock()
        ser.port = "COM3"
        ser.get_settings.return_value = {}
        ser.open.side_effect = Exception("still gone")
        self._run(ser, ["[CONTROL] RFC2217_CONNECT\n",
                        "[CONTROL] RFC2217_DISCONNECT\n"])  # should not raise



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

    def test_settings_without_payload_is_ignored(self):
        # A bare "[CONTROL] SETTINGS" line carries no payload (no colon), so
        # the handler returns early without touching the port.
        ser = MagicMock()
        ser.baudrate = 9600
        self._run(["[CONTROL] SETTINGS\n"], ser=ser)
        self.assertEqual(ser.baudrate, 9600)

    def test_rfc2217_ready(self):
        self._run(["[CONTROL] RFC2217_READY:4000\n"])

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.esp32_manual_reset")
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

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.esp32_manual_reset")
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

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.esp32_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=False)
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value=None)
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value=None)
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=False)
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=OSError("write fail"))
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value=None)
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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
        
        with patch("com2tty.windows.control_handler.os.path.exists", side_effect=fake_exists):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")
        mock_sleep.assert_any_call(0.5)

    @patch("com2tty.windows.control_handler.os.name", "posix")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value=None)
    @patch("com2tty.windows.control_handler.list_removable_drives", return_value=["A:\\", "T:\\"])
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

        with patch("com2tty.windows.control_handler.os.path.exists", side_effect=fake_exists):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, None, "pico")

        mock_open.assert_called_once_with(os.path.join("T:\\", "flash.uf2"), "wb")

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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



class TestWindowCloserAndExplorer(unittest.TestCase):

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
    @patch("com2tty.windows.control_handler.os.name", "nt")
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

    @patch("com2tty.windows.control_handler.os.name", "posix")
    def test_close_explorer_non_windows(self):
        """Line 469: Non-Windows ??early return."""
        # We call _close_explorer_for_drive indirectly through _flash_uf2
        # But since it's a nested function, we test it via the full path
        # The simplest test: on non-nt, just verify no ctypes import
        # This is already covered when os.name != 'nt'
        pass

    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
    @patch("com2tty.windows.control_handler.os.name", "posix")
    @patch("com2tty.windows.control_handler.list_removable_drives", return_value=["T:\\"])
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



class TestControlHandlerEdgeCases(unittest.TestCase):

    @patch("com2tty.windows.control_handler.os.name", "nt")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
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

        mock_ctypes = MagicMock()

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

        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")

            self.assertTrue(mock_user32.ShowWindow.call_count >= 1)
            self.assertTrue(mock_user32.PostMessageW.call_count >= 1)

    @patch("com2tty.windows.control_handler.os.name", "nt")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.get_drive_by_serial", return_value="T:\\\\")
    @patch("com2tty.windows.control_handler.os.path.exists", return_value=True)
    @patch("builtins.open")
    @patch("com2tty.windows.control_handler.AutoplaySuppressor")
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

        mock_ctypes = MagicMock()
        mock_user32 = mock_ctypes.windll.user32
        mock_user32.EnumWindows.side_effect = Exception("enum error")

        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            read_wsl_stderr(proc, ser, sd, rfc_evt, q, uf2_evt, uf2_q, "123456", "pico")

    @patch("com2tty.windows.control_handler.time.sleep")
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

    @patch("com2tty.windows.control_handler.time.sleep")
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
