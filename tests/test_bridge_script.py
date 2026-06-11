import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call
import sys
import os
import tempfile
import threading
import socket as stdlib_socket
import hashlib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import termios  # real on Linux, mock on Windows via conftest
from com2tty.bridge import (
    main, cleanup_symlink, get_rc_files, clean_rc, inject_rc,
    get_pty_settings, run_rfc2217_server_thread, run_uf2_relay_thread,
    setup_picotool_interceptor, cleanup_picotool_interceptor,
    intercepted_picotools, MARKER_START, MARKER_END,
    PICOTOOL_WRAPPER_CONTENT, kill_leftover_listener, md5_hexdigest,
    restore_orphaned_picotools,
)


# ── get_rc_files ──────────────────────────────────────────────────────────

class TestGetRcFiles(unittest.TestCase):

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    def test_returns_bashrc_path(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".bashrc"))

    @patch.dict(os.environ, {"SHELL": "/usr/bin/zsh"})
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    def test_includes_zshrc_for_zsh_shell(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].endswith(".zshrc"))

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.bridge.os.path.exists", return_value=True)
    def test_includes_zshrc_when_file_exists(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].endswith(".zshrc"))


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

    def test_preserves_content_before_inline_marker(self):
        # A start marker appended to an existing command line must keep the
        # user's code prefix while dropping the injected block.
        path = self._make_tmp(
            f"echo hi {MARKER_START}\nexport X=1\n{MARKER_END}\n")
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[path]):
                clean_rc()
            with open(path) as f:
                text = f.read()
            self.assertIn("echo hi", text)
            self.assertNotIn("export X=1", text)
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

    def test_adds_newline_when_file_lacks_trailing_newline(self):
        # A user rc file not ending in a newline must not get the injection
        # block glued onto the last line.
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bashrc", delete=False
        )
        f.write("last line no newline")  # deliberately no trailing \n
        f.close()
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[f.name]):
                inject_rc(4000)
            with open(f.name) as fh:
                text = fh.read()
            self.assertIn("last line no newline\n" + MARKER_START, text)
        finally:
            os.unlink(f.name)

    def test_injects_custom_monitor_path(self):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bashrc", delete=False
        )
        f.write("old\n")
        f.close()
        try:
            with patch("com2tty.bridge.get_rc_files", return_value=[f.name]):
                inject_rc(4000, "/tmp/ttyACM5")
            with open(f.name) as fh:
                text = fh.read()
            self.assertIn("PLATFORMIO_MONITOR_PORT=/tmp/ttyACM5", text)
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


# ── kill_leftover_listener ────────────────────────────────────────────────

