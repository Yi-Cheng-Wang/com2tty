import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.cli import main


class TestCli(unittest.TestCase):

    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM1", "-b", "115200", "-w", "/dev/ttyUSB1",
                         "--debug"])
    def test_cli_parsing(self, mock_run):
        main()
        mock_run.assert_called_once_with(
            port="COM1",
            baud="115200",
            wsl_tty="/dev/ttyUSB1",
            bytesize=8,
            parity="N",
            stopbits=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            rfc2217_port=4000,
        )

    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM2"])
    def test_cli_defaults(self, mock_run):
        main()
        mock_run.assert_called_once_with(
            port="COM2",
            baud="auto",
            wsl_tty="/tmp/ttyUSB0",
            bytesize=8,
            parity="N",
            stopbits=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            rfc2217_port=4000,
        )

    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM2", "--rfc2217-port", "5000"])
    def test_cli_custom_rfc2217_port(self, mock_run):
        main()
        self.assertEqual(mock_run.call_args[1]["rfc2217_port"], 5000)

    @patch("com2tty.cli.run_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "COM2"])
    def test_cli_keyboard_interrupt(self, mock_exit, mock_run):
        mock_run.side_effect = KeyboardInterrupt()
        main()
        mock_exit.assert_called_once_with(0)

    @patch("com2tty.cli.run_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "COM2", "--debug"])
    def test_cli_fatal_error(self, mock_exit, mock_run):
        mock_run.side_effect = Exception("Fatal runtime error")
        main()
        mock_exit.assert_called_once_with(1)

    @patch("com2tty.cli.run_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "COM2"])
    def test_cli_fatal_error_no_debug(self, mock_exit, mock_run):
        mock_run.side_effect = Exception("err")
        main()
        mock_exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
