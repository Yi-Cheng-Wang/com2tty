"""Tests for com2tty.boards: VID classification and the reset sequences
added for the SAMD/AVR, nRF52, and STM32 families. The sequences shared
with the original host module (esp32/pico) are exercised in test_host.py."""
import os
import queue
import sys
import threading
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.core.boards import (
    BOARD_CHOICES,
    BOARD_LABELS,
    UF2_FAMILIES,
    classify_vid,
)
from com2tty.windows.board_reset import (
    samd_touch_reset,
    stm32_manual_reset,
)


class TestClassifyVid(unittest.TestCase):

    def test_none_vid_is_unknown(self):
        self.assertEqual(classify_vid(None), "unknown")

    def test_unmapped_vid_is_unknown(self):
        self.assertEqual(classify_vid(0xDEAD), "unknown")

    def test_known_families(self):
        self.assertEqual(classify_vid(0x2E8A), "pico")
        self.assertEqual(classify_vid(0x303A), "esp32")
        self.assertEqual(classify_vid(0x239A), "nrf52")
        self.assertEqual(classify_vid(0x2341), "samd")
        self.assertEqual(classify_vid(0x0483), "stm32")

    def test_uf2_families_and_labels_consistent(self):
        for family in UF2_FAMILIES:
            self.assertIn(family, BOARD_LABELS)
        for choice in BOARD_CHOICES:
            if choice not in ("auto", "none"):
                self.assertIn(choice, BOARD_LABELS)


class TestSamdTouchReset(unittest.TestCase):

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_sequence(self, mock_sleep):
        ser = MagicMock()
        ser.baudrate = 115200
        samd_touch_reset(ser)
        # Touch happened at 1200 baud, then the original rate was restored
        # while the port was closed.
        ser.close.assert_called_once()
        self.assertEqual(ser.baudrate, 115200)
        self.assertTrue(ser.dtr)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_restores_default_when_old_baud_was_1200(self, mock_sleep):
        ser = MagicMock()
        ser.baudrate = 1200
        samd_touch_reset(ser)
        self.assertEqual(ser.baudrate, 115200)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_close_failure_is_tolerated(self, mock_sleep):
        ser = MagicMock()
        ser.baudrate = 9600
        ser.close.side_effect = OSError("already gone")
        samd_touch_reset(ser)  # should not raise
        self.assertEqual(ser.baudrate, 9600)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_exception_is_logged_not_raised(self, mock_sleep):
        ser = MagicMock()
        type(ser).dtr = PropertyMock(side_effect=OSError("port died"))
        samd_touch_reset(ser)  # should not raise


class TestStm32ManualReset(unittest.TestCase):

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_pulse_sequence(self, mock_sleep):
        ser = MagicMock()
        stm32_manual_reset(ser)
        # Reset released and BOOT0 de-asserted at the end of the pulse.
        self.assertFalse(ser.rts)
        self.assertFalse(ser.dtr)

    @patch("com2tty.windows.board_reset.time.sleep")
    def test_exception_is_logged_not_raised(self, mock_sleep):
        ser = MagicMock()
        type(ser).rts = PropertyMock(side_effect=OSError("no port"))
        stm32_manual_reset(ser)  # should not raise


class TestNewBoardSessionPaths(unittest.TestCase):
    """The host wires samd into RFC2217 connect and stm32 into disconnect."""

    def _run_session(self, board_type):
        from com2tty.windows.control_handler import read_wsl_stderr
        proc = MagicMock()
        ser = MagicMock()
        ser.get_settings.return_value = {}
        sd = threading.Event()
        rfc_evt = threading.Event()
        proc.stderr.readline.side_effect = [
            b"[CONTROL] RFC2217_CONNECT\n",
            b"[CONTROL] RFC2217_DISCONNECT\n",
            b"",
        ]
        read_wsl_stderr(proc, ser, sd, rfc_evt, queue.Queue(),
                        threading.Event(), queue.Queue(), None, board_type)
        return ser

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.samd_touch_reset")
    def test_samd_touch_on_connect(self, mock_touch, mock_sleep, mock_redir):
        ser = self._run_session("samd")
        mock_touch.assert_called_once_with(ser)

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.stm32_manual_reset")
    def test_stm32_reset_on_disconnect(self, mock_reset, mock_sleep, mock_redir):
        ser = self._run_session("stm32")
        mock_reset.assert_called_once_with(ser)

    @patch("com2tty.windows.control_handler.Redirector")
    @patch("com2tty.windows.control_handler.time.sleep")
    @patch("com2tty.windows.control_handler.pico_manual_reset")
    def test_nrf52_uses_uf2_touch_on_disconnect(self, mock_reset, mock_sleep,
                                                mock_redir):
        ser = self._run_session("nrf52")
        mock_reset.assert_called_once_with(ser)


if __name__ == "__main__":
    unittest.main()