class TestKillLeftoverListener(unittest.TestCase):

    # signal.SIGKILL is absent on Windows (this runs only inside WSL/Linux in
    # production); create it for the tests so the kill path is exercised here.
    @patch("com2tty.bridge.signal.SIGKILL", 9, create=True)
    @patch("time.sleep")
    @patch("com2tty.bridge.os.kill")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_kills_only_com2tty_process(self, mock_sp, mock_open, mock_kill,
                                        mock_sleep):
        """A PID whose cmdline references our bridge is killed."""
        mock_sp.return_value = MagicMock(stdout=b" 1234\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"python3\x00bridge.py\x00--symlink"
        kill_leftover_listener(4000)
        mock_kill.assert_called_once()
        self.assertEqual(mock_kill.call_args[0][0], 1234)
        mock_sleep.assert_called_once_with(0.3)

    @patch("com2tty.bridge.sys.stderr")
    @patch("time.sleep")
    @patch("com2tty.bridge.os.kill")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_spares_unrelated_process(self, mock_sp, mock_open, mock_kill,
                                      mock_sleep, mock_stderr):
        """A PID owned by an unrelated process is NOT killed."""
        mock_sp.return_value = MagicMock(stdout=b" 5678\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"/usr/bin/nginx\x00-g\x00daemon off;"
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()
        mock_sleep.assert_not_called()
        written = "".join(c.args[0] for c in mock_stderr.write.call_args_list)
        self.assertIn("unrelated process", written)

    @patch("time.sleep")
    @patch("com2tty.bridge.os.kill")
    @patch("builtins.open", side_effect=FileNotFoundError("no proc"))
    @patch("subprocess.run")
    def test_skips_pid_with_unreadable_cmdline(self, mock_sp, mock_open,
                                               mock_kill, mock_sleep):
        """A PID whose /proc cmdline cannot be read is skipped."""
        mock_sp.return_value = MagicMock(stdout=b"9999\n")
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()

    @patch("com2tty.bridge.signal.SIGKILL", 9, create=True)
    @patch("time.sleep")
    @patch("com2tty.bridge.os.kill", side_effect=ProcessLookupError())
    @patch("builtins.open", new_callable=MagicMock)
    @patch("subprocess.run")
    def test_kill_exception_swallowed(self, mock_sp, mock_open, mock_kill,
                                      mock_sleep):
        """os.kill raising (process already gone) is tolerated."""
        mock_sp.return_value = MagicMock(stdout=b"1234\n")
        handle = mock_open.return_value.__enter__.return_value
        handle.read.return_value = b"bridge.py"
        kill_leftover_listener(4000)  # should not raise

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_pids_no_sleep(self, mock_sp, mock_sleep):
        """Empty fuser output means nothing to kill and no sleep."""
        mock_sp.return_value = MagicMock(stdout=b"")
        kill_leftover_listener(4000)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("com2tty.bridge.os.kill")
    @patch("subprocess.run")
    def test_non_numeric_pid_skipped(self, mock_sp, mock_kill, mock_sleep):
        """Non-numeric tokens in fuser output are ignored."""
        mock_sp.return_value = MagicMock(stdout=b"notapid\n")
        kill_leftover_listener(4000)
        mock_kill.assert_not_called()

    @patch("com2tty.bridge.sys.stderr")
    @patch("subprocess.run", side_effect=FileNotFoundError("no fuser"))
    def test_fuser_missing_prints_note(self, mock_sp, mock_stderr):
        kill_leftover_listener(4000)
        written = "".join(c.args[0] for c in mock_stderr.write.call_args_list)
        self.assertIn("psmisc", written)

    @patch("subprocess.run", side_effect=Exception("boom"))
    def test_generic_exception_ignored(self, mock_sp):
        kill_leftover_listener(4000)  # should not raise


# ── restore_orphaned_picotools ────────────────────────────────────────────

class TestRestoreOrphanedPicotools(unittest.TestCase):

    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.path.lexists", return_value=True)
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    @patch("com2tty.bridge.os.path.islink", return_value=True)
    @patch("com2tty.bridge.glob.glob")
    def test_restores_orphan(self, mock_glob, mock_islink, mock_exists,
                             mock_lexists, mock_remove, mock_rename):
        real = "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"
        mock_glob.return_value = [real]
        restore_orphaned_picotools()
        picotool = real[:-len(".real")]
        mock_remove.assert_called_once_with(picotool)
        mock_rename.assert_called_once_with(real, picotool)

    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.path.exists", return_value=True)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    def test_skips_when_real_binary_present(self, mock_glob, mock_islink,
                                            mock_exists, mock_rename):
        """If a genuine binary occupies the live path, do not clobber it."""
        mock_glob.return_value = [
            "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"]
        restore_orphaned_picotools()
        mock_rename.assert_not_called()

    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.path.lexists", return_value=False)
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    def test_restores_when_live_path_absent(self, mock_glob, mock_islink,
                                            mock_exists, mock_lexists,
                                            mock_remove, mock_rename):
        """Live picotool path is gone entirely: rename .real back, no remove."""
        real = "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"
        mock_glob.return_value = [real]
        restore_orphaned_picotools()
        mock_remove.assert_not_called()
        mock_rename.assert_called_once_with(real, real[:-len(".real")])

    @patch("com2tty.bridge.os.path.islink", side_effect=OSError("boom"))
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    def test_exception_swallowed(self, mock_glob, mock_exists, mock_islink):
        mock_glob.return_value = [
            "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"]
        restore_orphaned_picotools()  # should not raise

    @patch("com2tty.bridge.glob.glob", return_value=[])
    def test_no_orphans(self, mock_glob):
        restore_orphaned_picotools()  # nothing to do


# ── md5_hexdigest ─────────────────────────────────────────────────────────

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
    @patch("com2tty.bridge.os.read")
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


# ── setup_picotool_interceptor ────────────────────────────────────────────

class TestSetupPicotoolInterceptor(unittest.TestCase):

    def setUp(self):
        intercepted_picotools.clear()

    @patch("com2tty.bridge.os.symlink")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.path.lexists")
    @patch("com2tty.bridge.os.path.exists")
    @patch("com2tty.bridge.os.path.isfile", return_value=True)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    def test_successful_setup(self, mock_open, mock_chmod, mock_glob,
                               mock_islink, mock_isfile, mock_exists,
                               mock_lexists, mock_rename, mock_remove,
                               mock_symlink):
        """Wrapper created, glob finds a picotool binary, rename + symlink succeed."""
        mock_glob.return_value = ["/home/user/.platformio/packages/tool-picotool-rp2040/picotool"]
        mock_exists.return_value = False  # real_path does not exist yet
        mock_lexists.return_value = False  # picotool_path does not exist after rename

        setup_picotool_interceptor(5001)

        # Wrapper file written and chmod'd
        mock_open.assert_called_once_with("/tmp/com2tty_picotool.py", "w")
        mock_chmod.assert_called_once_with("/tmp/com2tty_picotool.py", 0o755)

        # picotool renamed and symlinked
        picotool_path = "/home/user/.platformio/packages/tool-picotool-rp2040/picotool"
        real_path = picotool_path + ".real"
        mock_rename.assert_called_once_with(picotool_path, real_path)
        mock_symlink.assert_called_once_with("/tmp/com2tty_picotool.py", picotool_path)
        self.assertEqual(len(intercepted_picotools), 1)
        self.assertEqual(intercepted_picotools[0], (picotool_path, real_path))

    @patch("com2tty.bridge.os.symlink")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.path.lexists", return_value=True)
    @patch("com2tty.bridge.os.path.exists", return_value=True)
    @patch("com2tty.bridge.os.path.isfile", return_value=True)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    def test_existing_real_path_and_lexists(self, mock_open, mock_chmod, mock_glob,
                                            mock_islink, mock_isfile, mock_exists,
                                            mock_lexists, mock_rename, mock_remove,
                                            mock_symlink):
        """real_path already exists (skip rename), picotool_path lexists (remove before symlink)."""
        mock_glob.return_value = ["/home/user/.platformio/packages/tool-picotool-rp2040/picotool"]

        setup_picotool_interceptor(5001)

        # rename NOT called because real_path already exists
        mock_rename.assert_not_called()
        # remove called because picotool_path lexists
        mock_remove.assert_called_once()
        mock_symlink.assert_called_once()
        self.assertEqual(len(intercepted_picotools), 1)

    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", side_effect=PermissionError("cannot write"))
    def test_wrapper_creation_failure(self, mock_open, mock_chmod, mock_glob):
        """If wrapper file creation fails, function returns early."""
        setup_picotool_interceptor(5001)

        mock_chmod.assert_not_called()
        mock_glob.assert_not_called()
        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("com2tty.bridge.os.path.islink", return_value=True)
    def test_skip_symlink_path(self, mock_islink, mock_open, mock_chmod, mock_glob):
        """Paths that are already symlinks are skipped."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.os.path.isfile", return_value=False)
    def test_skip_non_file_path(self, mock_isfile, mock_islink, mock_open,
                                 mock_chmod, mock_glob):
        """Paths that are not regular files are skipped."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.bridge.os.symlink", side_effect=OSError("symlink fail"))
    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.path.lexists", return_value=False)
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    @patch("com2tty.bridge.os.path.isfile", return_value=True)
    @patch("com2tty.bridge.os.path.islink", return_value=False)
    @patch("com2tty.bridge.glob.glob")
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    def test_rename_symlink_exception(self, mock_open, mock_chmod, mock_glob,
                                       mock_islink, mock_isfile, mock_exists,
                                       mock_lexists, mock_rename, mock_symlink):
        """Exception during rename/symlink is caught and logged."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        # Exception caught, nothing added to intercepted_picotools
        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.bridge.glob.glob", return_value=[])
    @patch("com2tty.bridge.os.chmod")
    @patch("builtins.open", new_callable=MagicMock)
    def test_no_glob_matches(self, mock_open, mock_chmod, mock_glob):
        """When glob returns no matches, nothing is intercepted."""
        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)


# ── cleanup_picotool_interceptor ──────────────────────────────────────────

class TestCleanupPicotoolInterceptor(unittest.TestCase):

    def setUp(self):
        intercepted_picotools.clear()

    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.path.exists", return_value=True)
    @patch("com2tty.bridge.os.path.lexists", return_value=True)
    def test_successful_restore(self, mock_lexists, mock_exists,
                                 mock_remove, mock_rename):
        """Cleanup removes symlink and renames .real back to original."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()

        mock_remove.assert_called_once_with("/path/picotool")
        mock_rename.assert_called_once_with("/path/picotool.real", "/path/picotool")

    @patch("com2tty.bridge.os.path.lexists", side_effect=OSError("fail"))
    def test_exception_during_restore(self, mock_lexists):
        """Exception during cleanup is caught and logged, not raised."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()  # should not raise

    @patch("com2tty.bridge.os.rename")
    @patch("com2tty.bridge.os.remove")
    @patch("com2tty.bridge.os.path.exists", return_value=False)
    @patch("com2tty.bridge.os.path.lexists", return_value=False)
    def test_no_files_to_restore(self, mock_lexists, mock_exists,
                                  mock_remove, mock_rename):
        """When neither symlink nor .real exists, skip gracefully."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()

        mock_remove.assert_not_called()
        mock_rename.assert_not_called()


