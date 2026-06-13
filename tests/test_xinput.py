import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows import gamepad_host as xi
from com2tty.wsl import evdev_sink as pb


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
        with patch("com2tty.windows.gamepad_host._load_xinput"):
            with self.assertRaises(ValueError):
                xi.GamepadSource(7)

    def _make_source(self, get_state_impl):
        fake_dll = MagicMock()
        fake_dll.XInputGetState.side_effect = get_state_impl
        # GamepadSource prefers the ordinal-100 export (XInputGetStateEx);
        # serve the same implementation there.
        fake_dll.__getitem__.return_value = fake_dll.XInputGetState
        with patch("com2tty.windows.gamepad_host._load_xinput", return_value=fake_dll):
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
        fake_dll.__getitem__.return_value = fake_dll.XInputGetState
        with patch("com2tty.windows.gamepad_host._load_xinput", return_value=fake_dll):
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


class TestGetStateExFallback(unittest.TestCase):

    def test_falls_back_to_documented_call(self):
        fake_dll = MagicMock()
        fake_dll.__getitem__.side_effect = AttributeError("no ordinal 100")
        with patch("com2tty.windows.gamepad_host._load_xinput", return_value=fake_dll):
            src = xi.GamepadSource(0)
        self.assertIs(src._get_state, fake_dll.XInputGetState)


class TestSetRumble(unittest.TestCase):

    def _source(self):
        fake_dll = MagicMock()
        fake_dll.__getitem__.return_value = fake_dll.XInputGetState
        with patch("com2tty.windows.gamepad_host._load_xinput", return_value=fake_dll):
            return xi.GamepadSource(0), fake_dll

    def test_success(self):
        src, dll = self._source()
        dll.XInputSetState.return_value = xi.ERROR_SUCCESS
        self.assertTrue(src.set_rumble(1000, 2000))
        dll.XInputSetState.assert_called_once()

    def test_device_not_connected(self):
        src, dll = self._source()
        dll.XInputSetState.return_value = xi.ERROR_DEVICE_NOT_CONNECTED
        self.assertFalse(src.set_rumble(1000, 2000))

    def test_exception_returns_false(self):
        src, dll = self._source()
        dll.XInputSetState.side_effect = OSError("dll error")
        self.assertFalse(src.set_rumble(0, 0))

    def test_values_masked_to_u16(self):
        src, dll = self._source()
        dll.XInputSetState.return_value = xi.ERROR_SUCCESS
        self.assertTrue(src.set_rumble(0x1FFFF, -1))


class TestRumbleReader(unittest.TestCase):

    def test_roundtrip_with_pad_bridge(self):
        reader = xi.RumbleReader()
        frames = reader.feed(pb.pack_rumble(0x1234, 0xFFFF))
        self.assertEqual(frames, [(0x1234, 0xFFFF)])

    def test_partial_then_complete(self):
        blob = pb.pack_rumble(10, 20)
        reader = xi.RumbleReader()
        self.assertEqual(reader.feed(blob[:3]), [])
        self.assertEqual(reader.feed(blob[3:]), [(10, 20)])

    def test_resync_on_garbage(self):
        reader = xi.RumbleReader()
        frames = reader.feed(b"junk" + pb.pack_rumble(1, 2) + b"\x00")
        self.assertEqual(frames, [(1, 2)])

    def test_false_magic0_skipped(self):
        # A stray 0xFB not followed by 0xFE must be discarded.
        reader = xi.RumbleReader()
        frames = reader.feed(b"\xfb\x00\x00\x00\x00\x00" + pb.pack_rumble(3, 4))
        self.assertEqual(frames, [(3, 4)])

    def test_no_magic_clears_buffer(self):
        reader = xi.RumbleReader()
        self.assertEqual(reader.feed(b"\x00\x01\x02"), [])
        self.assertEqual(len(reader._buf), 0)

    def test_multiple_frames_in_one_feed(self):
        blob = pb.pack_rumble(1, 1) + pb.pack_rumble(2, 2)
        self.assertEqual(xi.RumbleReader().feed(blob), [(1, 1), (2, 2)])


class TestLoadXinput(unittest.TestCase):

    def test_returns_first_available_dll(self):
        with patch("com2tty.windows.gamepad_host.ctypes") as mc:
            mc.WinDLL.side_effect = lambda path: "dll-" + path
            result = xi._load_xinput()
            # Loaded the newest candidate, by absolute System32 path.
            self.assertTrue(result.endswith(os.path.join("System32", "xinput1_4.dll")))

    def test_falls_through_to_next_dll(self):
        def fake_windll(path):
            if path.endswith("xinput1_4.dll"):
                raise OSError("missing")
            return "dll-" + path
        with patch("com2tty.windows.gamepad_host.ctypes") as mc:
            mc.WinDLL.side_effect = fake_windll
            # xinput1_4 fails, so the next candidate (xinput1_3) is used.
            self.assertTrue(xi._load_xinput().endswith("xinput1_3.dll"))

    def test_loads_from_system32_not_cwd(self):
        seen = []
        with patch("com2tty.windows.gamepad_host.ctypes") as mc:
            mc.WinDLL.side_effect = lambda path: seen.append(path) or "ok"
            xi._load_xinput()
        # Every load attempt used an absolute path rooted at System32, never
        # a bare name resolvable from the current working directory.
        self.assertTrue(all(os.path.isabs(p) and "System32" in p for p in seen))

    def test_raises_when_no_dll_found(self):
        with patch("com2tty.windows.gamepad_host.ctypes") as mc:
            mc.WinDLL.side_effect = OSError("no such dll")
            with self.assertRaises(OSError):
                xi._load_xinput()


if __name__ == "__main__":
    unittest.main()
