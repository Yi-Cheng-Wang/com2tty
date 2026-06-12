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
            distro=None,
            board="auto",
            wait=False,
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
            distro=None,
            board="auto",
            wait=False,
        )

    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM2", "--wait"])
    def test_cli_wait_flag(self, mock_run):
        main()
        self.assertTrue(mock_run.call_args[1]["wait"])

    @patch("com2tty.host.run_with_respawn")
    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM2", "--auto-respawn"])
    def test_cli_auto_respawn_uses_wrapper_and_implies_wait(self, mock_run,
                                                            mock_resp):
        main()
        mock_run.assert_not_called()
        mock_resp.assert_called_once()
        self.assertIs(mock_resp.call_args[0][0], mock_run)
        self.assertEqual(mock_resp.call_args[1]["port"], "COM2")
        self.assertTrue(mock_resp.call_args[1]["wait"])

    @patch("com2tty.host.run_multi_bridge")
    @patch("sys.argv", ["com2tty", "COM3", "COM5", "--auto-respawn"])
    def test_cli_auto_respawn_multi_port(self, mock_multi):
        main()
        self.assertTrue(mock_multi.call_args[1]["auto_respawn"])
        self.assertTrue(mock_multi.call_args[1]["wait"])

    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM2", "--distro", "Ubuntu-22.04",
                         "--board", "esp32"])
    def test_cli_distro_and_board(self, mock_run):
        main()
        self.assertEqual(mock_run.call_args[1]["distro"], "Ubuntu-22.04")
        self.assertEqual(mock_run.call_args[1]["board"], "esp32")

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


class TestCliMultiPort(unittest.TestCase):

    @patch("com2tty.host.run_multi_bridge")
    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM3", "COM5"])
    def test_two_ports_use_multi_bridge(self, mock_run, mock_multi):
        main()
        mock_run.assert_not_called()
        mock_multi.assert_called_once()
        self.assertEqual(mock_multi.call_args[1]["ports"], ["COM3", "COM5"])

    @patch("com2tty.host.run_multi_bridge")
    @patch("com2tty.cli.run_bridge")
    @patch("sys.argv", ["com2tty", "COM3"])
    def test_single_port_uses_run_bridge(self, mock_run, mock_multi):
        main()
        mock_multi.assert_not_called()
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[1]["port"], "COM3")

    @patch("com2tty.host.run_multi_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "COM3", "COM5"])
    def test_multi_keyboard_interrupt(self, mock_exit, mock_multi):
        mock_multi.side_effect = KeyboardInterrupt()
        main()
        mock_exit.assert_called_once_with(0)


class TestCliVersionAndList(unittest.TestCase):

    @patch("sys.argv", ["com2tty", "--version"])
    def test_version_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("com2tty.discovery.print_port_list")
    @patch("sys.argv", ["com2tty", "--list"])
    def test_list_calls_discovery(self, mock_list):
        main()
        mock_list.assert_called_once_with(as_json=False)

    @patch("com2tty.discovery.print_port_list")
    @patch("sys.argv", ["com2tty", "-l"])
    def test_list_short_flag(self, mock_list):
        main()
        mock_list.assert_called_once_with(as_json=False)

    @patch("com2tty.discovery.print_port_list")
    @patch("sys.argv", ["com2tty", "--list", "--json"])
    def test_list_json_flag(self, mock_list):
        main()
        mock_list.assert_called_once_with(as_json=True)

    @patch("com2tty.doctor.run_doctor", return_value=0)
    @patch("sys.argv", ["com2tty", "--doctor"])
    def test_doctor_dispatch(self, mock_doctor):
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 0)
        mock_doctor.assert_called_once_with(distro=None, rfc2217_port=4000)

    @patch("com2tty.doctor.run_doctor", return_value=1)
    @patch("sys.argv", ["com2tty", "--doctor", "--distro", "Ubuntu",
                        "--rfc2217-port", "5000"])
    def test_doctor_propagates_failure_and_options(self, mock_doctor):
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 1)
        mock_doctor.assert_called_once_with(distro="Ubuntu", rfc2217_port=5000)

    @patch("com2tty.cli.run_bridge")
    @patch("com2tty.discovery.print_port_list")
    @patch("sys.argv", ["com2tty", "--list"])
    def test_list_does_not_start_bridge(self, mock_list, mock_run):
        main()
        mock_run.assert_not_called()


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
            distro=None,
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
            distro=None,
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
    @patch("sys.argv", ["com2tty", "COM3", "--gamepad"])
    def test_gamepad_with_port_errors(self, mock_pad):
        # A positional COM port together with --gamepad used to be silently
        # ignored; it is now a hard argument error.
        with self.assertRaises(SystemExit):
            main()
        mock_pad.assert_not_called()

    @patch("com2tty.host.run_multi_gamepad_bridge")
    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad", "--pad-index", "0", "1"])
    def test_multiple_pad_indices_use_multi_bridge(self, mock_pad,
                                                   mock_multi):
        main()
        mock_pad.assert_not_called()
        mock_multi.assert_called_once()
        self.assertEqual(mock_multi.call_args[0][0], [0, 1])
        self.assertFalse(mock_multi.call_args[1]["auto_respawn"])

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad", "--pad-index", "1", "1"])
    def test_duplicate_pad_indices_error(self, mock_pad):
        with self.assertRaises(SystemExit):
            main()
        mock_pad.assert_not_called()

    @patch("com2tty.host.run_with_respawn")
    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.argv", ["com2tty", "--gamepad", "--auto-respawn"])
    def test_gamepad_auto_respawn_uses_wrapper(self, mock_pad, mock_resp):
        main()
        mock_pad.assert_not_called()
        mock_resp.assert_called_once()
        self.assertIs(mock_resp.call_args[0][0], mock_pad)
        self.assertEqual(mock_resp.call_args[1]["pad_index"], 0)

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_keyboard_interrupt(self, mock_exit, mock_pad):
        mock_pad.side_effect = KeyboardInterrupt()
        main()
        mock_exit.assert_called_once_with(0)

    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "--gamepad"])
    def test_gamepad_fatal_error_no_debug(self, mock_exit, mock_pad):
        mock_pad.side_effect = Exception("boom")
        main()
        mock_exit.assert_called_once_with(1)

    @patch("traceback.print_exc")
    @patch("com2tty.cli.run_gamepad_bridge")
    @patch("sys.exit")
    @patch("sys.argv", ["com2tty", "--gamepad", "--debug"])
    def test_gamepad_fatal_error_with_debug(self, mock_exit, mock_pad, mock_tb):
        mock_pad.side_effect = Exception("boom")
        main()
        mock_exit.assert_called_once_with(1)
        mock_tb.assert_called_once()


if __name__ == "__main__":
    unittest.main()
