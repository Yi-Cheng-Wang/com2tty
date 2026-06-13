"""Tests for com2tty.windows.wsl_process (wsl.exe invocation, path translation, environment checks)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import com2tty.windows.wsl_process as wp
from com2tty.windows.wsl_process import (
    _arm_kill_on_close,
    _assign_kill_on_close_job,
    check_wsl_environment,
    get_wsl_path,
    spawn_wsl_helper,
    terminate_wsl_helper,
    wsl_command,
)


class TestTerminateWslHelper(unittest.TestCase):

    def test_graceful_exit_closes_stdin_and_does_not_terminate(self):
        # The helper exits on the stdin EOF; no forced terminate is needed.
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        terminate_wsl_helper(proc, timeout=0.1)
        proc.stdin.close.assert_called_once()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_already_exited_is_left_alone(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        terminate_wsl_helper(proc)
        proc.terminate.assert_not_called()
        proc.stdin.close.assert_not_called()

    def test_handles_proc_without_stdin(self):
        # A helper spawned without a stdin pipe: skip the close, still wait.
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = None
        proc.wait.return_value = 0
        terminate_wsl_helper(proc, timeout=0.1)  # must not raise
        proc.terminate.assert_not_called()

    def test_stdin_close_failure_is_tolerated(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        proc.stdin.close.side_effect = OSError("pipe gone")
        terminate_wsl_helper(proc, timeout=0.1)  # must not raise
        proc.terminate.assert_not_called()

    def test_terminates_when_graceful_wait_times_out(self):
        import subprocess
        proc = MagicMock()
        proc.poll.return_value = None
        # First wait (graceful window) times out; second (post-terminate) exits.
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="wsl", timeout=0.1), 0]
        terminate_wsl_helper(proc, timeout=0.1)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_escalates_to_kill_when_terminate_also_times_out(self):
        import subprocess
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="wsl", timeout=0.1)
        terminate_wsl_helper(proc, timeout=0.1)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


def _ok_kernel32():
    """A kernel32 mock whose every Job-Object call succeeds."""
    k = MagicMock()
    k.CreateJobObjectW.return_value = 0x1000
    k.SetInformationJobObject.return_value = 1
    k.OpenProcess.return_value = 0x2000
    k.AssignProcessToJobObject.return_value = 1
    k.CloseHandle.return_value = 1
    return k


class TestAssignKillOnCloseJob(unittest.TestCase):

    def test_non_int_pid_returns_none(self):
        # A mocked Popen yields a non-int pid; the real ctypes path is skipped.
        self.assertIsNone(_assign_kill_on_close_job(MagicMock()))

    def test_success_uses_default_windll(self):
        k = _ok_kernel32()
        # create=True so this also runs on non-Windows CI, where ctypes has no
        # WinDLL attribute to patch.
        with patch.object(wp.ctypes, "WinDLL", return_value=k,
                          create=True) as m_windll:
            job = _assign_kill_on_close_job(1234)
        m_windll.assert_called_once()
        self.assertEqual(job, 0x1000)
        k.AssignProcessToJobObject.assert_called_once()

    def test_create_job_failure_returns_none(self):
        k = _ok_kernel32()
        k.CreateJobObjectW.return_value = 0
        self.assertIsNone(_assign_kill_on_close_job(1234, kernel32=k))

    def test_set_information_failure_closes_and_returns_none(self):
        k = _ok_kernel32()
        k.SetInformationJobObject.return_value = 0
        self.assertIsNone(_assign_kill_on_close_job(1234, kernel32=k))
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_open_process_failure_closes_and_returns_none(self):
        k = _ok_kernel32()
        k.OpenProcess.return_value = 0
        self.assertIsNone(_assign_kill_on_close_job(1234, kernel32=k))
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_assign_failure_closes_both_and_returns_none(self):
        k = _ok_kernel32()
        k.AssignProcessToJobObject.return_value = 0
        self.assertIsNone(_assign_kill_on_close_job(1234, kernel32=k))
        # Both the job and the opened process handle are released.
        k.CloseHandle.assert_any_call(0x1000)
        k.CloseHandle.assert_any_call(0x2000)


class _FakeProc:
    """A real object (not a MagicMock) so hasattr reflects actual state."""

    def __init__(self, pid=4321):
        self.pid = pid


class TestArmKillOnClose(unittest.TestCase):

    def test_noop_off_windows(self):
        proc = _FakeProc()
        with patch.object(wp.os, "name", "posix"):
            _arm_kill_on_close(proc)
        self.assertFalse(hasattr(proc, "_com2tty_kill_job"))

    def test_attaches_job_handle_on_windows(self):
        proc = _FakeProc()
        with patch.object(wp.os, "name", "nt"), \
             patch.object(wp, "_assign_kill_on_close_job", return_value=0x99):
            _arm_kill_on_close(proc)
        self.assertEqual(proc._com2tty_kill_job, 0x99)

    def test_no_attach_when_job_setup_returns_none(self):
        proc = _FakeProc()
        with patch.object(wp.os, "name", "nt"), \
             patch.object(wp, "_assign_kill_on_close_job", return_value=None):
            _arm_kill_on_close(proc)
        self.assertFalse(hasattr(proc, "_com2tty_kill_job"))

    def test_setup_exception_is_swallowed(self):
        proc = _FakeProc()
        with patch.object(wp.os, "name", "nt"), \
             patch.object(wp, "_assign_kill_on_close_job",
                          side_effect=OSError("denied")):
            _arm_kill_on_close(proc)  # must not raise


class TestSpawnWslHelper(unittest.TestCase):

    def test_spawns_and_arms_kill_on_close(self):
        proc = MagicMock()
        with patch("subprocess.Popen", return_value=proc) as m_pop, \
             patch.object(wp, "_arm_kill_on_close") as m_arm:
            result = spawn_wsl_helper(["wsl", "--exec", "true"])
        self.assertIs(result, proc)
        m_pop.assert_called_once()
        m_arm.assert_called_once_with(proc)


class TestGetWslPath(unittest.TestCase):

    def test_success(self):
        with patch("subprocess.run") as m:
            res = MagicMock()
            res.stdout = "/mnt/d/success\n"
            m.return_value = res
            self.assertEqual(get_wsl_path(r"D:\success"), "/mnt/d/success")

    def test_fallback(self):
        with patch("subprocess.run", side_effect=Exception("missing")):
            path = get_wsl_path(r"C:\Users\u\file.py")
            self.assertEqual(path, "/mnt/c/Users/u/file.py")



class TestWslCommand(unittest.TestCase):

    def test_no_distro(self):
        self.assertEqual(
            wsl_command(None, "python3", "-u", "/mnt/c/x.py"),
            ["wsl", "--exec", "python3", "-u", "/mnt/c/x.py"],
        )

    def test_with_distro(self):
        self.assertEqual(
            wsl_command("Ubuntu-22.04", "wslpath", "-u", r"C:\x"),
            ["wsl", "-d", "Ubuntu-22.04", "--exec", "wslpath", "-u", r"C:\x"],
        )

    def test_path_with_spaces_stays_single_argument(self):
        cmd = wsl_command(None, "python3", "-u", "/mnt/c/Program Files/x.py")
        self.assertIn("/mnt/c/Program Files/x.py", cmd)



class TestCheckWslEnvironment(unittest.TestCase):

    @patch("com2tty.windows.wsl_process.shutil.which", return_value=None)
    def test_wsl_missing(self, mock_which):
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("wsl.exe", str(ctx.exception))

    @patch("com2tty.windows.wsl_process.subprocess.run", side_effect=OSError("cannot start"))
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_distro_start_failure(self, mock_which, mock_run):
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment(distro="Ubuntu")
        self.assertIn("Ubuntu", str(ctx.exception))

    @patch("com2tty.windows.wsl_process.subprocess.run")
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_python3_missing(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="python3: command not found", stdout="")
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("python3", str(ctx.exception))

    @patch("com2tty.windows.wsl_process.subprocess.run")
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_python3_missing_no_output(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=127, stderr="", stdout="")
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment()
        self.assertIn("no output", str(ctx.exception))

    @patch("com2tty.windows.wsl_process.subprocess.run")
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_script_not_readable(self, mock_which, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            check_wsl_environment("/mnt/c/x/bridge.py")
        self.assertIn("automount", str(ctx.exception))

    @patch("com2tty.windows.wsl_process.subprocess.run")
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_success_with_script(self, mock_which, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]
        check_wsl_environment("/mnt/c/x/bridge.py")  # should not raise
        self.assertEqual(mock_run.call_count, 2)

    @patch("com2tty.windows.wsl_process.subprocess.run")
    @patch("com2tty.windows.wsl_process.shutil.which", return_value="C:\\wsl.exe")
    def test_success_without_script(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        check_wsl_environment()  # should not raise
        self.assertEqual(mock_run.call_count, 1)
