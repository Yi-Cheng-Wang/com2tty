import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.rfc2217_server import Redirector


@patch("serial.rfc2217.PortManager")
class TestRedirector(unittest.TestCase):

    # ── init / stop ──────────────────────────────────────────────────────

    def test_init(self, mock_pm):
        ser, sock = MagicMock(), MagicMock()
        r = Redirector(ser, sock)
        self.assertIs(r.serial, ser)
        self.assertIs(r.socket, sock)
        self.assertFalse(r.alive)
        mock_pm.assert_called_once()

    def test_init_debug(self, mock_pm):
        Redirector(MagicMock(), MagicMock(), debug=True)
        mock_pm.assert_called_once()

    def test_stop_when_alive(self, mock_pm):
        r = Redirector(MagicMock(), MagicMock())
        r.alive = True
        r.stop()
        self.assertFalse(r.alive)

    def test_stop_when_already_dead(self, mock_pm):
        r = Redirector(MagicMock(), MagicMock())
        r.stop()
        self.assertFalse(r.alive)

    # ── write ────────────────────────────────────────────────────────────

    def test_write_success(self, mock_pm):
        sock = MagicMock()
        r = Redirector(MagicMock(), sock)
        r.write(b"data")
        sock.sendall.assert_called_with(b"data")

    def test_write_exception(self, mock_pm):
        sock = MagicMock()
        sock.sendall.side_effect = Exception("fail")
        r = Redirector(MagicMock(), sock)
        r.write(b"data")  # should not raise

    # ── reader ───────────────────────────────────────────────────────────

    def test_reader_with_data(self, mock_pm):
        ser = MagicMock()
        sock = MagicMock()
        r = Redirector(ser, sock)
        r.alive = True
        ser.in_waiting = 5
        ser.read.side_effect = [b"hello", Exception("done")]
        r.rfc2217.escape.return_value = [b"hello"]

        r.reader()

        sock.sendall.assert_called_with(b"hello")
        self.assertFalse(r.alive)

    def test_reader_no_data(self, mock_pm):
        ser = MagicMock()
        r = Redirector(ser, MagicMock())
        r.alive = True
        ser.in_waiting = 0
        call = [0]

        def fake_read(n):
            call[0] += 1
            if call[0] > 2:
                raise Exception("exit")
            return b""

        ser.read.side_effect = fake_read
        r.reader()
        self.assertFalse(r.alive)

    def test_reader_exception_while_alive(self, mock_pm):
        ser = MagicMock()
        r = Redirector(ser, MagicMock())
        r.alive = True
        ser.in_waiting = 1
        ser.read.side_effect = Exception("read err")

        r.reader()
        self.assertFalse(r.alive)

    def test_reader_exception_while_dead(self, mock_pm):
        ser = MagicMock()
        r = Redirector(ser, MagicMock())
        r.alive = False
        # reader() loop body never runs because alive is False
        r.reader()
        self.assertFalse(r.alive)

    def test_reader_exception_after_alive_cleared(self, mock_pm):
        # alive flips to False mid-read (e.g. concurrent stop()); the except
        # handler then skips the error log and just breaks.
        ser = MagicMock()
        r = Redirector(ser, MagicMock())
        r.alive = True
        ser.in_waiting = 1

        def boom(n):
            r.alive = False
            raise Exception("read err during stop")
        ser.read.side_effect = boom

        r.reader()
        self.assertFalse(r.alive)

    # ── writer ───────────────────────────────────────────────────────────

    def test_writer_eof(self, mock_pm):
        sock = MagicMock()
        sock.recv.return_value = b""
        r = Redirector(MagicMock(), sock)
        r.alive = True

        r.writer()

        self.assertFalse(r.alive)

    def test_writer_with_data(self, mock_pm):
        sock = MagicMock()
        ser = MagicMock()
        sock.recv.side_effect = [b"cmd", b""]
        r = Redirector(ser, sock)
        r.alive = True
        r.rfc2217.filter.return_value = [b"cmd"]

        r.writer()

        ser.write.assert_called_with(b"cmd")

    def test_writer_exception_while_alive(self, mock_pm):
        sock = MagicMock()
        sock.recv.side_effect = Exception("recv err")
        r = Redirector(MagicMock(), sock)
        r.alive = True

        r.writer()
        self.assertFalse(r.alive)

    def test_writer_exception_while_dead(self, mock_pm):
        sock = MagicMock()
        sock.recv.side_effect = Exception("recv err")
        r = Redirector(MagicMock(), sock)
        r.alive = False
        # writer exits immediately because alive is False
        r.writer()

    def test_writer_exception_after_alive_cleared(self, mock_pm):
        # alive flips to False mid-recv; the except handler skips the log.
        sock = MagicMock()
        r = Redirector(MagicMock(), sock)
        r.alive = True

        def boom(n):
            r.alive = False
            raise Exception("recv err during stop")
        sock.recv.side_effect = boom

        r.writer()
        self.assertFalse(r.alive)

    # ── statusline_poller ────────────────────────────────────────────────

    @patch("com2tty.rfc2217_server.time.sleep")
    def test_statusline_poller_normal(self, mock_sleep, mock_pm):
        r = Redirector(MagicMock(), MagicMock())
        r.alive = True

        call = [0]
        def stop_after_one(*a):
            call[0] += 1
            if call[0] >= 1:
                r.alive = False
        mock_sleep.side_effect = stop_after_one

        r.statusline_poller()
        r.rfc2217.check_modem_lines.assert_called()

    @patch("com2tty.rfc2217_server.time.sleep")
    def test_statusline_poller_exception(self, mock_sleep, mock_pm):
        r = Redirector(MagicMock(), MagicMock())
        r.alive = True
        r.rfc2217.check_modem_lines.side_effect = Exception("err")

        call = [0]
        def stop_after_one(*a):
            call[0] += 1
            if call[0] >= 1:
                r.alive = False
        mock_sleep.side_effect = stop_after_one

        r.statusline_poller()  # should not raise

    # ── shortcircuit ─────────────────────────────────────────────────────

    def test_shortcircuit(self, mock_pm):
        """shortcircuit starts reader + poller threads, then calls writer."""
        sock = MagicMock()
        ser = MagicMock()
        sock.recv.return_value = b""  # writer EOF → stop()
        ser.in_waiting = 0
        ser.read.return_value = b""

        r = Redirector(ser, sock)
        r.shortcircuit()

        self.assertFalse(r.alive)


if __name__ == "__main__":
    unittest.main()
