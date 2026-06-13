"""Tests for com2tty.wsl.servers.uf2_relay (firmware relay from the picotool wrapper)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import threading
import socket as stdlib_socket
import hashlib


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


from com2tty.wsl.servers.uf2_relay import (
    md5_hexdigest,
    run_uf2_relay_thread,
)


class TestRunUf2RelayThread(unittest.TestCase):
    """Tests for the UF2 relay TCP server thread."""

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_bind_failure(self, mock_sock_cls, mock_sp, mock_sleep):
        """When bind fails, function returns immediately."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.bind.side_effect = OSError("address in use")

        run_uf2_relay_thread(5001, threading.Event())

        sock.listen.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_accept_timeout_then_break(self, mock_sock_cls, mock_sp, mock_sleep):
        """Accept timeout continues, then generic exception breaks."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = [
            stdlib_socket.timeout(),  # continue
            OSError("exit"),           # break
        ]

        run_uf2_relay_thread(5001, threading.Event())

        self.assertEqual(sock.accept.call_count, 2)
        sock.close.assert_called_once()

    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_successful_uf2_receive_with_ack(self, mock_sock_cls, mock_sp,
                                              mock_sleep, mock_os_read,
                                              mock_select, mock_stdout):
        """Full happy path: receive UF2 data, get ACK, write to stdout."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        uf2_data = b"\x00UF2_TEST_DATA\x00" * 10
        conn.recv.side_effect = [uf2_data, b""]  # data then EOF

        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        # select returns stdin ready with ACK
        mock_select.return_value = ([0], [], [])
        mock_os_read.return_value = b"[CONTROL] UF2_ACK\n"

        mock_buf = MagicMock()
        mock_stdout.buffer = mock_buf

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # Verify UF2 data written to stdout
        mock_buf.write.assert_called_once_with(bytearray(uf2_data))
        mock_buf.flush.assert_called_once()
        conn.close.assert_called()
        self.assertFalse(evt.is_set())

    @patch("com2tty.wsl.servers.uf2_relay.sys.stderr")
    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    @patch("time.time")
    def test_uf2_receive_timeout_waiting_for_ack(self, mock_time, mock_sock_cls,
                                                   mock_sp, mock_sleep,
                                                   mock_os_read, mock_select,
                                                   mock_stdout, mock_stderr):
        """UF2 data received but ACK times out → error message."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        conn.recv.side_effect = [b"data", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        # Make time.time() return values that cause timeout
        # First call sets timeout_time = 100 + 5.0 = 105.0
        # Second call is the while condition check → past timeout
        mock_time.side_effect = [100.0, 106.0]

        # select never returns stdin ready (empty)
        mock_select.return_value = ([], [], [])

        mock_stdout.buffer = MagicMock()

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # stdout.buffer.write should NOT have been called
        mock_stdout.buffer.write.assert_not_called()
        # Error message about timeout written to stderr
        mock_stderr.write.assert_any_call("[CONTROL] UF2_ERROR: Timeout waiting for host UF2_ACK\n")

    @patch("com2tty.wsl.servers.uf2_relay.sys.stderr")
    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    @patch("time.time")
    def test_ack_wait_loops_when_stdin_not_ready(self, mock_time, mock_sock_cls,
                                                 mock_sp, mock_sleep,
                                                 mock_os_read, mock_select,
                                                 mock_stdout, mock_stderr):
        """An ACK-wait iteration where stdin is not ready must loop and recheck
        the deadline (covers the stdin-not-ready arc of the ACK loop)."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        conn.recv.side_effect = [b"data", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]
        # timeout_time=105; iter1 101<105 -> enter loop; iter2 106 -> exit.
        mock_time.side_effect = [100.0, 101.0, 106.0]
        mock_select.return_value = ([], [], [])  # stdin never ready
        mock_stdout.buffer = MagicMock()

        run_uf2_relay_thread(5001, threading.Event())
        mock_stdout.buffer.write.assert_not_called()

    @patch("com2tty.wsl.servers.uf2_relay.sys.stderr")
    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_uf2_receive_ack_but_stdout_write_fails(self, mock_sock_cls, mock_sp,
                                                      mock_sleep, mock_os_read,
                                                      mock_select, mock_stdout,
                                                      mock_stderr):
        """ACK received but stdout write raises exception."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        conn.recv.side_effect = [b"data", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        mock_select.return_value = ([0], [], [])
        mock_os_read.return_value = b"[CONTROL] UF2_ACK\n"

        mock_buf = MagicMock()
        mock_buf.write.side_effect = BrokenPipeError("pipe broken")
        mock_stdout.buffer = mock_buf

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # Error about write failure logged
        mock_stderr.write.assert_any_call(
            "[CONTROL] UF2_ERROR: Failed to write to stdout: pipe broken\n"
        )

    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_recv_exception_during_data_collection(self, mock_sock_cls, mock_sp,
                                                    mock_sleep, mock_os_read,
                                                    mock_select, mock_stdout):
        """Exception during conn.recv is caught, partial data still processed."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        conn.recv.side_effect = [b"partial", ConnectionResetError("reset")]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        mock_select.return_value = ([0], [], [])
        mock_os_read.return_value = b"[CONTROL] UF2_ACK\n"

        mock_buf = MagicMock()
        mock_stdout.buffer = mock_buf

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # Partial data should still be written
        mock_buf.write.assert_called_once_with(bytearray(b"partial"))
        conn.close.assert_called()

    @patch("time.sleep")
    @patch("subprocess.run", side_effect=Exception("no fuser"))
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_fuser_cleanup_exception_ignored(self, mock_sock_cls, mock_sp,
                                              mock_sleep):
        """Exception from fuser subprocess is silently ignored."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = OSError("exit")

        run_uf2_relay_thread(5001, threading.Event())

        sock.listen.assert_called_once_with(1)
        sock.close.assert_called_once()

    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_ack_wait_loops_over_non_ack_chunk(self, mock_sock_cls, mock_sp,
                                               mock_sleep, mock_os_read,
                                               mock_select, mock_stdout):
        """A non-ACK chunk arriving first must loop and keep waiting until the
        ACK is seen (covers the keep-waiting branch of the ACK loop)."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        conn.recv.side_effect = [b"firmware", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]
        mock_select.return_value = ([0], [], [])
        # First read has no ACK marker, second completes it.
        mock_os_read.side_effect = [b"noise", b"[CONTROL] UF2_ACK\n"]

        mock_buf = MagicMock()
        mock_stdout.buffer = mock_buf

        run_uf2_relay_thread(5001, threading.Event())
        mock_buf.write.assert_called_once_with(bytearray(b"firmware"))

    @patch("com2tty.wsl.servers.uf2_relay.sys.stderr")
    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_stdin_read_returns_eof_during_ack_wait(self, mock_sock_cls, mock_sp,
                                                     mock_sleep, mock_os_read,
                                                     mock_select, mock_stdout,
                                                     mock_stderr):
        """stdin read returns empty bytes (EOF) during ACK wait → break, timeout."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        conn.recv.side_effect = [b"data", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        mock_select.return_value = ([0], [], [])
        mock_os_read.return_value = b""  # EOF on stdin

        mock_stdout.buffer = MagicMock()

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # No ACK → timeout path
        mock_stdout.buffer.write.assert_not_called()
        mock_stderr.write.assert_any_call(
            "[CONTROL] UF2_ERROR: Timeout waiting for host UF2_ACK\n"
        )

    @patch("com2tty.wsl.servers.uf2_relay.sys.stderr")
    @patch("com2tty.wsl.servers.uf2_relay.sys.stdout")
    @patch("com2tty.wsl.servers.uf2_relay.select.select")
    @patch("com2tty.wsl.servers.uf2_relay.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.base.socket.socket")
    def test_stdin_read_exception_during_ack_wait(self, mock_sock_cls, mock_sp,
                                                    mock_sleep, mock_os_read,
                                                    mock_select, mock_stdout,
                                                    mock_stderr):
        """Exception during os.read in ACK wait → break, timeout."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()

        conn.recv.side_effect = [b"data", b""]
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 12345)),
            OSError("exit"),
        ]

        mock_select.return_value = ([0], [], [])
        mock_os_read.side_effect = OSError("read error")

        mock_stdout.buffer = MagicMock()

        evt = threading.Event()
        run_uf2_relay_thread(5001, evt)

        # No ACK → timeout path
        mock_stdout.buffer.write.assert_not_called()
        mock_stderr.write.assert_any_call(
            "[CONTROL] UF2_ERROR: Timeout waiting for host UF2_ACK\n"
        )



class TestMd5Hexdigest(unittest.TestCase):

    def test_matches_hashlib(self):
        self.assertEqual(md5_hexdigest(b"data"),
                         hashlib.md5(b"data").hexdigest())

    def test_fallback_without_usedforsecurity(self):
        real_md5 = hashlib.md5

        def legacy_md5(data, **kwargs):
            if kwargs:
                raise TypeError("usedforsecurity not supported")
            return real_md5(data)

        with patch("hashlib.md5", side_effect=legacy_md5):
            self.assertEqual(md5_hexdigest(b"data"),
                             real_md5(b"data").hexdigest())
