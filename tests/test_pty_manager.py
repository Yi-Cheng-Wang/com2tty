"""Tests for com2tty.wsl.pty_manager (pty primitives and termios polling)."""
import unittest
from unittest.mock import patch
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import termios  # real on Linux, mock on Windows via conftest

from com2tty.wsl.pty_manager import (
    _refuse_if_foreign_live_pty,
    cleanup_symlink,
    create_symlink_with_fallback,
    get_pty_settings,
    open_pty,
    set_raw_mode,
)


def _cooked_attrs():
    """A termios attrs list with every cooked-mode flag this code clears set."""
    iflag = (termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
             | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
    oflag = termios.OPOST
    cflag = termios.CS8 | termios.PARENB  # must survive untouched
    lflag = (termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
             | termios.IEXTEN)
    cc = [0] * 32
    return [iflag, oflag, cflag, lflag, termios.B9600, termios.B9600, cc]


class TestRefuseIfForeignLivePty(unittest.TestCase):

    @patch("os.path.islink", return_value=False)
    def test_non_link_is_allowed(self, m_islink):
        _refuse_if_foreign_live_pty("/tmp/ttyUSB0", "/dev/pts/3")  # no raise

    @patch("os.path.exists", return_value=True)
    @patch("os.readlink", return_value="/dev/pts/9")
    @patch("os.path.islink", return_value=True)
    def test_foreign_live_pty_is_refused(self, m_islink, m_readlink, m_exists):
        with self.assertRaises(FileExistsError):
            _refuse_if_foreign_live_pty("/tmp/ttyUSB0", "/dev/pts/3")

    @patch("os.readlink", return_value="/dev/pts/3")
    @patch("os.path.islink", return_value=True)
    def test_our_own_link_is_allowed(self, m_islink, m_readlink):
        _refuse_if_foreign_live_pty("/tmp/ttyUSB0", "/dev/pts/3")  # no raise

    @patch("os.path.exists", return_value=False)
    @patch("os.readlink", return_value="/dev/pts/9")
    @patch("os.path.islink", return_value=True)
    def test_dangling_link_is_allowed(self, m_islink, m_readlink, m_exists):
        # The previous owner's pts is gone; safe to replace.
        _refuse_if_foreign_live_pty("/tmp/ttyUSB0", "/dev/pts/3")  # no raise

    @patch("os.readlink", side_effect=OSError("boom"))
    @patch("os.path.islink", return_value=True)
    def test_unreadable_link_is_allowed(self, m_islink, m_readlink):
        _refuse_if_foreign_live_pty("/tmp/ttyUSB0", "/dev/pts/3")  # no raise


class TestCreateSymlinkAntiHijack(unittest.TestCase):

    @patch("os.symlink", create=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.readlink", return_value="/dev/pts/9")
    @patch("os.path.islink", return_value=True)
    def test_refuses_to_hijack_and_does_not_symlink(
            self, m_islink, m_readlink, m_exists, m_symlink):
        with self.assertRaises(FileExistsError):
            create_symlink_with_fallback("/dev/pts/3", "/tmp/ttyUSB0")
        m_symlink.assert_not_called()


class TestStickyBitFallback(unittest.TestCase):
    """When the privileged target is unwritable, the /tmp fallback must cope
    with a sticky /tmp that forbids unlinking another user's link (issue 2)."""

    @patch("os.path.islink", return_value=False)
    @patch("os.path.lexists", return_value=True)
    def test_permission_error_retries_user_scoped_path(self, m_lex, m_islink):
        # target symlink fails (privileged path) -> fall back to /tmp; the
        # first /tmp candidate cannot be unlinked (sticky-bit, foreign owner),
        # so a user-scoped candidate is used instead.
        def fake_symlink(src, dst):
            if dst == "/dev/ttyUSB0":
                raise OSError("read-only fs")
            # /tmp candidates succeed.

        def fake_unlink(path):
            if path == "/tmp/ttyUSB0":
                raise PermissionError("sticky /tmp, not owner")

        with patch("os.symlink", create=True, side_effect=fake_symlink) as m_sym, \
                patch("os.unlink", create=True, side_effect=fake_unlink), \
                patch("getpass.getuser", return_value="alice"):
            result = create_symlink_with_fallback("/dev/pts/3", "/dev/ttyUSB0")

        self.assertEqual(result, "/tmp/ttyUSB0_alice")
        m_sym.assert_any_call("/dev/pts/3", "/tmp/ttyUSB0_alice")

    @patch("os.path.islink", return_value=False)
    @patch("os.path.lexists", return_value=False)
    def test_plain_tmp_fallback_when_no_permission_problem(self, m_lex, m_islink):
        def fake_symlink(src, dst):
            if dst == "/dev/ttyUSB0":
                raise OSError("read-only fs")

        with patch("os.symlink", create=True, side_effect=fake_symlink), \
                patch("os.unlink", create=True):
            result = create_symlink_with_fallback("/dev/pts/3", "/dev/ttyUSB0")

        self.assertEqual(result, "/tmp/ttyUSB0")

    @patch("os.path.islink", return_value=False)
    @patch("os.path.lexists", return_value=True)
    def test_getuser_failure_falls_through_to_pid(self, m_lex, m_islink):
        # If getpass.getuser() raises (no resolvable username), the user-scoped
        # candidate is skipped and the pid-scoped one is used.
        def fake_symlink(src, dst):
            if dst == "/dev/ttyUSB0":
                raise OSError("read-only fs")

        def fake_unlink(path):
            if path == "/tmp/ttyUSB0":
                raise PermissionError("sticky /tmp")

        with patch("os.symlink", create=True, side_effect=fake_symlink), \
                patch("os.unlink", create=True, side_effect=fake_unlink), \
                patch("getpass.getuser", side_effect=OSError("no username")), \
                patch("os.getpid", return_value=4242):
            result = create_symlink_with_fallback("/dev/pts/3", "/dev/ttyUSB0")

        self.assertEqual(result, "/tmp/ttyUSB0_4242")

    @patch("os.path.islink", return_value=False)
    @patch("os.path.lexists", return_value=True)
    def test_raises_when_every_candidate_denied(self, m_lex, m_islink):
        def fake_symlink(src, dst):
            if dst == "/dev/ttyUSB0":
                raise OSError("read-only fs")

        with patch("os.symlink", create=True, side_effect=fake_symlink), \
                patch("os.unlink", create=True,
                      side_effect=PermissionError("denied")), \
                patch("getpass.getuser", return_value="bob"):
            with self.assertRaises(PermissionError):
                create_symlink_with_fallback("/dev/pts/3", "/dev/ttyUSB0")


class TestGetPtySettings(unittest.TestCase):

    def test_8n1_9600(self):
        attrs = [0, 0, termios.CS8, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 9600)
        self.assertEqual(bs, 8)
        self.assertEqual(par, "N")
        self.assertEqual(sb, "1")

    def test_7e2_115200(self):
        cflag = termios.CS7 | termios.PARENB | termios.CSTOPB
        attrs = [0, 0, cflag, 0, 0, termios.B115200, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            baud, bs, par, sb = get_pty_settings(3)
        self.assertEqual(baud, 115200)
        self.assertEqual(bs, 7)
        self.assertEqual(par, "E")
        self.assertEqual(sb, "2")

    def test_odd_parity(self):
        cflag = termios.CS8 | termios.PARENB | termios.PARODD
        attrs = [0, 0, cflag, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            _, _, par, _ = get_pty_settings(3)
        self.assertEqual(par, "O")

    def test_space_parity(self):
        # CMSPAR (stick parity) without PARODD = space parity. CMSPAR is patched
        # in so the branch is exercised on the mock termios too (Windows CI).
        cmspar = 0x40000000
        cflag = termios.CS8 | termios.PARENB | cmspar
        attrs = [0, 0, cflag, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.CMSPAR", cmspar, create=True), \
                patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            _, _, par, _ = get_pty_settings(3)
        self.assertEqual(par, "S")

    def test_mark_parity(self):
        # CMSPAR with PARODD = mark parity.
        cmspar = 0x40000000
        cflag = termios.CS8 | termios.PARENB | termios.PARODD | cmspar
        attrs = [0, 0, cflag, 0, 0, termios.B9600, 0]
        with patch("com2tty.wsl.pty_manager.termios.CMSPAR", cmspar, create=True), \
                patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs):
            _, _, par, _ = get_pty_settings(3)
        self.assertEqual(par, "M")

    def test_returns_none_on_exception(self):
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr",
                   side_effect=Exception("bad fd")):
            self.assertEqual(get_pty_settings(99),
                             (None, None, None, None))



class TestSetRawMode(unittest.TestCase):
    """The emulated serial line must be raw, not a cooked login terminal."""

    def test_clears_cooked_flags_and_keeps_cflag(self):
        attrs = _cooked_attrs()
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr", return_value=attrs), \
                patch("com2tty.wsl.pty_manager.termios.tcsetattr") as m_set:
            set_raw_mode(7)

        m_set.assert_called_once()
        fd, when, new = m_set.call_args[0]
        self.assertEqual(fd, 7)
        self.assertEqual(when, termios.TCSANOW)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = new
        # Every cooked flag the device traffic would trip over is gone.
        for bit in (termios.ICRNL, termios.INLCR, termios.IGNCR, termios.IXON,
                    termios.ISTRIP, termios.BRKINT, termios.PARMRK,
                    termios.IGNBRK):
            self.assertFalse(iflag & bit)
        self.assertFalse(oflag & termios.OPOST)
        for bit in (termios.ECHO, termios.ECHONL, termios.ICANON,
                    termios.ISIG, termios.IEXTEN):
            self.assertFalse(lflag & bit)
        # Byte size / parity / baud are the bridge's to report, untouched here.
        self.assertEqual(cflag, termios.CS8 | termios.PARENB)
        self.assertEqual(ispeed, termios.B9600)
        self.assertEqual(ospeed, termios.B9600)
        # Raw reads: deliver each byte immediately, no inter-byte timer.
        self.assertEqual(cc[termios.VMIN], 1)
        self.assertEqual(cc[termios.VTIME], 0)

    def test_tcgetattr_failure_is_swallowed(self):
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr",
                   side_effect=OSError("no tty")), \
                patch("com2tty.wsl.pty_manager.termios.tcsetattr") as m_set:
            set_raw_mode(7)  # must not raise
        m_set.assert_not_called()

    def test_tcsetattr_failure_is_swallowed(self):
        with patch("com2tty.wsl.pty_manager.termios.tcgetattr",
                   return_value=_cooked_attrs()), \
                patch("com2tty.wsl.pty_manager.termios.tcsetattr",
                      side_effect=OSError("denied")):
            set_raw_mode(7)  # must not raise


class TestOpenPtyRawsTheSlave(unittest.TestCase):

    @patch("com2tty.wsl.pty_manager.set_raw_mode")
    @patch("os.ttyname", return_value="/dev/pts/3", create=True)
    @patch("os.openpty", return_value=(11, 12), create=True)
    def test_open_pty_sets_slave_raw(self, m_openpty, m_ttyname, m_raw):
        master_fd, slave_fd, slave_name = open_pty()
        self.assertEqual((master_fd, slave_fd, slave_name), (11, 12, "/dev/pts/3"))
        m_raw.assert_called_once_with(12)


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
