import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
import os
import tempfile
import threading
import socket as stdlib_socket

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import termios  # real on Linux, mock on Windows via conftest
from com2tty.bridge import (
    main, cleanup_symlink, get_rc_files, clean_rc, inject_rc,
    get_pty_settings, run_rfc2217_server_thread, MARKER_START, MARKER_END,
)


# ── get_rc_files ──────────────────────────────────────────────────────────

class TestGetRcFiles(unittest.TestCase):

    def test_returns_bashrc_path(self):
        paths = get_rc_files()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".bashrc"))


# ── clean_rc ──────────────────────────────────────────────────────────────

class TestCleanRc(unittest.TestCase):

    def _make_tmp(self, content):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bashrc", delete=False
        )
        f.write(content)
        f.close()
        return f.name

    def test_removes_injection_block(self):
        path = self._make_tmp(
            f"before\n{MARKER_START}\nexport X=1\n{MARKER_END}\nafter\n"
        )
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[path]):
                clean_rc()
            with open(path) as f:
                text = f.read()
            self.assertIn("before", text)
            self.assertIn("after", text)
            self.assertNotIn("COM2TTY", text)
        finally:
            os.unlink(path)

    def test_noop_without_markers(self):
        path = self._make_tmp("keep me\n")
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[path]):
                clean_rc()
            with open(path) as f:
                self.assertEqual(f.read(), "keep me\n")
        finally:
            os.unlink(path)

    def test_skips_nonexistent_file(self):
        with patch("com2tty.bridge.get_rc_files",
                    return_value=["/no/such/file"]):
            clean_rc()  # should not raise

    def test_handles_io_exception(self):
        path = self._make_tmp("x\n")
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[path]), \
                 patch("builtins.open", side_effect=PermissionError("no")):
                clean_rc()  # logs warning, does not raise
        finally:
            os.unlink(path)


# ── inject_rc ─────────────────────────────────────────────────────────────

class TestInjectRc(unittest.TestCase):

    def test_injects_env_vars(self):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bashrc", delete=False
        )
        f.write("old\n")
        f.close()
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[f.name]):
                inject_rc(4000)
            with open(f.name) as fh:
                text = fh.read()
            self.assertIn("PLATFORMIO_UPLOAD_PORT=rfc2217://127.0.0.1:4000", text)
            self.assertIn("PLATFORMIO_MONITOR_PORT=/tmp/ttyUSB0", text)
            self.assertIn(MARKER_START, text)
        finally:
            os.unlink(f.name)

    def test_handles_write_exception(self):
        with patch("com2tty.bridge.get_rc_files",
                    return_value=["/no/such/dir/bashrc"]), \
             patch("com2tty.bridge.clean_rc"):
            inject_rc(4000)  # should not raise


# ── get_pty_settings ──────────────────────────────────────────────────────