# ── run_uf2_relay_thread ──────────────────────────────────────────────────

class TestRunUf2RelayThread(unittest.TestCase):
    """Tests for the UF2 relay TCP server thread."""

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
    def test_bind_failure(self, mock_sock_cls, mock_sp, mock_sleep):
        """When bind fails, function returns immediately."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.bind.side_effect = OSError("address in use")

        run_uf2_relay_thread(5001, threading.Event())

        sock.listen.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stderr")
    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stderr")
    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stderr")
    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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
    @patch("com2tty.bridge.socket.socket")
    def test_fuser_cleanup_exception_ignored(self, mock_sock_cls, mock_sp,
                                              mock_sleep):
        """Exception from fuser subprocess is silently ignored."""
        sock = MagicMock()
        mock_sock_cls.return_value = sock
        sock.accept.side_effect = OSError("exit")

        run_uf2_relay_thread(5001, threading.Event())

        sock.listen.assert_called_once_with(1)
        sock.close.assert_called_once()

    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stderr")
    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("com2tty.bridge.sys.stderr")
    @patch("com2tty.bridge.sys.stdout")
    @patch("com2tty.bridge.select.select")
    @patch("com2tty.bridge.os.read")
    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("com2tty.bridge.socket.socket")
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

    @patch("sys.argv", ["bridge.py", "--symlink", "/dev/ttyUSB0"])
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_permission_fallback_when_fallback_path_absent(
            self, mock_unlink, mock_close, mock_read, mock_select,
            mock_symlink, mock_lexists, mock_ttyname, mock_openpty):
        # target lexists False, then fallback lexists False -> skip the unlink
        # and symlink straight to the fallback path.
        mock_lexists.side_effect = [False, False]

        def fake_sym(src, dst):
            if dst == "/dev/ttyUSB0":
                raise PermissionError("denied")
        mock_symlink.side_effect = fake_sym
        mock_select.return_value = ([0], [], [])

        main()

        mock_symlink.assert_any_call("/dev/pts/1", "/tmp/ttyUSB0")
        mock_unlink.assert_not_called()

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

        mock_inj.assert_called_once_with(4000, "/tmp/tty")
        mock_clean.assert_called_once()
        mock_thread_cls.assert_called()

    # -- post-select rfc2217 check (line 464) ------------------------------

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
        # iter-1: line 448 False → proceed; line 464 True → continue
        # iter-2: line 448 False → proceed; line 464 False → stdin EOF
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

    # -- post-select uf2_active check (line 465) ---------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "6000"])
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
    @patch("com2tty.bridge.setup_picotool_interceptor")
    @patch("com2tty.bridge.cleanup_picotool_interceptor")
    def test_post_select_uf2_active_check(
        self, mock_picotool_cleanup, mock_picotool_setup,
        mock_sleep, mock_close, mock_read, mock_select, mock_gps,
        mock_symlink, mock_lexists, mock_ttyname, mock_openpty,
        mock_thread_cls, mock_event_cls, mock_cleanup, mock_clean, mock_inj,
    ):
        """When uf2_active becomes set after select returns, the main loop
        should continue (skip data processing) and then on next iteration
        proceed normally.

        Line 448 checks: rfc2217_active.is_set() or uf2_active.is_set()
        Line 464 checks: rfc2217_active.is_set() or uf2_active.is_set()

        Since both events are the same mock, each is_set() call consumes
        from side_effect. We need:
          iter-1 line 448: is_set() → False, is_set() → False → proceed
          iter-1 line 464: is_set() → False, is_set() → True  → continue (line 465!)
          iter-2 line 448: is_set() → False, is_set() → False → proceed
          iter-2 line 464: is_set() → False, is_set() → False → proceed to stdin EOF
        """
        mock_evt = MagicMock()
        mock_evt.is_set.side_effect = [
            False, False,   # iter-1, line 448: rfc=False, uf2=False → proceed
            False, True,    # iter-1, line 464: rfc=False, uf2=True  → continue
            False, False,   # iter-2, line 448: rfc=False, uf2=False → proceed
            False, False,   # iter-2, line 464: rfc=False, uf2=False → proceed → EOF
        ]
        mock_event_cls.return_value = mock_evt

        mock_select.return_value = ([0], [], [])

        main()


if __name__ == "__main__":
    unittest.main()
