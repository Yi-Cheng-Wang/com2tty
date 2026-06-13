"""Tests for com2tty.wsl.serial_app (the WSL serial helper entry point)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


import com2tty.wsl.integrations.shell_env as _shell_env
from com2tty.wsl.liveness import alive_file_path  # noqa: F401
from com2tty.wsl.serial_app import main

_real_get_fish_conf_path = _shell_env.get_fish_conf_path


@pytest.fixture(autouse=True)
def _no_real_fish_conf(monkeypatch):
    """Keep clean_rc/inject_rc tests from touching a real fish config.

    The fish snippet path resolves from the live HOME/SHELL; on a developer
    machine with fish installed these tests would otherwise write and delete
    a real ~/.config/fish/conf.d/com2tty.fish. Tests that exercise the fish
    behaviour re-patch get_fish_conf_path themselves.
    """
    monkeypatch.setattr(_shell_env, "get_fish_conf_path", lambda: None)



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
    @patch("com2tty.wsl.serial_app.get_pty_settings")
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
    @patch("com2tty.wsl.serial_app.inject_rc")
    @patch("com2tty.wsl.serial_app.clean_rc")
    @patch("com2tty.wsl.serial_app.cleanup_symlink")
    @patch("com2tty.wsl.serial_app.threading.Event")
    @patch("com2tty.wsl.serial_app.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.wsl.serial_app.get_pty_settings",
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

    # -- multi-port secondary: --no-env-setup ------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "4002", "--no-env-setup"])
    @patch("com2tty.wsl.serial_app.restore_orphaned_picotools")
    @patch("com2tty.wsl.serial_app.setup_picotool_interceptor")
    @patch("com2tty.wsl.serial_app.inject_rc")
    @patch("com2tty.wsl.serial_app.clean_rc")
    @patch("com2tty.wsl.serial_app.cleanup_symlink")
    @patch("com2tty.wsl.serial_app.threading.Event")
    @patch("com2tty.wsl.serial_app.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.wsl.serial_app.get_pty_settings",
           return_value=(None, None, None, None))
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("time.sleep")
    def test_no_env_setup_skips_injection_and_interception(
        self, mock_sleep, mock_close, mock_read, mock_select, mock_gps,
        mock_symlink, mock_lexists, mock_ttyname, mock_openpty,
        mock_thread_cls, mock_event_cls, mock_cleanup, mock_clean, mock_inj,
        mock_intercept, mock_restore,
    ):
        mock_evt = MagicMock()
        mock_evt.is_set.side_effect = [True, False, False]
        mock_event_cls.return_value = mock_evt
        mock_select.return_value = ([0], [], [])

        main()

        # No shell-rc or picotool changes for a secondary bridge...
        mock_restore.assert_not_called()
        mock_inj.assert_not_called()
        mock_intercept.assert_not_called()
        mock_clean.assert_not_called()
        # ...but the RFC 2217 / UF2 relay threads still start.
        mock_thread_cls.assert_called()

    # -- post-select rfc2217 check (line 464) ------------------------------

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "5000"])
    @patch("com2tty.wsl.serial_app.inject_rc")
    @patch("com2tty.wsl.serial_app.clean_rc")
    @patch("com2tty.wsl.serial_app.cleanup_symlink")
    @patch("com2tty.wsl.serial_app.threading.Event")
    @patch("com2tty.wsl.serial_app.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.wsl.serial_app.get_pty_settings",
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
    @patch("com2tty.wsl.serial_app.inject_rc")
    @patch("com2tty.wsl.serial_app.clean_rc")
    @patch("com2tty.wsl.serial_app.cleanup_symlink")
    @patch("com2tty.wsl.serial_app.threading.Event")
    @patch("com2tty.wsl.serial_app.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.wsl.serial_app.get_pty_settings",
           return_value=(None, None, None, None))
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    @patch("time.sleep")
    @patch("com2tty.wsl.serial_app.setup_picotool_interceptor")
    @patch("com2tty.wsl.serial_app.cleanup_picotool_interceptor")
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



class TestMainHeartbeat(unittest.TestCase):

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty",
                         "--rfc2217-port", "4400"])
    @patch("com2tty.wsl.serial_app.ALIVE_TOUCH_INTERVAL", -1.0)
    @patch("com2tty.wsl.serial_app.remove_alive_files")
    @patch("com2tty.wsl.serial_app.touch_alive_files")
    @patch("com2tty.wsl.serial_app.restore_orphaned_picotools")
    @patch("com2tty.wsl.serial_app.setup_picotool_interceptor")
    @patch("com2tty.wsl.serial_app.inject_rc")
    @patch("com2tty.wsl.serial_app.clean_rc")
    @patch("com2tty.wsl.serial_app.cleanup_symlink")
    @patch("com2tty.wsl.serial_app.threading.Thread")
    @patch("os.openpty", create=True, return_value=(3, 4))
    @patch("os.ttyname", create=True, return_value="/dev/pts/1")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", create=True)
    @patch("com2tty.wsl.serial_app.get_pty_settings",
           return_value=(None, None, None, None))
    @patch("select.select")
    @patch("os.read", return_value=b"")
    @patch("os.close")
    def test_heartbeat_touched_in_loop_and_removed_on_exit(
            self, mock_close, mock_read, mock_select, mock_gps, mock_symlink,
            mock_lexists, mock_ttyname, mock_openpty, mock_thread_cls,
            mock_cleanup, mock_clean, mock_inj, mock_intercept, mock_restore,
            mock_touch, mock_remove_alive):
        mock_select.return_value = ([0], [], [])  # immediate stdin EOF

        main()

        # Touched at startup and again by the (interval-forced) loop pass.
        self.assertGreaterEqual(mock_touch.call_count, 2)
        mock_touch.assert_called_with([4400, 4401])
        mock_remove_alive.assert_called_once_with([4400, 4401])


if __name__ == "__main__":
    unittest.main()
