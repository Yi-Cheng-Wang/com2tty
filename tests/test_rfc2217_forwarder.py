"""Tests for com2tty.wsl.servers.rfc2217_forwarder (the upload-tool TCP doorway)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import threading
import socket as stdlib_socket


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


from com2tty.wsl.servers.rfc2217_forwarder import run_rfc2217_server_thread


class TestRunRfc2217ServerThread(unittest.TestCase):
    """Tests for the TCP forwarder that runs inside the WSL bridge process."""

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    def test_bind_failure(self, mock_sock_cls, mock_sp, mock_sleep):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.bind.side_effect = OSError("in use")

        run_rfc2217_server_thread(4000, threading.Event())

        sock.listen.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run", side_effect=Exception("no fuser"))
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    def test_fuser_exception_ignored(self, mock_sock_cls, mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = OSError("exit")

        run_rfc2217_server_thread(4000, threading.Event())

        sock.listen.assert_called_once_with(1)
        sock.close.assert_called_once()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    def test_accept_timeout_then_break(self, mock_sock_cls, mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = [
            stdlib_socket.timeout(),   # continue
            OSError("exit"),           # break
        ]

        run_rfc2217_server_thread(4000, threading.Event())
        self.assertEqual(sock.accept.call_count, 2)

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.select.select")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.os.read")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.os.write")
    def test_stdin_and_conn_forwarding(self, mock_wr, mock_rd, mock_sel,
                                       mock_sock_cls, mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 9999)),
            OSError("exit"),
        ]
        # select: first both ready, then stdin EOF
        mock_sel.side_effect = [
            ([0, conn], [], []),
            ([0], [], []),
        ]
        mock_rd.side_effect = [b"hello", b""]
        conn.recv.return_value = b"world"

        evt = threading.Event()
        run_rfc2217_server_thread(4000, evt)

        conn.sendall.assert_called_with(b"hello")
        mock_wr.assert_called_with(1, b"world")
        conn.close.assert_called()
        self.assertFalse(evt.is_set())

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.select.select")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.os.read")
    def test_session_loops_when_only_stdin_ready(self, mock_rd, mock_sel,
                                                 mock_sock_cls, mock_sp, mock_sl):
        """An iteration where only stdin is ready (conn not in the readset) must
        forward and loop again (covers the conn-not-ready arc)."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 9999)),
            OSError("exit"),
        ]
        # iter1: only stdin ready, real data -> sendall, conn not ready -> loop
        # iter2: only stdin ready, EOF -> break
        mock_sel.side_effect = [
            ([0], [], []),
            ([0], [], []),
        ]
        mock_rd.side_effect = [b"data", b""]

        run_rfc2217_server_thread(4000, threading.Event())
        conn.sendall.assert_called_once_with(b"data")
        conn.close.assert_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.select.select")
    def test_conn_blocking_io_and_reset(self, mock_sel, mock_sock_cls,
                                         mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 9999)),
            OSError("exit"),
        ]
        # First: conn ready (BlockingIOError), then conn ready (ConnReset)
        mock_sel.side_effect = [
            ([conn], [], []),
            ([conn], [], []),
        ]
        conn.recv.side_effect = [BlockingIOError(), ConnectionResetError()]

        run_rfc2217_server_thread(4000, threading.Event())
        conn.close.assert_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.select.select")
    def test_conn_eof(self, mock_sel, mock_sock_cls, mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 9999)),
            OSError("exit"),
        ]
        mock_sel.return_value = ([conn], [], [])
        conn.recv.return_value = b""  # EOF

        run_rfc2217_server_thread(4000, threading.Event())
        conn.close.assert_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.socket.socket")
    @patch("com2tty.wsl.servers.rfc2217_forwarder.select.select", side_effect=Exception("boom"))
    def test_session_exception(self, mock_sel, mock_sock_cls, mock_sp,
                                mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        conn = MagicMock()
        sock.accept.side_effect = [
            (conn, ("127.0.0.1", 9999)),
            OSError("exit"),
        ]

        run_rfc2217_server_thread(4000, threading.Event())
        conn.close.assert_called()
