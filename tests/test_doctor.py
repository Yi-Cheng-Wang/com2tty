"""Tests for com2tty.windows.doctor (``com2tty --doctor`` environment self-check)."""
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.doctor import (
    OK, WARN, FAIL, SKIP,
    _run,
    check_wsl_exe,
    check_wsl_exec,
    check_python3,
    check_bridge_script,
    check_fuser,
    check_ports,
    check_leftovers,
    check_uinput,
    check_autoplay_marker,
    check_xinput,
    collect_doctor_results,
    run_doctor,
)


def _result(returncode=0, stdout="", stderr=""):
    res = MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


class TestRunHelper(unittest.TestCase):

    @patch("subprocess.run", return_value=_result(0, "out\n", "err\n"))
    def test_success_strips_output(self, mock_run):
        self.assertEqual(_run(["x"]), (0, "out", "err"))

    @patch("subprocess.run",
           side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30))
    def test_exception_returns_none_code(self, mock_run):
        rc, out, err = _run(["x"])
        self.assertIsNone(rc)
        self.assertIn("30", err)

    @patch("subprocess.run", return_value=_result(0, None, None))
    def test_none_streams_tolerated(self, mock_run):
        self.assertEqual(_run(["x"]), (0, "", ""))


class TestIndividualChecks(unittest.TestCase):

    @patch("shutil.which", return_value="C:\\Windows\\wsl.exe")
    def test_wsl_exe_found(self, mock_which):
        status, _, detail = check_wsl_exe()
        self.assertEqual(status, OK)
        self.assertIn("wsl.exe", detail)

    @patch("shutil.which", return_value=None)
    def test_wsl_exe_missing(self, mock_which):
        status, _, detail = check_wsl_exe()
        self.assertEqual(status, FAIL)
        self.assertIn("wsl --install", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "", ""))
    def test_wsl_exec_ok(self, mock_run):
        self.assertEqual(check_wsl_exec(None)[0], OK)

    @patch("com2tty.windows.doctor._run", return_value=(1, "", "unknown option"))
    def test_wsl_exec_fail_mentions_windows_version(self, mock_run):
        status, _, detail = check_wsl_exec(None)
        self.assertEqual(status, FAIL)
        self.assertIn("1903", detail)

    @patch("com2tty.windows.doctor._run", return_value=(None, "", ""))
    def test_wsl_exec_no_output_fail(self, mock_run):
        status, _, detail = check_wsl_exec(None)
        self.assertEqual(status, FAIL)
        self.assertIn("no output", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "Python 3.10.6", ""))
    def test_python3_ok(self, mock_run):
        status, _, detail = check_python3(None)
        self.assertEqual(status, OK)
        self.assertIn("3.10", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "", "Python 3.8.2"))
    def test_python3_version_on_stderr(self, mock_run):
        # Old CPython printed --version to stderr.
        status, _, detail = check_python3(None)
        self.assertEqual(status, OK)
        self.assertIn("3.8", detail)

    @patch("com2tty.windows.doctor._run", return_value=(127, "", "not found"))
    def test_python3_missing(self, mock_run):
        status, _, detail = check_python3(None)
        self.assertEqual(status, FAIL)
        self.assertIn("--distro", detail)

    @patch("com2tty.windows.doctor.get_wsl_path", return_value="/mnt/c/x/bridge.py")
    @patch("com2tty.windows.doctor._run", return_value=(0, "", ""))
    def test_bridge_script_readable(self, mock_run, mock_path):
        status, _, detail = check_bridge_script(None)
        self.assertEqual(status, OK)
        self.assertEqual(detail, "/mnt/c/x/bridge.py")

    @patch("com2tty.windows.doctor.get_wsl_path", return_value="/mnt/c/x/bridge.py")
    @patch("com2tty.windows.doctor._run", return_value=(1, "", ""))
    def test_bridge_script_unreadable(self, mock_run, mock_path):
        status, _, detail = check_bridge_script(None)
        self.assertEqual(status, FAIL)
        self.assertIn("automount", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "/usr/bin/fuser", ""))
    def test_fuser_present(self, mock_run):
        self.assertEqual(check_fuser(None)[0], OK)

    @patch("com2tty.windows.doctor._run", return_value=(1, "", ""))
    def test_fuser_missing_is_warning(self, mock_run):
        status, _, detail = check_fuser(None)
        self.assertEqual(status, WARN)
        self.assertIn("psmisc", detail)

    def test_ports_skipped_without_fuser(self):
        results = check_ports(None, 4000, fuser_ok=False)
        self.assertEqual([r[0] for r in results], [SKIP, SKIP])

    @patch("com2tty.windows.doctor._run", return_value=(1, "", ""))
    def test_ports_free(self, mock_run):
        results = check_ports(None, 4000, fuser_ok=True)
        self.assertEqual([r[0] for r in results], [OK, OK])
        # Both the RFC 2217 port and its +1 UF2 relay port are probed.
        probed = [c.args[0][-1] for c in mock_run.call_args_list]
        self.assertEqual(probed, ["4000/tcp", "4001/tcp"])

    @patch("com2tty.windows.doctor._run", return_value=(0, "1234", ""))
    def test_ports_in_use_is_warning(self, mock_run):
        results = check_ports(None, 4000, fuser_ok=True)
        self.assertEqual([r[0] for r in results], [WARN, WARN])
        self.assertIn("1234", results[0][2])

    @patch("com2tty.windows.doctor._run", return_value=(None, "", "boom"))
    def test_ports_probe_error_is_skip(self, mock_run):
        results = check_ports(None, 4000, fuser_ok=True)
        self.assertEqual([r[0] for r in results], [SKIP, SKIP])

    @patch("com2tty.windows.doctor._run", return_value=(0, "0 0", ""))
    def test_leftovers_none(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, OK)
        self.assertEqual(detail, "none")

    @patch("com2tty.windows.doctor._run", return_value=(0, "2 1", ""))
    def test_leftovers_found(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, WARN)
        self.assertIn("2 intercepted picotool", detail)
        self.assertIn("1 rc file", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "1 0", ""))
    def test_leftovers_only_picotool(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, WARN)
        self.assertIn("picotool", detail)
        self.assertNotIn("rc file", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "0 1", ""))
    def test_leftovers_only_rc(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, WARN)
        self.assertIn("rc file", detail)
        self.assertNotIn("picotool", detail)

    @patch("com2tty.windows.doctor._run", return_value=(1, "", "probe failed"))
    def test_leftovers_probe_failure_is_skip(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, SKIP)
        self.assertIn("probe failed", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "garbage", ""))
    def test_leftovers_unexpected_output_is_skip(self, mock_run):
        status, _, detail = check_leftovers(None)
        self.assertEqual(status, SKIP)
        self.assertIn("garbage", detail)

    @patch("com2tty.windows.doctor._run", return_value=(0, "", ""))
    def test_uinput_accessible(self, mock_run):
        self.assertEqual(check_uinput(None)[0], OK)

    @patch("com2tty.windows.doctor._run", return_value=(1, "", ""))
    def test_uinput_missing_is_warning(self, mock_run):
        status, _, detail = check_uinput(None)
        self.assertEqual(status, WARN)
        self.assertIn("--gamepad --uinput", detail)

    def test_autoplay_marker_absent(self):
        # conftest redirects tempfile.gettempdir to a per-test directory.
        self.assertEqual(check_autoplay_marker()[0], OK)

    def test_autoplay_marker_present(self):
        from com2tty.windows.os_hacks.autoplay import _autoplay_marker_path
        with open(_autoplay_marker_path(), "w") as f:
            f.write("{}")
        try:
            status, _, detail = check_autoplay_marker()
            self.assertEqual(status, WARN)
            self.assertIn("crashed", detail)
        finally:
            os.remove(_autoplay_marker_path())

    def test_xinput_skipped_off_windows(self):
        self.assertEqual(check_xinput(os_name="posix")[0], SKIP)

    @patch("com2tty.windows.gamepad_host._load_xinput")
    def test_xinput_ok(self, mock_load):
        self.assertEqual(check_xinput(os_name="nt")[0], OK)
        mock_load.assert_called_once()

    @patch("com2tty.windows.gamepad_host._load_xinput", side_effect=OSError("no DLL"))
    def test_xinput_missing_is_warning(self, mock_load):
        status, _, detail = check_xinput(os_name="nt")
        self.assertEqual(status, WARN)
        self.assertIn("no DLL", detail)


