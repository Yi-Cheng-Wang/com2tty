"""Tests for com2tty.windows.discovery (the --list port enumeration)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.discovery import (
    _busid,
    collect_ports,
    format_port_table,
    print_port_list,
)


def _port(device, description="", vid=None, pid=None, location=None,
          serial_number=None):
    return SimpleNamespace(device=device, description=description, vid=vid,
                           pid=pid, location=location,
                           serial_number=serial_number)


class TestBusid(unittest.TestCase):

    def test_strips_interface_suffix(self):
        self.assertEqual(_busid("1-6:x.0"), "1-6")

    def test_plain_location_unchanged(self):
        self.assertEqual(_busid("2-6"), "2-6")

    def test_empty_and_none(self):
        self.assertEqual(_busid(""), "")
        self.assertEqual(_busid(None), "")


class TestCollectPorts(unittest.TestCase):

    @patch("serial.tools.list_ports.comports")
    def test_usb_port_fields(self, mock_comports):
        mock_comports.return_value = [
            _port("COM17", "USB Serial Device (COM17)", vid=0x2E8A,
                  pid=0xF00F, location="1-6:x.0",
                  serial_number="98C4FFA253A63FB7"),
        ]
        rows = collect_ports()
        self.assertEqual(rows, [{
            "device": "COM17",
            "description": "USB Serial Device (COM17)",
            "vid_pid": "2E8A:F00F",
            "busid": "1-6",
            "serial_number": "98C4FFA253A63FB7",
            "board": "pico",
        }])

    @patch("serial.tools.list_ports.comports")
    def test_non_usb_port_has_empty_fields(self, mock_comports):
        mock_comports.return_value = [
            _port("COM3", "Bluetooth link (COM3)"),
        ]
        rows = collect_ports()
        self.assertEqual(rows[0]["vid_pid"], "")
        self.assertEqual(rows[0]["busid"], "")
        self.assertEqual(rows[0]["serial_number"], "")
        self.assertEqual(rows[0]["board"], "unknown")

    @patch("serial.tools.list_ports.comports")
    def test_numeric_port_order(self, mock_comports):
        mock_comports.return_value = [
            _port("COM17"), _port("COM3"), _port("COM4"),
        ]
        rows = collect_ports()
        self.assertEqual([r["device"] for r in rows],
                         ["COM3", "COM4", "COM17"])

    @patch("serial.tools.list_ports.comports")
    def test_none_description_becomes_empty(self, mock_comports):
        mock_comports.return_value = [_port("COM5", description=None)]
        self.assertEqual(collect_ports()[0]["description"], "")


class TestFormatPortTable(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(format_port_table([]), ["No serial ports found."])

    def test_header_separator_and_alignment(self):
        rows = [{
            "device": "COM17", "description": "USB Serial Device",
            "vid_pid": "2E8A:F00F", "busid": "1-6",
            "serial_number": "98C4FFA253A63FB7", "board": "pico",
        }]
        lines = format_port_table(rows)
        self.assertEqual(len(lines), 3)  # header, separator, one row
        self.assertTrue(lines[0].startswith("Device"))
        self.assertTrue(set(lines[1]) <= {"-", " "})
        self.assertIn("COM17", lines[2])
        self.assertIn("2E8A:F00F", lines[2])
        self.assertIn("1-6", lines[2])
        self.assertIn("pico", lines[2])

    def test_missing_values_render_as_dash(self):
        rows = [{
            "device": "COM3", "description": "Bluetooth link",
            "vid_pid": "", "busid": "", "serial_number": "",
            "board": "unknown",
        }]
        line = format_port_table(rows)[2]
        self.assertIn("-", line)
        self.assertIn("unknown", line)


class TestPrintPortList(unittest.TestCase):

    @patch("serial.tools.list_ports.comports")
    def test_prints_each_line(self, mock_comports):
        mock_comports.return_value = [
            _port("COM17", "USB Serial Device", vid=0x2E8A, pid=0xF00F,
                  location="1-6:x.0", serial_number="SER123"),
        ]
        with patch("builtins.print") as mock_print:
            print_port_list()
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("COM17", printed)
        self.assertIn("1-6", printed)

    @patch("serial.tools.list_ports.comports", return_value=[])
    def test_prints_placeholder_when_empty(self, mock_comports):
        with patch("builtins.print") as mock_print:
            print_port_list()
        mock_print.assert_called_once_with("No serial ports found.")

    @patch("serial.tools.list_ports.comports")
    def test_json_output(self, mock_comports):
        import json
        mock_comports.return_value = [
            _port("COM17", "USB Serial Device", vid=0x2E8A, pid=0xF00F,
                  location="1-6:x.0", serial_number="SER123"),
        ]
        with patch("builtins.print") as mock_print:
            print_port_list(as_json=True)
        mock_print.assert_called_once()
        data = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["device"], "COM17")
        self.assertEqual(data[0]["vid_pid"], "2E8A:F00F")
        self.assertEqual(data[0]["board"], "pico")

    @patch("serial.tools.list_ports.comports", return_value=[])
    def test_json_output_empty_is_valid_json(self, mock_comports):
        import json
        with patch("builtins.print") as mock_print:
            print_port_list(as_json=True)
        self.assertEqual(json.loads(mock_print.call_args[0][0]), [])


if __name__ == "__main__":
    unittest.main()
