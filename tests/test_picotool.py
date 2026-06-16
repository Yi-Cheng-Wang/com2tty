"""Tests for com2tty.wsl.integrations.picotool (binary interception and self-healing)."""
import unittest
from unittest.mock import patch
import sys
import os


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


from com2tty.core.constants import PICOTOOL_OWNER_FILE
from com2tty.wsl.integrations.picotool import (
    cleanup_picotool_interceptor,
    intercepted_picotools,
    restore_orphaned_picotools,
    setup_picotool_interceptor,
)


class TestRestoreOrphanedPicotools(unittest.TestCase):

    def setUp(self):
        # No ownership marker: the orphan-recovery path itself is under test.
        patcher = patch("com2tty.wsl.integrations.picotool.read_pid_file", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    def test_restores_orphan(self, mock_glob, mock_islink, mock_exists,
                             mock_lexists, mock_remove, mock_rename):
        real = "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"
        mock_glob.return_value = [real]
        restore_orphaned_picotools()
        picotool = real[:-len(".real")]
        mock_remove.assert_called_once_with(picotool)
        mock_rename.assert_called_once_with(real, picotool)

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    def test_skips_when_real_binary_present(self, mock_glob, mock_islink,
                                            mock_exists, mock_rename):
        """If a genuine binary occupies the live path, do not clobber it."""
        mock_glob.return_value = [
            "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"]
        restore_orphaned_picotools()
        mock_rename.assert_not_called()

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    def test_restores_when_live_path_absent(self, mock_glob, mock_islink,
                                            mock_exists, mock_lexists,
                                            mock_remove, mock_rename):
        """Live picotool path is gone entirely: rename .real back, no remove."""
        real = "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"
        mock_glob.return_value = [real]
        restore_orphaned_picotools()
        mock_remove.assert_not_called()
        mock_rename.assert_called_once_with(real, real[:-len(".real")])

    @patch("com2tty.wsl.integrations.picotool.os.path.islink", side_effect=OSError("boom"))
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    def test_exception_swallowed(self, mock_glob, mock_exists, mock_islink):
        mock_glob.return_value = [
            "/home/u/.platformio/packages/tool-picotool-rp2040/picotool.real"]
        restore_orphaned_picotools()  # should not raise

    @patch("com2tty.wsl.integrations.picotool.glob.glob", return_value=[])
    def test_no_orphans(self, mock_glob):
        restore_orphaned_picotools()  # nothing to do



class TestSetupPicotoolInterceptor(unittest.TestCase):

    def setUp(self):
        intercepted_picotools.clear()

    @patch("com2tty.wsl.integrations.picotool.os.symlink")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists")
    @patch("com2tty.wsl.integrations.picotool.os.path.exists")
    @patch("com2tty.wsl.integrations.picotool.os.path.isfile", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    def test_successful_setup(self, mock_write, mock_glob,
                               mock_islink, mock_isfile, mock_exists,
                               mock_lexists, mock_rename, mock_remove,
                               mock_symlink):
        """Wrapper created, glob finds a picotool binary, rename + symlink succeed."""
        mock_glob.return_value = ["/home/user/.platformio/packages/tool-picotool-rp2040/picotool"]
        mock_exists.return_value = False  # real_path does not exist yet
        mock_lexists.return_value = False  # picotool_path does not exist after rename

        setup_picotool_interceptor(5001, "deadbeefcafe")

        # Wrapper written owner-only (0o700) with the session token embedded,
        # then the ownership marker -- both via the symlink-safe secure_write.
        self.assertEqual(mock_write.call_count, 2)
        wrapper_call, owner_call = mock_write.call_args_list
        self.assertEqual(wrapper_call.args[0], "/tmp/com2tty_picotool.py")
        self.assertIn("deadbeefcafe", wrapper_call.args[1])
        self.assertEqual(wrapper_call.kwargs.get("mode"), 0o700)
        self.assertEqual(owner_call.args[0], PICOTOOL_OWNER_FILE)
        self.assertEqual(owner_call.kwargs.get("mode"), 0o600)

        # picotool renamed and symlinked
        picotool_path = "/home/user/.platformio/packages/tool-picotool-rp2040/picotool"
        real_path = picotool_path + ".real"
        mock_rename.assert_called_once_with(picotool_path, real_path)
        mock_symlink.assert_called_once_with("/tmp/com2tty_picotool.py", picotool_path)
        self.assertEqual(len(intercepted_picotools), 1)
        self.assertEqual(intercepted_picotools[0], (picotool_path, real_path))

    @patch("com2tty.wsl.integrations.picotool.os.symlink")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.isfile", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    def test_existing_real_path_and_lexists(self, mock_write, mock_glob,
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

    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write",
           side_effect=PermissionError("cannot write"))
    def test_wrapper_creation_failure(self, mock_write, mock_glob):
        """If wrapper file creation fails, function returns early."""
        setup_picotool_interceptor(5001)

        mock_glob.assert_not_called()
        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=True)
    def test_skip_symlink_path(self, mock_islink, mock_write, mock_glob):
        """Paths that are already symlinks are skipped."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.isfile", return_value=False)
    def test_skip_non_file_path(self, mock_isfile, mock_islink, mock_write,
                                 mock_glob):
        """Paths that are not regular files are skipped."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.wsl.integrations.picotool.os.symlink", side_effect=OSError("symlink fail"))
    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.isfile", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    def test_rename_symlink_exception(self, mock_write, mock_glob,
                                       mock_islink, mock_isfile, mock_exists,
                                       mock_lexists, mock_rename, mock_symlink):
        """Exception during rename/symlink is caught and logged."""
        mock_glob.return_value = ["/some/path/picotool"]

        setup_picotool_interceptor(5001)

        # Exception caught, nothing added to intercepted_picotools
        self.assertEqual(len(intercepted_picotools), 0)

    @patch("com2tty.wsl.integrations.picotool.glob.glob", return_value=[])
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    def test_no_glob_matches(self, mock_write, mock_glob):
        """When glob returns no matches, nothing is intercepted."""
        setup_picotool_interceptor(5001)

        self.assertEqual(len(intercepted_picotools), 0)



