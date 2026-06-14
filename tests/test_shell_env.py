"""Tests for com2tty.wsl.integrations.shell_env (rc-file and fish env injection)."""
import unittest
from unittest.mock import patch
import sys
import os
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


import com2tty.wsl.integrations.shell_env as _shell_env
from com2tty.wsl.integrations.shell_env import (
    _atomic_write_lines,
    _marker_pid,
    clean_fish_conf,
    clean_rc,
    get_rc_files,
    inject_fish_conf,
    inject_rc,
    marker_end,
    marker_start,
)

# This session's own (test-process) marker lines; clean_rc with no own_pid
# removes any block, so most existing tests can keep composing with these.
MARKER_START = marker_start()
MARKER_END = marker_end()

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



class TestAtomicWriteLines(unittest.TestCase):

    def test_creates_file_when_absent(self):
        # os.stat on a missing path raises -> orig_mode falls back to None,
        # and the file is created from scratch.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "new.bashrc")
            _atomic_write_lines(path, ["a\n", "b\n"])
            with open(path) as f:
                self.assertEqual(f.read(), "a\nb\n")

    def test_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".bashrc")
            with open(path, "w") as f:
                f.write("old\n")
            os.chmod(path, 0o640)
            # Read back what the platform actually stored (Windows ignores the
            # group/other bits), so the assertion is portable.
            expected = os.stat(path).st_mode & 0o777
            _atomic_write_lines(path, ["new\n"])
            self.assertEqual(os.stat(path).st_mode & 0o777, expected)

    def test_cleans_up_temp_and_reraises_on_failure(self):
        captured = {}
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*a, **k):
            fd, tmp = real_mkstemp(*a, **k)
            captured["tmp"] = tmp
            return fd, tmp

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".bashrc")
            with open(path, "w") as f:
                f.write("old\n")
            with patch("com2tty.wsl.integrations.shell_env.tempfile.mkstemp",
                       side_effect=spy_mkstemp), \
                 patch("com2tty.wsl.integrations.shell_env.os.replace",
                       side_effect=OSError("nope")):
                with self.assertRaises(OSError):
                    _atomic_write_lines(path, ["new\n"])
            # The temp file must not be left behind, and the original is intact.
            self.assertFalse(os.path.exists(captured["tmp"]))
            with open(path) as f:
                self.assertEqual(f.read(), "old\n")

    def test_temp_unlink_failure_is_swallowed_but_original_error_raised(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".bashrc")
            with open(path, "w") as f:
                f.write("old\n")
            with patch("com2tty.wsl.integrations.shell_env.os.replace",
                       side_effect=OSError("replace failed")), \
                 patch("com2tty.wsl.integrations.shell_env.os.unlink",
                       side_effect=OSError("unlink failed")):
                # The cleanup unlink also fails, but that is swallowed and the
                # original replace failure still propagates.
                with self.assertRaises(OSError):
                    _atomic_write_lines(path, ["new\n"])


class TestGetRcFiles(unittest.TestCase):

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.exists", return_value=False)
    def test_returns_bashrc_path(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".bashrc"))

    @patch.dict(os.environ, {"SHELL": "/usr/bin/zsh"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.exists", return_value=False)
    def test_includes_zshrc_for_zsh_shell(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].endswith(".zshrc"))

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.exists", return_value=True)
    def test_includes_zshrc_when_file_exists(self, mock_exists):
        paths = get_rc_files()
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].endswith(".zshrc"))



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
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]):
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
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]):
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
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]):
                clean_rc()
            with open(path) as f:
                self.assertEqual(f.read(), "keep me\n")
        finally:
            os.unlink(path)

    def test_skips_nonexistent_file(self):
        with patch("com2tty.wsl.integrations.shell_env.get_rc_files",
                    return_value=["/no/such/file"]):
            clean_rc()  # should not raise

    def test_handles_io_exception(self):
        path = self._make_tmp("x\n")
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]), \
                 patch("builtins.open", side_effect=PermissionError("no")):
                clean_rc()  # logs warning, does not raise
        finally:
            os.unlink(path)



