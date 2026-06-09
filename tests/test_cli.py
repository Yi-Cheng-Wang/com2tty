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


class TestCliGamepad(unittest.TestCase):

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_defaults(self, mock_pad):
        main()
        mock_pad.assert_called_once_with(
            pad_index=0,
            poll_hz=250,
            name="Microsoft X-Box 360 pad",
            use_uinput=False,
            tmp_path="/tmp/com2pad0",
        )

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad", "--pad-index", "2",
                         "--poll-hz", "500", "--pad-name", "Custom Pad",
                         "--uinput", "--wsl-pad", "/tmp/mypad"])
    def test_gamepad_options(self, mock_pad):
        main()
        mock_pad.assert_called_once_with(
            pad_index=2,
            poll_hz=500,
            name="Custom Pad",
            use_uinput=True,
            tmp_path="/tmp/mypad",
        )

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_defaults_to_tmp_not_uinput(self, mock_pad):
        main()
        self.assertFalse(mock_pad.call_args[1]["use_uinput"])

    @patch("com2tty.cli.run_bridge")
    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_does_not_call_serial(self, mock_pad, mock_serial):
        main()
        mock_pad.assert_called_once()
        mock_serial.assert_not_called()

    @patch("sys.argv", ["com2tty"])
    def test_missing_port_without_gamepad_errors(self):
        # argparse parser.error raises SystemExit
        with self.assertRaises(SystemExit):
            main()

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_keyboard_interrupt(self, mock_exit, mock_pad):
        mock_pad.side_effect = KeyboardInterrupt()
        main()
        mock_exit.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
