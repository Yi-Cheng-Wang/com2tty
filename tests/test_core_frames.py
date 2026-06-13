import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.core import frames


class TestPackParseFrame(unittest.TestCase):

    def test_roundtrip(self):
        blob = frames.pack_frame(2, True, buttons=0x1234, lt=10, rt=20,
                                 lx=-100, ly=200, rx=-300, ry=400)
        self.assertEqual(len(blob), frames.FRAME_SIZE)
        state = frames.parse_frame(blob)
        self.assertEqual(state, {
            "index": 2, "connected": True, "buttons": 0x1234,
            "lt": 10, "rt": 20, "lx": -100, "ly": 200, "rx": -300, "ry": 400,
        })

    def test_disconnected_flag(self):
        state = frames.parse_frame(frames.pack_frame(0, False))
        self.assertFalse(state["connected"])

    def test_bad_magic_returns_none(self):
        blob = bytearray(frames.pack_frame(0, True))
        blob[0] = 0x00
        self.assertIsNone(frames.parse_frame(bytes(blob)))

    def test_bad_length_returns_none(self):
        self.assertIsNone(frames.parse_frame(b"\xab\xcd"))


class TestFrameReader(unittest.TestCase):

    def test_partial_then_complete(self):
        reader = frames.FrameReader()
        blob = frames.pack_frame(1, True, buttons=0x0001)
        self.assertEqual(reader.feed(blob[:7]), [])
        states = reader.feed(blob[7:])
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["buttons"], 0x0001)

    def test_resync_on_garbage(self):
        reader = frames.FrameReader()
        blob = frames.pack_frame(0, True)
        states = reader.feed(b"\x00\xff\xab\x00" + blob)
        self.assertEqual(len(states), 1)

    def test_multiple_frames_in_one_read(self):
        reader = frames.FrameReader()
        blob = frames.pack_frame(0, True) + frames.pack_frame(1, False)
        states = reader.feed(blob)
        self.assertEqual([s["index"] for s in states], [0, 1])


class TestRumble(unittest.TestCase):

    def test_pack_rumble_layout(self):
        blob = frames.pack_rumble(0x1122, 0x3344)
        self.assertEqual(
            blob, struct.pack("<BBHH", 0xFB, 0xFE, 0x1122, 0x3344))

    def test_reader_roundtrip_with_noise(self):
        reader = frames.RumbleReader()
        data = b"\x01\x02" + frames.pack_rumble(100, 200) + b"\xfb"
        self.assertEqual(reader.feed(data), [(100, 200)])
        # The dangling magic byte completes into a frame later.
        rest = frames.pack_rumble(7, 8)[1:]
        self.assertEqual(reader.feed(rest), [(7, 8)])

    def test_false_magic_prefix_is_skipped(self):
        reader = frames.RumbleReader()
        data = b"\xfb\x00" + frames.pack_rumble(1, 2)
        self.assertEqual(reader.feed(data), [(1, 2)])


class TestCrossModuleSync(unittest.TestCase):
    """core.frames is the single source of truth; the WSL evdev sink and the
    Windows poller re-export it, and these assertions pin the re-exports."""

    def test_xinput_constants_match(self):
        from com2tty.windows import gamepad_host as xi
        self.assertEqual(xi.FRAME_FORMAT, frames.FRAME_FORMAT)
        self.assertEqual((xi.FRAME_MAGIC0, xi.FRAME_MAGIC1),
                         (frames.FRAME_MAGIC0, frames.FRAME_MAGIC1))
        self.assertEqual(xi.RUMBLE_FORMAT, frames.RUMBLE_FORMAT)
        self.assertEqual((xi.RUMBLE_MAGIC0, xi.RUMBLE_MAGIC1),
                         (frames.RUMBLE_MAGIC0, frames.RUMBLE_MAGIC1))

    def test_pad_bridge_constants_match(self):
        from com2tty.wsl import evdev_sink as pb
        self.assertEqual(pb.FRAME_FORMAT, frames.FRAME_FORMAT)
        self.assertEqual((pb.FRAME_MAGIC0, pb.FRAME_MAGIC1),
                         (frames.FRAME_MAGIC0, frames.FRAME_MAGIC1))
        self.assertEqual(pb.RUMBLE_FORMAT, frames.RUMBLE_FORMAT)
        self.assertEqual((pb.RUMBLE_MAGIC0, pb.RUMBLE_MAGIC1),
                         (frames.RUMBLE_MAGIC0, frames.RUMBLE_MAGIC1))

    def test_pack_frame_identical_to_xinput(self):
        from com2tty.windows import gamepad_host as xi
        args = (3, True, 0x00F0, 1, 2, -3, 4, -5, 6)
        self.assertEqual(xi.pack_frame(*args), frames.pack_frame(*args))


if __name__ == "__main__":
    unittest.main()