class TestRunDoctor(unittest.TestCase):
    """End-to-end composition of the checks, with each check stubbed."""

    def _patch_all(self, **overrides):
        defaults = {
            "check_wsl_exe": (OK, "wsl.exe on PATH", ""),
            "check_wsl_exec": (OK, "wsl --exec support", ""),
            "check_python3": (OK, "python3 in WSL", "Python 3.10"),
            "check_bridge_script": (OK, "bridge script readable", ""),
            "check_fuser": (OK, "fuser available", ""),
            "check_leftovers": (OK, "leftovers", "none"),
            "check_uinput": (OK, "/dev/uinput", ""),
            "check_autoplay_marker": (OK, "AutoPlay marker", "none"),
            "check_xinput": (OK, "XInput DLL", ""),
        }
        defaults.update(overrides)
        patchers = [patch(f"com2tty.windows.doctor.{name}", return_value=value)
                    for name, value in defaults.items()]
        patchers.append(patch(
            "com2tty.windows.doctor.check_ports",
            return_value=[(OK, "RFC 2217 port 4000 free", ""),
                          (OK, "UF2 relay port 4001 free", "")]))
        return patchers

    def _run_doctor(self, patchers, **kwargs):
        mocks = [p.start() for p in patchers]
        try:
            with patch("builtins.print") as mock_print:
                code = run_doctor(**kwargs)
            printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list
                                if c.args)
            return code, printed, mocks
        finally:
            for p in patchers:
                p.stop()

    def test_all_ok_exits_zero(self):
        code, printed, _ = self._run_doctor(self._patch_all())
        self.assertEqual(code, 0)
        self.assertIn("All checks passed.", printed)
        self.assertIn("[  OK] wsl.exe on PATH", printed)

    def test_warning_exits_zero_with_summary(self):
        code, printed, _ = self._run_doctor(self._patch_all(
            check_fuser=(WARN, "fuser available", "not found")))
        self.assertEqual(code, 0)
        self.assertIn("1 warning(s)", printed)
        self.assertIn("[WARN] fuser available -- not found", printed)

    def test_failure_exits_one(self):
        code, printed, _ = self._run_doctor(self._patch_all(
            check_python3=(FAIL, "python3 in WSL", "not found")))
        self.assertEqual(code, 1)
        self.assertIn("1 check(s) failed", printed)

    def test_missing_wsl_skips_dependent_checks(self):
        patchers = self._patch_all(
            check_wsl_exe=(FAIL, "wsl.exe on PATH", "not found"))
        code, printed, mocks = self._run_doctor(patchers)
        self.assertEqual(code, 1)
        # No WSL: none of the in-WSL checks may run.
        self.assertNotIn("python3 in WSL", printed)
        self.assertNotIn("RFC 2217 port", printed)
        # The host-side checks still run.
        self.assertIn("AutoPlay marker", printed)
        self.assertIn("XInput DLL", printed)

    def test_failed_wsl_exec_skips_dependent_checks(self):
        code, printed, _ = self._run_doctor(self._patch_all(
            check_wsl_exec=(FAIL, "wsl --exec support", "unsupported")))
        self.assertEqual(code, 1)
        self.assertNotIn("python3 in WSL", printed)

    def test_failed_python3_skips_leftover_probe(self):
        code, printed, _ = self._run_doctor(self._patch_all(
            check_python3=(FAIL, "python3 in WSL", "not found")))
        self.assertEqual(code, 1)
        self.assertNotIn("leftovers", printed)
        # The non-python checks in WSL still run.
        self.assertIn("/dev/uinput", printed)

    def test_distro_and_port_are_forwarded(self):
        patchers = self._patch_all()
        mocks = [p.start() for p in patchers]
        try:
            with patch("builtins.print"):
                run_doctor(distro="Ubuntu", rfc2217_port=5000)
            # check_ports is the last patcher in the list.
            mocks[-1].assert_called_once_with("Ubuntu", 5000, True)
        finally:
            for p in patchers:
                p.stop()


class TestCollectDoctorResults(unittest.TestCase):
    """The structured API behind both run_doctor and the dashboard's table."""

    @patch("com2tty.windows.doctor.check_xinput",
           return_value=(OK, "XInput DLL", ""))
    @patch("com2tty.windows.doctor.check_autoplay_marker",
           return_value=(OK, "AutoPlay marker", "none"))
    @patch("com2tty.windows.doctor.check_wsl_exe",
           return_value=(FAIL, "wsl.exe on PATH", "not found"))
    def test_returns_structured_tuples_and_gates_on_wsl(
            self, mock_wsl, mock_marker, mock_xinput):
        results = collect_doctor_results(distro=None, rfc2217_port=4000)
        # Every entry is a (status, label, detail) triple.
        for status, label, detail in results:
            self.assertIn(status, (OK, WARN, FAIL, SKIP))
            self.assertIsInstance(label, str)
        labels = [label for _, label, _ in results]
        # WSL missing: no in-WSL probe ran, but host-side checks still did.
        self.assertNotIn("python3 in WSL", labels)
        self.assertIn("XInput DLL", labels)