class TestInjectRc(unittest.TestCase):

    def test_injects_env_vars(self):
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bashrc", delete=False
        )
        f.write("old\n")
        f.close()
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[f.name]):
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
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[f.name]):
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
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[f.name]):
                inject_rc(4000, "/tmp/ttyACM5")
            with open(f.name) as fh:
                text = fh.read()
            self.assertIn("PLATFORMIO_MONITOR_PORT=/tmp/ttyACM5", text)
        finally:
            os.unlink(f.name)

    def test_handles_write_exception(self):
        with patch("com2tty.wsl.integrations.shell_env.get_rc_files",
                    return_value=["/no/such/dir/bashrc"]), \
             patch("com2tty.wsl.integrations.shell_env.clean_rc"):
            inject_rc(4000)  # should not raise

    def test_append_is_atomic(self):
        # The block must be written through _atomic_write_lines, never an
        # in-place append that a crash could truncate (issue 5).
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".bashrc", delete=False)
        f.write("old\n")
        f.close()
        try:
            # clean_rc (called first) is stubbed so the only atomic write left
            # is the injection itself.
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files",
                       return_value=[f.name]), \
                 patch("com2tty.wsl.integrations.shell_env.clean_rc"), \
                 patch("com2tty.wsl.integrations.shell_env._atomic_write_lines") as m_atomic:
                inject_rc(4000)
            m_atomic.assert_called_once()
            written_path, written_lines = m_atomic.call_args.args
            self.assertEqual(written_path, f.name)
            self.assertEqual(written_lines[0], "old\n")
            self.assertTrue(any(MARKER_START in ln for ln in written_lines))
        finally:
            os.unlink(f.name)

    def test_preserves_existing_content_exactly(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".bashrc", delete=False)
        f.write("line1\nline2\n")
        f.close()
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files",
                       return_value=[f.name]):
                inject_rc(4000)
            with open(f.name) as fh:
                text = fh.read()
            self.assertTrue(text.startswith("line1\nline2\n"))
            self.assertIn(MARKER_END, text)
        finally:
            os.unlink(f.name)



class TestFishConf(unittest.TestCase):

    @patch.dict(os.environ, {"SHELL": "/usr/bin/fish"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.isdir", return_value=False)
    def test_path_for_fish_shell(self, mock_isdir):
        path = _real_get_fish_conf_path()
        self.assertTrue(path.endswith(os.path.join("conf.d", "com2tty.fish")))

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.isdir", return_value=True)
    def test_path_when_fish_dir_exists(self, mock_isdir):
        self.assertIsNotNone(_real_get_fish_conf_path())

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    @patch("com2tty.wsl.integrations.shell_env.os.path.isdir", return_value=False)
    def test_no_path_without_fish(self, mock_isdir):
        self.assertIsNone(_real_get_fish_conf_path())

    def test_inject_writes_set_gx_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conf.d", "com2tty.fish")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path):
                inject_fish_conf(4000, "/tmp/ttyACM5")
            with open(path) as f:
                content = f.read()
            self.assertIn("set -gx PLATFORMIO_UPLOAD_PORT rfc2217://127.0.0.1:4000",
                          content)
            self.assertIn("set -gx PLATFORMIO_MONITOR_PORT /tmp/ttyACM5", content)

    def test_inject_noop_without_fish(self):
        with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=None):
            inject_fish_conf(4000)  # should not raise, nothing to assert

    def test_inject_write_failure_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conf.d", "com2tty.fish")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path), \
                 patch("builtins.open", side_effect=OSError("read-only")):
                inject_fish_conf(4000)  # should not raise

    def test_clean_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "com2tty.fish")
            with open(path, "w") as f:
                f.write("set -gx X 1\n")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path):
                clean_fish_conf()
            self.assertFalse(os.path.exists(path))

    def test_clean_noop_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "com2tty.fish")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path):
                clean_fish_conf()  # should not raise

    def test_clean_noop_without_fish(self):
        with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=None):
            clean_fish_conf()  # should not raise

    def test_clean_remove_failure_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "com2tty.fish")
            with open(path, "w") as f:
                f.write("x\n")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path), \
                 patch("com2tty.wsl.integrations.shell_env.os.remove", side_effect=OSError("busy")):
                clean_fish_conf()  # should not raise

    def test_clean_rc_also_cleans_fish(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "com2tty.fish")
            with open(path, "w") as f:
                f.write("x\n")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=path), \
                 patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[]):
                clean_rc()
            self.assertFalse(os.path.exists(path))

    def test_inject_rc_also_writes_fish(self):
        with tempfile.TemporaryDirectory() as d:
            fish_path = os.path.join(d, "conf.d", "com2tty.fish")
            rc_path = os.path.join(d, ".bashrc")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path", return_value=fish_path), \
                 patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[rc_path]):
                inject_rc(4000, "/tmp/ttyUSB0")
            self.assertTrue(os.path.exists(fish_path))