class TestGetPtySettings(unittest.TestCase):

    def test_8n1_9600(self):
        attrs = [0, 0, termios.CS8, 0, 0, termios.B9600, 0]
        with patch("com2tty.bridge.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 9600)
        self.assertEqual(bs, 8)
        self.assertEqual(par, "N")
        self.assertEqual(sb, "1")

    def test_7e2_115200(self):
        cflag = termios.CS7 | termios.PARENB | termios.CSTOPB
        attrs = [0, 0, cflag, 0, 0, termios.B115200, 0]
        with patch("com2tty.bridge.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 115200)
        self.assertEqual(bs, 7)
        self.assertEqual(par, "E")
        self.assertEqual(sb, "2")

    def test_odd_parity(self):
        cflag = termios.CS8 | termios.PARENB | termios.PARODD
        attrs = [0, 0, cflag, 0, 0, termios.B9600, 0]
        with patch("com2tty.bridge.termios.tcgetattr", return_value=attrs):
            _, _, par, _ = get_pty_settings(3)
        self.assertEqual(par, "O")

    def test_returns_none_on_exception(self):
        with patch("com2tty.bridge.termios.tcgetattr",
                   side_effect=Exception("bad fd")):
            self.assertEqual(get_pty_settings(99),
                             (None, None, None, None))


# ── cleanup_symlink ───────────────────────────────────────────────────────

class TestCleanupSymlink(unittest.TestCase):

    @patch("os.path.lexists")
    @patch("os.unlink", create=True)
    def test_removes_existing(self, mock_unlink, mock_lex):
        mock_lex.return_value = True
        cleanup_symlink("/tmp/tty")
        mock_unlink.assert_called_once_with("/tmp/tty")

    @patch("os.path.lexists")
    @patch("os.unlink", create=True)
    def test_noop_when_absent(self, mock_unlink, mock_lex):
        mock_lex.return_value = False
        cleanup_symlink("/tmp/tty")
        mock_unlink.assert_not_called()

    @patch("os.path.lexists", return_value=True)
    @patch("os.unlink", create=True, side_effect=Exception("fail"))
    def test_handles_exception(self, mock_unlink, mock_lex):
        cleanup_symlink("/tmp/tty")  # should not raise


# ── run_rfc2217_server_thread ─────────────────────────────────────────────

class TestRunRfc2217ServerThread(unittest.TestCase):
    """Tests for the TCP forwarder that runs inside the WSL bridge process."""

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
    def test_bind_failure(self, mock_sock_cls, mock_sp, mock_sleep):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.bind.side_effect = OSError("in use")

        run_rfc2217_server_thread(4000, threading.Event())

        sock.listen.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run", side_effect=Exception("no fuser"))
    @patch("com2tty.bridge.socket.socket")
    def test_fuser_exception_ignored(self, mock_sock_cls, mock_sp, mock_sl):
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = OSError("exit")

        run_rfc2217_server_thread(4000, threading.Event())

        sock.listen.assert_called_once_with(1)
        sock.close.assert_called_once()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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
    @patch("com2tty.bridge.socket.socket")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("com2tty.bridge.os.write")
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
    @patch("com2tty.bridge.socket.socket")
    @patch("com2tty.bridge.select.select")
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
    @patch("com2tty.bridge.socket.socket")
    @patch("com2tty.bridge.select.select")
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
    @patch("com2tty.bridge.socket.socket")
    @patch("com2tty.bridge.select.select", side_effect=Exception("boom"))
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


# ── main() ────────────────────────────────────────────────────────────────

class TestBridgeMain(unittest.TestCase):

    # -- helpers -----------------------------------------------------------

    def _base_patches(self):
        """Return a dict of common patches for main() tests."""
        return dict(
            openpty=(3, 4),
            ttyname="/dev/pts/1",
            lexists=False,
            pty_settings=(None, None, None, None),
        )

    # -- normal flow -------------------------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/dev/ttyUSB0"])
    @patch("os.openpty", create=True)
    @patch("os.ttyname", create=True)
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    @patch("os.unlink", create=True)
    @patch("os.close")
    def test_normal_stdin_and_pty(self, mock_close, mock_unlink, mock_write,
                                  mock_read, mock_select, mock_symlink,
                                  mock_lexists, mock_ttyname, mock_openpty):
        mock_openpty.return_value = (3, 4)
        mock_ttyname.return_value = "/dev/pts/1"
        mock_lexists.return_value = False

        call = [0]
        def fake_select(*a, **kw):
            call[0] += 1
            if call[0] == 1:
                return ([0], [], [])
            elif call[0] == 2:
                return ([3], [], [])
            raise KeyboardInterrupt()

        mock_select.side_effect = fake_select
        mock_read.side_effect = [b"stdin_data", b"pty_data"]

        main()

        mock_symlink.assert_called_with("/dev/pts/1", "/dev/ttyUSB0")
        mock_write.assert_any_call(3, b"stdin_data")
        mock_write.assert_any_call(1, b"pty_data")

    # -- permission fallback -----------------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/dev/ttyUSB0"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_permission_fallback(self, mock_unlink, mock_close, mock_read,
                                  mock_select, mock_symlink, mock_lexists,
                                  mock_ttyname, mock_openpty):
        mock_lexists.side_effect = [False, True]

        def fake_sym(src, dst):
            if dst == "/dev/ttyUSB0":
                raise PermissionError("denied")
        mock_symlink.side_effect = fake_sym
        mock_select.return_value = ([0], [], [])

        main()

        mock_symlink.assert_any_call("/dev/pts/1", "/tmp/ttyUSB0")

    # -- EIO + EOF on PTY master -------------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.close", side_effect=Exception("close err"))
    @patch("os.unlink", create=True)
    def test_eio_then_eof(self, mock_unlink, mock_close, mock_read,
                           mock_select, mock_symlink, mock_lexists,
                           mock_ttyname, mock_openpty):
        mock_select.side_effect = lambda *a, **k: ([3], [], [])

        eio = OSError()
        eio.errno = 5
        mock_read.side_effect = [eio, b""]

        main()  # should not raise

    # -- generic OSError on PTY master -------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_generic_oserror(self, mock_unlink, mock_close, mock_read,
                              mock_select, mock_symlink, mock_lexists,
                              mock_ttyname, mock_openpty):
        mock_select.side_effect = lambda *a, **k: ([3], [], [])
        err = OSError("bad")
        err.errno = 99
        mock_read.side_effect = [err]

        main()  # caught by pragma‐covered except

    # -- PTY master EOF (line 290‑292) -------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_pty_eof(self, mock_unlink, mock_close, mock_write, mock_read,
                      mock_select, mock_symlink, mock_lexists,
                      mock_ttyname, mock_openpty):
        mock_select.return_value = ([3], [], [])
        mock_read.return_value = b""  # master EOF

        main()

    # -- fatal exception in openpty ----------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, side_effect=Exception("fatal"))
    def test_fatal_exception(self, mock_openpty):
        main()  # should not raise

    # -- settings change detection -----------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.bridge.get_pty_settings")
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_settings_change(self, mock_unlink, mock_close, mock_read,
                              mock_select, mock_gps, mock_symlink,
                              mock_lexists, mock_ttyname, mock_openpty):
        mock_gps.return_value = (9600, 8, "N", "1")
        # 1st select: timeout (empty) → settings written, loop again
        # 2nd select: stdin ready → EOF → exit
        mock_select.side_effect = [([], [], []), ([0], [], [])]

        main()  # settings CONTROL message is written to stderr

    # -- with --rfc2217-port -----------------------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "4000"])
    @patch("com2tty.bridge.inject_rc")
    @patch("com2tty.bridge.clean_rc")
    @patch("com2tty.bridge.cleanup_symlink")
    @patch("com2tty.bridge.threading.Event")
    @patch("com2tty.bridge.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.bridge.get_pty_settings",
           return_value=(None, None, None, None))
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("time.sleep")
    def test_rfc2217_port_inject_and_clean(
        self, mock_sleep, mock_close, mock_read, mock_select, mock_gps,
        mock_symlink, mock_lexists, mock_ttyname, mock_openpty,
        mock_thread_cls, mock_event_cls, mock_cleanup, mock_clean, mock_inj,
    ):
        mock_evt = MagicMock()
        # is_set sequence: yield once → proceed → post-select proceed → exit
        mock_evt.is_set.side_effect = [True, False, False]
        mock_event_cls.return_value = mock_evt

        mock_select.return_value = ([0], [], [])

        main()

        mock_inj.assert_called_once_with(4000)
        mock_clean.assert_called_once()
        mock_thread_cls.assert_called()

    # -- post-select rfc2217 check (line 272) ------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "5000"])
    @patch("com2tty.bridge.inject_rc")
    @patch("com2tty.bridge.clean_rc")
    @patch("com2tty.bridge.cleanup_symlink")
    @patch("com2tty.bridge.threading.Event")
    @patch("com2tty.bridge.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.bridge.get_pty_settings",
           return_value=(None, None, None, None))
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("time.sleep")
    def test_post_select_rfc2217_check(
        self, mock_sleep, mock_close, mock_read, mock_select, mock_gps,
        mock_symlink, mock_lexists, mock_ttyname, mock_openpty,
        mock_thread_cls, mock_event_cls, mock_cleanup, mock_clean, mock_inj,
    ):
        mock_evt = MagicMock()
        # iter-1: line 256 False → proceed; line 272 True → continue
        # iter-2: line 256 False → proceed; line 272 False → stdin EOF
        mock_evt.is_set.side_effect = [False, True, False, False]
        mock_event_cls.return_value = mock_evt

        mock_select.return_value = ([0], [], [])

        main()

    # -- symlink already exists → unlink first (line 213) ------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=True)
    @patch("os.symlink", create=True)
    @patch("os.unlink", create=True)
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    def test_existing_symlink_removed(self, mock_close, mock_read,
                                       mock_select, mock_unlink, mock_sym,
                                       mock_lex, mock_tty, mock_pty):
        mock_select.return_value = ([0], [], [])
        main()
        mock_unlink.assert_any_call("/tmp/tty")


if __name__ == "__main__":
    unittest.main()
