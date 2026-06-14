"""Tests for com2tty.windows.dashboard.run_dashboard (the TUI entry point)."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.dashboard import run_dashboard


class TestRunDashboard(unittest.TestCase):

    def test_launches_app_and_returns_zero(self):
        fake_app = MagicMock()
        with patch("com2tty.windows.dashboard.app.DashboardApp",
                   return_value=fake_app) as mock_cls:
            rc = run_dashboard(distro="Ubuntu", rfc2217_port=5000, debug=True)
        self.assertEqual(rc, 0)
        mock_cls.assert_called_once_with(distro="Ubuntu", rfc2217_port=5000,
                                         debug=True)
        fake_app.run.assert_called_once_with()

    def test_missing_textual_returns_one_with_hint(self):
        # Make importing the app module fail like a missing 'textual'.
        with patch.dict(sys.modules,
                        {"com2tty.windows.dashboard.app": None}):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                rc = run_dashboard()
        self.assertEqual(rc, 1)
        self.assertIn("pip install textual", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
