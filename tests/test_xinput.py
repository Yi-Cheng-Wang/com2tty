import os
import sys
import struct
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty import xinput as xi
from com2tty import pad_bridge as pb


class TestPackFrame(unittest.TestCase):

    def test_size_and_magic(self):
        f = xi.pack_frame(0, True)
        self.assertEqual(len(f), 16)
        self.assertEqual(f[0], 0xAB)
        self.assertEqual(f[1], 0xCD)

    def test_connected_flag(self):
        self.assertEqual(xi.pack_frame(0, True)[3], 0x01)
        self.assertEqual(xi.pack_frame(0, False)[3], 0x00)

    def test_roundtrip_with_pad_bridge(self):
        f = xi.pack_frame(2, True, buttons=0x1004, lt=200, rt=50,
                          lx=12345, ly=-32768, rx=-1, ry=100)
        st = pb.parse_frame(f)
        self.assertEqual(st["index"], 2)
        self.assertTrue(st["connected"])
        self.assertEqual(st["buttons"], 0x1004)
        self.assertEqual(st["lt"], 200)
        self.assertEqual(st["rt"], 50)
        self.assertEqual(st["lx"], 12345)
        self.assertEqual(st["ly"], -32768)
        self.assertEqual(st["rx"], -1)
        self.assertEqual(st["ry"], 100)

    def test_index_masked(self):
        # Index byte is masked to a single byte.
        f = xi.pack_frame(3, False)
        self.assertEqual(f[2], 3)


class TestGamepadSource(unittest.TestCase):

    def test_rejects_bad_index(self):
        with patch("com2tty.xinput._load_xinput"):
            with self.assertRaises(ValueError):
                xi.GamepadSource(7)

    def _make_source(self, get_state_impl):
        fake_dll = MagicMock()
        fake_dll.XInputGetState.side_effect = get_state_impl
        with patch("com2tty.xinput._load_xinput", return_value=fake_dll):
            return xi.GamepadSource(0), fake_dll

    def test_disconnected(self):
        def impl(idx, ptr):
            return xi.ERROR_DEVICE_NOT_CONNECTED
        src, _ = self._make_source(impl)
        changed, frame = src.poll()
        self.assertTrue(changed)          # first observation = change
        self.assertEqual(frame[3], 0x00)  # not connected
        changed2, _ = src.poll()
        self.assertFalse(changed2)        # still disconnected = no change

    def test_connected_change_detection(self):
        import ctypes
        packet = {"n": 5}

        def impl(idx, ptr):
            # Populate the caller's XINPUT_STATE struct via its pointer.
            addr = ctypes.cast(ptr, ctypes.c_void_p).value
            state = xi._XINPUT_STATE.from_address(addr)
            state.dwPacketNumber = packet["n"]
            state.Gamepad.wButtons = 0x1000
            return xi.ERROR_SUCCESS

        fake_dll = MagicMock()
        fake_dll.XInputGetState.side_effect = impl
        with patch("com2tty.xinput._load_xinput", return_value=fake_dll):
            src = xi.GamepadSource(0)

        changed, frame = src.poll()
        self.assertTrue(changed)
        st = pb.parse_frame(frame)
        self.assertTrue(st["connected"])
        self.assertEqual(st["buttons"], 0x1000)

        # Same packet number -> no change.
        changed2, _ = src.poll()
        self.assertFalse(changed2)

        # New packet number -> change.
        packet["n"] = 6
        changed3, _ = src.poll()
        self.assertTrue(changed3)


if __name__ == "__main__":
    unittest.main()
