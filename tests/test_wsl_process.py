"""Tests for com2tty.windows.wsl_process (wsl.exe invocation, path translation, environment checks)."""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.wsl_process import (
    check_wsl_environment,
    get_wsl_path,
    wsl_command,
)


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