class TestCleanupPicotoolInterceptor(unittest.TestCase):

    def setUp(self):
        intercepted_picotools.clear()

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=True)
    def test_successful_restore(self, mock_lexists, mock_exists,
                                 mock_remove, mock_rename):
        """Cleanup removes symlink, renames .real back, drops the owner marker."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()

        mock_remove.assert_any_call("/path/picotool")
        mock_remove.assert_any_call(PICOTOOL_OWNER_FILE)
        self.assertEqual(mock_remove.call_count, 2)
        mock_rename.assert_called_once_with("/path/picotool.real", "/path/picotool")

    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", side_effect=OSError("fail"))
    def test_exception_during_restore(self, mock_lexists):
        """Exception during cleanup is caught and logged, not raised."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()  # should not raise

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=False)
    def test_no_files_to_restore(self, mock_lexists, mock_exists,
                                  mock_remove, mock_rename):
        """When neither symlink nor .real exists, skip gracefully."""
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))

        cleanup_picotool_interceptor()

        # Only the ownership marker is removed; no picotool paths touched.
        mock_remove.assert_called_once_with(PICOTOOL_OWNER_FILE)
        mock_rename.assert_not_called()



class TestPicotoolOwnership(unittest.TestCase):

    def setUp(self):
        intercepted_picotools.clear()

    @patch("com2tty.wsl.integrations.picotool.os.symlink")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.os.path.isfile", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.islink", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.glob.glob",
           return_value=["/home/u/.platformio/packages/tool-picotool-x/picotool"])
    @patch("com2tty.wsl.integrations.picotool.secure_write")
    def test_owner_marker_write_failure_is_tolerated(
            self, mock_write, mock_glob, mock_islink, mock_isfile,
            mock_exists, mock_lexists, mock_rename, mock_remove, mock_symlink):
        # Wrapper write succeeds, owner-marker write fails: interception must
        # still be in effect.
        mock_write.side_effect = [None, PermissionError("denied")]
        setup_picotool_interceptor(5001)
        self.assertEqual(len(intercepted_picotools), 1)

    @patch("com2tty.wsl.integrations.picotool.os.rename")
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.os.path.exists", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.os.path.lexists", return_value=True)
    def test_cleanup_owner_marker_remove_failure_is_tolerated(
            self, mock_lexists, mock_exists, mock_remove, mock_rename):
        intercepted_picotools.append(("/path/picotool", "/path/picotool.real"))
        # First remove (the symlink) succeeds, second (owner marker) fails.
        mock_remove.side_effect = [None, OSError("locked")]
        cleanup_picotool_interceptor()  # should not raise

    @patch("com2tty.wsl.integrations.picotool.glob.glob")
    @patch("com2tty.wsl.integrations.picotool.pid_alive", return_value=True)
    @patch("com2tty.wsl.integrations.picotool.read_pid_file", return_value=99999999)
    def test_restore_skips_live_owner(self, mock_read, mock_alive, mock_glob):
        restore_orphaned_picotools()
        mock_glob.assert_not_called()

    @patch("com2tty.wsl.integrations.picotool.glob.glob", return_value=[])
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.pid_alive", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.read_pid_file", return_value=12345)
    def test_restore_reclaims_dead_owner_marker(self, mock_read, mock_alive,
                                                mock_remove, mock_glob):
        restore_orphaned_picotools()
        mock_remove.assert_called_once_with(PICOTOOL_OWNER_FILE)

    @patch("com2tty.wsl.integrations.picotool.glob.glob", return_value=[])
    @patch("com2tty.wsl.integrations.picotool.os.remove", side_effect=OSError("locked"))
    @patch("com2tty.wsl.integrations.picotool.pid_alive", return_value=False)
    @patch("com2tty.wsl.integrations.picotool.read_pid_file", return_value=12345)
    def test_restore_owner_marker_remove_failure_is_tolerated(
            self, mock_read, mock_alive, mock_remove, mock_glob):
        restore_orphaned_picotools()  # should not raise

    @patch("com2tty.wsl.integrations.picotool.glob.glob", return_value=[])
    @patch("com2tty.wsl.integrations.picotool.os.remove")
    @patch("com2tty.wsl.integrations.picotool.read_pid_file")
    def test_restore_reclaims_own_marker(self, mock_read, mock_remove,
                                         mock_glob):
        # An owner marker pointing at *this* process is stale by definition
        # (we have not intercepted anything yet at startup).
        mock_read.return_value = os.getpid()
        restore_orphaned_picotools()
        mock_remove.assert_called_once_with(PICOTOOL_OWNER_FILE)