class TestCleanRcOwnership(unittest.TestCase):

    def _make_tmp(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".bashrc",
                                        delete=False)
        f.write(content)
        f.close()
        return f.name

    def _three_block_content(self, other_pid=424242):
        own = marker_start(os.getpid())
        other = marker_start(other_pid)
        legacy_start = "# === COM2TTY INJECTION START ==="
        legacy_end = "# === COM2TTY INJECTION END ==="
        return (
            "user line\n"
            f"{other}\nexport OTHER=1\n{marker_end()}\n"
            f"{own}\nexport OWN=2\n{marker_end()}\n"
            f"{legacy_start}\nexport LEGACY=3\n{legacy_end}\n"
            "tail\n"
        )

    def test_preserves_other_live_session_block(self):
        path = self._make_tmp(self._three_block_content())
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]), \
                 patch("com2tty.wsl.integrations.shell_env.pid_alive", return_value=True):
                clean_rc(own_pid=os.getpid())
            with open(path) as f:
                text = f.read()
            self.assertIn("export OTHER=1", text)   # live session: kept
            self.assertIn(marker_start(424242), text)
            self.assertNotIn("export OWN=2", text)  # own block: removed
            self.assertNotIn("export LEGACY=3", text)  # legacy: removed
            self.assertIn("user line", text)
            self.assertIn("tail", text)
            # Exactly one start/end marker pair survives.
            self.assertEqual(text.count("COM2TTY INJECTION START"), 1)
            self.assertEqual(text.count("COM2TTY INJECTION END"), 1)
        finally:
            os.unlink(path)

    def test_removes_dead_session_block(self):
        path = self._make_tmp(self._three_block_content())
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]), \
                 patch("com2tty.wsl.integrations.shell_env.pid_alive", return_value=False):
                clean_rc(own_pid=os.getpid())
            with open(path) as f:
                text = f.read()
            self.assertNotIn("COM2TTY", text)  # every block reclaimed
            self.assertIn("user line", text)
            self.assertIn("tail", text)
        finally:
            os.unlink(path)

    def test_full_clean_removes_even_live_blocks(self):
        path = self._make_tmp(self._three_block_content())
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]), \
                 patch("com2tty.wsl.integrations.shell_env.pid_alive", return_value=True):
                clean_rc()  # no own_pid: full cleanup
            with open(path) as f:
                text = f.read()
            self.assertNotIn("COM2TTY", text)
        finally:
            os.unlink(path)

    def test_inject_rc_tags_block_with_own_pid(self):
        path = self._make_tmp("old\n")
        try:
            with patch("com2tty.wsl.integrations.shell_env.get_rc_files", return_value=[path]):
                inject_rc(4000)
            with open(path) as f:
                text = f.read()
            self.assertIn(f"[pid={os.getpid()}]", text)
        finally:
            os.unlink(path)



class TestFishConfOwnership(unittest.TestCase):

    def _write(self, d, pid):
        path = os.path.join(d, "com2tty.fish")
        with open(path, "w") as f:
            f.write(f"# Written by com2tty [pid={pid}]; removed automatically"
                    " when it exits.\nset -gx PLATFORMIO_UPLOAD_PORT x\n")
        return path

    def test_preserves_other_live_session_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, 424242)
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path",
                       return_value=path), \
                 patch("com2tty.wsl.integrations.shell_env.pid_alive", return_value=True):
                clean_fish_conf(own_pid=os.getpid())
            self.assertTrue(os.path.exists(path))

    def test_removes_dead_session_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, 424242)
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path",
                       return_value=path), \
                 patch("com2tty.wsl.integrations.shell_env.pid_alive", return_value=False):
                clean_fish_conf(own_pid=os.getpid())
            self.assertFalse(os.path.exists(path))

    def test_removes_own_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, os.getpid())
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path",
                       return_value=path):
                clean_fish_conf(own_pid=os.getpid())
            self.assertFalse(os.path.exists(path))

    def test_unreadable_snippet_is_still_removed(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, 424242)
            real_open = open

            def fail_on_snippet(p, *a, **kw):
                if p == path:
                    raise OSError("unreadable")
                return real_open(p, *a, **kw)

            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path",
                       return_value=path), \
                 patch("builtins.open", side_effect=fail_on_snippet):
                clean_fish_conf(own_pid=os.getpid())
            self.assertFalse(os.path.exists(path))

    def test_inject_fish_conf_records_pid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conf.d", "com2tty.fish")
            with patch("com2tty.wsl.integrations.shell_env.get_fish_conf_path",
                       return_value=path):
                inject_fish_conf(4000)
            with open(path) as f:
                first_line = f.readline()
            self.assertEqual(_marker_pid(first_line), os.getpid())
