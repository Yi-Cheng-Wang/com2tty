import unittest
from unittest.mock import MagicMock, patch
import serial
import sys
import os
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.host import (
    get_wsl_path,
    get_serial_settings,
    read_wsl_stdout,
    read_com_port,
    read_wsl_stderr,
    run_bridge
)

class TestCom2TtyHost(unittest.TestCase):
    
    def test_wsl_path_conversion(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("wslpath tool not found")
            path = get_wsl_path(r"C:\Users\Username\some\file.py")
            self.assertEqual(path, "/mnt/c/Users/Username/some/file.py")
            
        with patch("subprocess.run") as mock_run:
            mock_res = MagicMock()
            mock_res.stdout = "/mnt/d/success\n"
            mock_run.return_value = mock_res
            path = get_wsl_path(r"D:\success")
            self.assertEqual(path, "/mnt/d/success")

    def test_serial_settings_mapping(self):
        bs, par, sb = get_serial_settings(8, "N", 1)
        self.assertEqual(bs, serial.EIGHTBITS)
        self.assertEqual(par, serial.PARITY_NONE)
        self.assertEqual(sb, serial.STOPBITS_ONE)
        
        bs, par, sb = get_serial_settings(7, "E", 2)
        self.assertEqual(bs, serial.SEVENBITS)
        self.assertEqual(par, serial.PARITY_EVEN)
        self.assertEqual(sb, serial.STOPBITS_TWO)
        
        bs, par, sb = get_serial_settings(6, "O", 1.5)
        self.assertEqual(bs, serial.SIXBITS)
        self.assertEqual(par, serial.PARITY_ODD)
        self.assertEqual(sb, serial.STOPBITS_ONE_POINT_FIVE)

    @patch("subprocess.run")
    def test_get_system_baudrate(self, mock_run):
        from com2tty.host import get_system_baudrate
        
        # Test success (English and Localized output)
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "\n  Baud:            115200\n"
        mock_run.return_value = mock_res
        self.assertEqual(get_system_baudrate("COM1"), 115200)
        
        # Test no number found
        mock_res.stdout = "No data here"
        self.assertIsNone(get_system_baudrate("COM1"))
        
        # Test command failure
        mock_res.returncode = 1
        self.assertIsNone(get_system_baudrate("COM1"))
        
        # Test exception
        mock_run.side_effect = Exception("missing mode.com")
        self.assertIsNone(get_system_baudrate("COM1"))

    def test_read_wsl_stdout_normal(self):
        proc = MagicMock()
        ser = MagicMock()
        shutdown_event = threading.Event()
        
        proc.stdout.read.side_effect = [b"hello", b""] # Data then EOF
        
        read_wsl_stdout(proc, ser, shutdown_event)
        
        ser.write.assert_called_with(b"hello")
        self.assertTrue(shutdown_event.is_set())

    def test_read_wsl_stdout_exception(self):
        proc = MagicMock()
        ser = MagicMock()
        shutdown_event = threading.Event()
        
        proc.stdout.read.side_effect = Exception("Read Error")
        
        read_wsl_stdout(proc, ser, shutdown_event)
        
        self.assertTrue(shutdown_event.is_set())

    def test_read_com_port_normal(self):
        proc = MagicMock()
        ser = MagicMock()
        shutdown_event = threading.Event()
        
        def fake_read(*args, **kwargs):
            if not getattr(fake_read, "called", False):
                fake_read.called = True
                return b"com_data"
            shutdown_event.set() # Stop the loop gracefully
            return b""
            
        ser.read.side_effect = fake_read
        
        read_com_port(ser, proc, shutdown_event)
        
        proc.stdin.write.assert_called_with(b"com_data")
        self.assertTrue(shutdown_event.is_set())

    def test_read_com_port_exception(self):
        proc = MagicMock()
        ser = MagicMock()
        shutdown_event = threading.Event()
        
        ser.read.side_effect = Exception("COM Error")
        
        read_com_port(ser, proc, shutdown_event)
        
        self.assertTrue(shutdown_event.is_set())

    def test_read_wsl_stderr_normal(self):
        proc = MagicMock()
        shutdown_event = threading.Event()
        
        proc.stderr.readline.side_effect = [b"error_log\n", b""] # Data then EOF
        
        read_wsl_stderr(proc, shutdown_event)
        self.assertTrue(shutdown_event.is_set() is False)

    def test_read_wsl_stderr_exception(self):
        proc = MagicMock()
        shutdown_event = threading.Event()
        
        proc.stderr.readline.side_effect = Exception("Stderr Error")
        
        read_wsl_stderr(proc, shutdown_event)
        self.assertTrue(shutdown_event.is_set() is False)

    @patch("com2tty.host.get_system_baudrate")
    @patch("com2tty.host.get_wsl_path")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists")
    @patch("threading.Thread")
    @patch("time.sleep")
    def test_run_bridge(self, mock_sleep, mock_thread, mock_exists, mock_popen, mock_serial, mock_wsl_path, mock_get_sys_baud):
        mock_exists.return_value = True
        mock_wsl_path.return_value = "/wsl/bridge.py"
        mock_get_sys_baud.return_value = 115200
        
        mock_proc = MagicMock()
        # Immediately pretend it exited to break loop
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc
        
        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False)
        
        # Ensure the baudrate used was the auto-detected 115200
        mock_serial.assert_called_once()
        self.assertEqual(mock_serial.call_args[1]["baudrate"], 115200)
        
        mock_proc.terminate.assert_not_called()

    @patch("com2tty.host.get_system_baudrate")
    @patch("com2tty.host.get_wsl_path")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists")
    @patch("threading.Thread")
    @patch("time.sleep")
    def test_run_bridge_auto_fallback(self, mock_sleep, mock_thread, mock_exists, mock_popen, mock_serial, mock_wsl_path, mock_get_sys_baud):
        mock_exists.return_value = True
        mock_wsl_path.return_value = "/wsl/bridge.py"
        mock_get_sys_baud.return_value = None # Force fallback
        
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc
        
        run_bridge("COM1", "auto", "/tmp/tty", 8, "N", 1, False, False, False)
        
        # Ensure it fell back to 9600
        self.assertEqual(mock_serial.call_args[1]["baudrate"], 9600)

    @patch("com2tty.host.get_wsl_path")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists")
    @patch("time.sleep")
    def test_run_bridge_keyboard_interrupt(self, mock_sleep, mock_exists, mock_popen, mock_serial, mock_wsl_path):
        mock_exists.return_value = True
        mock_wsl_path.return_value = "/wsl/bridge.py"
        
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        mock_sleep.side_effect = KeyboardInterrupt() # Simulate Ctrl+C
        
        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False, False)
        
        mock_serial.return_value.close.assert_called()
        mock_proc.terminate.assert_called()

    @patch("com2tty.host.get_wsl_path")
    @patch("serial.Serial")
    @patch("subprocess.Popen")
    @patch("os.path.exists")
    @patch("time.sleep")
    def test_run_bridge_cleanup_exceptions(self, mock_sleep, mock_exists, mock_popen, mock_serial, mock_wsl_path):
        mock_exists.return_value = True
        mock_wsl_path.return_value = "/wsl/bridge.py"
        
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = __import__("subprocess").TimeoutExpired(cmd="wsl", timeout=3.0)
        mock_popen.return_value = mock_proc
        
        mock_sleep.side_effect = KeyboardInterrupt()
        
        mock_ser = MagicMock()
        mock_ser.close.side_effect = Exception("Close error")
        mock_serial.return_value = mock_ser
        
        run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False, False)
        
        mock_proc.kill.assert_called()

    @patch("serial.Serial")
    @patch("os.path.exists")
    def test_run_bridge_file_not_found(self, mock_exists, mock_serial):
        mock_exists.return_value = False
        with self.assertRaises(FileNotFoundError):
            run_bridge("COM1", 9600, "/tmp/tty", 8, "N", 1, False, False, False)

if __name__ == "__main__":
    unittest.main()
