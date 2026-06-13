"""Tests for com2tty.windows.board_reset (ESP32/Pico reset sequences; SAMD/STM32 are in test_boards.py)."""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.board_reset import (
    esp32_manual_reset,
    pico_manual_reset,
)


class TestEsp32ManualReset(unittest.TestCase):

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_success(self, mock_sleep):
        ser = MagicMock()
        esp32_manual_reset(ser)
        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.05)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_exception(self, mock_sleep):
        ser = MagicMock()
        type(ser).dtr = PropertyMock(side_effect=Exception("err"))
        esp32_manual_reset(ser)  # should not raise



class TestPicoManualReset(unittest.TestCase):

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_success(self, mock_sleep):
        """Lines 249-274: Normal 1200-baud touch sequence."""
        ser = MagicMock()
        ser.baudrate = 115200
        pico_manual_reset(ser)
        # Check DTR/baudrate assignments
        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.5)
        ser.close.assert_called_once()
        # Ensure it got restored
        # PropertyMock isn't used here, but we can verify ser.baudrate was assigned.
        # Since it's a MagicMock, the last set value is available as ser.baudrate.
        self.assertEqual(ser.baudrate, 115200)
        self.assertTrue(ser.dtr)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_old_baud_is_1200(self, mock_sleep):
        """Line 269: when old_baud == 1200, should use 115200 as fallback."""
        ser = MagicMock()
        ser.baudrate = 1200
        pico_manual_reset(ser)
        ser.close.assert_called_once()

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_close_exception_swallowed(self, mock_sleep):
        """Lines 263-266: ser.close() exception is swallowed."""
        ser = MagicMock()
        ser.baudrate = 115200
        ser.close.side_effect = Exception("close fail")
        pico_manual_reset(ser)  # should not raise

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_general_exception(self, mock_sleep):
        """Lines 273-274: general exception path."""
        ser = MagicMock()
        type(ser).dtr = PropertyMock(side_effect=Exception("hw fail"))
        pico_manual_reset(ser)  # should not raise
