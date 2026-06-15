"""Tests for com2tty.windows.dashboard._remediation (copy-paste fixes)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.dashboard._remediation import (
    PSMISC_SETUP,
    PYTHON3_SETUP,
    UINPUT_SETUP,
    remediation_for_results,
    serial_dev_link,
)


class TestRemediationForResults(unittest.TestCase):

    def test_collects_known_warn_and_fail_fixes(self):
        results = [
            ("OK", "wsl.exe on PATH", ""),
            ("WARN", "/dev/uinput writable (gamepad --uinput)", "no"),
            ("FAIL", "python3 in WSL", "missing"),
            ("WARN", "fuser available in WSL", "install psmisc"),
        ]
        topics = remediation_for_results(results)
        self.assertIn(UINPUT_SETUP, topics)
        self.assertIn(PYTHON3_SETUP, topics)
        self.assertIn(PSMISC_SETUP, topics)

    def test_ignores_ok_and_skip_statuses(self):
        results = [("OK", "/dev/uinput writable", ""),
                   ("SKIP", "python3 in WSL", "")]
        self.assertEqual(remediation_for_results(results), [])

    def test_unknown_label_has_no_fix(self):
        self.assertEqual(
            remediation_for_results([("FAIL", "some other check", "x")]), [])

    def test_dedupes_repeated_topic(self):
        # Two failing checks that map to the same fix yield it only once.
        results = [("WARN", "uinput check A", "x"),
                   ("FAIL", "uinput check B", "y")]
        self.assertEqual(remediation_for_results(results), [UINPUT_SETUP])


class TestSerialDevLink(unittest.TestCase):

    def test_builds_ln_command_for_paths(self):
        title, explanation, commands = serial_dev_link("/tmp/ttyUSB1",
                                                       "/dev/ttyUSB1")
        self.assertIn("/dev/ttyUSB1", title)
        self.assertIn("/dev", explanation)
        # Reminds the user the device name (ttyUSB1) is replaceable.
        self.assertIn("ttyUSB1", explanation)
        self.assertEqual(commands, ["sudo ln -sf /tmp/ttyUSB1 /dev/ttyUSB1"])


if __name__ == "__main__":
    unittest.main()
