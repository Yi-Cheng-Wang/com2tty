import os
import sys
import struct
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty import pad_bridge as pb


class TestIoctlConstants(unittest.TestCase):
    """Canonical x86_64 values from linux/uinput.h + asm-generic/ioctl.h."""

    def test_constants(self):
        self.assertEqual(pb.UI_DEV_CREATE, 0x5501)
        self.assertEqual(pb.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(pb.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(pb.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(pb.UI_SET_ABSBIT, 0x40045567)


class TestStructLayout(unittest.TestCase):

    def test_input_event_size(self):
        # 64-bit timeval (16) + type(2) + code(2) + value(4)
        self.assertEqual(len(pb.encode_event(pb.EV_KEY, pb.BTN_A, 1)), 24)

    def test_uinput_user_dev_size(self):
        # name[80] + input_id(8) + ff_effects_max(4) + 4*ABS_CNT*4
        self.assertEqual(len(pb.build_uinput_user_dev("pad", 1, 2, 3)), 1116)

    def test_uinput_user_dev_fields(self):
        blob = pb.build_uinput_user_dev("Microsoft X-Box 360 pad",
                                        0x045e, 0x028e, 0x0110)
        name = blob[:80].split(b"\x00", 1)[0]
        self.assertEqual(name, b"Microsoft X-Box 360 pad")
        bustype, vendor, product, version = struct.unpack("=HHHH", blob[80:88])
        self.assertEqual((vendor, product), (0x045e, 0x028e))
        self.assertEqual(bustype, 0x03)  # BUS_USB

    def test_frame_size(self):
        self.assertEqual(pb.FRAME_SIZE, 16)


class TestParseFrame(unittest.TestCase):

    def _frame(self, **kw):
        d = dict(m0=pb.FRAME_MAGIC0, m1=pb.FRAME_MAGIC1, index=0, flags=1,
                 buttons=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0)
        d.update(kw)
        return struct.pack(pb.FRAME_FORMAT, d["m0"], d["m1"], d["index"],
                           d["flags"], d["buttons"], d["lt"], d["rt"],
                           d["lx"], d["ly"], d["rx"], d["ry"])

    def test_valid(self):
        st = pb.parse_frame(self._frame(buttons=0x1000, lx=123))
        self.assertTrue(st["connected"])
        self.assertEqual(st["buttons"], 0x1000)
        self.assertEqual(st["lx"], 123)

    def test_bad_magic(self):
        self.assertIsNone(pb.parse_frame(self._frame(m0=0x00)))
        self.assertIsNone(pb.parse_frame(self._frame(m1=0x00)))

    def test_bad_length(self):
        self.assertIsNone(pb.parse_frame(b"\xab\xcd"))

    def test_disconnected_flag(self):
        st = pb.parse_frame(self._frame(flags=0))
        self.assertFalse(st["connected"])


class TestStateToEvents(unittest.TestCase):

    def _state(self, **kw):
        d = dict(index=0, connected=True, buttons=0, lt=0, rt=0,
                 lx=0, ly=0, rx=0, ry=0)
        d.update(kw)
        return d

    def _find(self, events, etype, code):
        for t, c, v in events:
            if t == etype and c == code:
                return v
        raise AssertionError(f"event {etype}/{code} not found")

    def test_button_a(self):
        ev = pb.state_to_events(self._state(buttons=0x1000))
        self.assertEqual(self._find(ev, pb.EV_KEY, pb.BTN_A), 1)
        self.assertEqual(self._find(ev, pb.EV_KEY, pb.BTN_B), 0)

    def test_dpad(self):
        ev = pb.state_to_events(self._state(buttons=0x0004))  # LEFT
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_HAT0X), -1)
        ev = pb.state_to_events(self._state(buttons=0x0008))  # RIGHT
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_HAT0X), 1)
        ev = pb.state_to_events(self._state(buttons=0x0001))  # UP
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_HAT0Y), -1)
        ev = pb.state_to_events(self._state(buttons=0x0002))  # DOWN
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_HAT0Y), 1)

    def test_y_axis_inverted(self):
        ev = pb.state_to_events(self._state(ly=100, ry=-200))
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_Y), -100)
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_RY), 200)

    def test_y_axis_clamp_overflow(self):
        # -(-32768) would be 32768, must clamp to 32767
        ev = pb.state_to_events(self._state(ly=-32768))
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_Y), 32767)

    def test_triggers(self):
        ev = pb.state_to_events(self._state(lt=255, rt=128))
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_Z), 255)
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_RZ), 128)

    def test_disconnected_is_neutral(self):
        ev = pb.state_to_events(self._state(connected=False, buttons=0xFFFF,
                                            lx=9999, lt=255))
        self.assertEqual(self._find(ev, pb.EV_KEY, pb.BTN_A), 0)
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_X), 0)
        self.assertEqual(self._find(ev, pb.EV_ABS, pb.ABS_Z), 0)


class TestFrameReader(unittest.TestCase):

    def _frame(self, buttons=0):
        return struct.pack(pb.FRAME_FORMAT, pb.FRAME_MAGIC0, pb.FRAME_MAGIC1,
                           0, 1, buttons, 0, 0, 0, 0, 0, 0)

    def test_single(self):
        r = pb.FrameReader()
        out = r.feed(self._frame(buttons=0x1000))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["buttons"], 0x1000)

    def test_multiple(self):
        r = pb.FrameReader()
        out = r.feed(self._frame(1) + self._frame(2) + self._frame(4))
        self.assertEqual([s["buttons"] for s in out], [1, 2, 4])

    def test_split_across_feeds(self):
        r = pb.FrameReader()
        f = self._frame(0x2000)
        self.assertEqual(r.feed(f[:7]), [])
        out = r.feed(f[7:])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["buttons"], 0x2000)

    def test_resync_after_junk(self):
        r = pb.FrameReader()
        out = r.feed(b"\x00\x11\x22" + self._frame(0x4000))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["buttons"], 0x4000)

    def test_false_magic0_then_real(self):
        # 0xAB not followed by 0xCD must be skipped without losing the next frame
        r = pb.FrameReader()
        out = r.feed(b"\xab\x00" + self._frame(0x8000))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["buttons"], 0x8000)

    def test_parse_returning_none_is_skipped(self):
        # Guard: a magic-aligned frame that parse_frame rejects (returns None)
        # is consumed and dropped rather than appended.
        r = pb.FrameReader()
        with patch("com2tty.pad_bridge.parse_frame", return_value=None):
            out = r.feed(self._frame(buttons=0x1000))
        self.assertEqual(out, [])


class TestEncodeReport(unittest.TestCase):

    def test_appends_syn_report(self):
        events = [(pb.EV_KEY, pb.BTN_A, 1), (pb.EV_ABS, pb.ABS_X, 100)]
        blob = pb.encode_report(events)
        # 2 events + 1 trailing SYN_REPORT, each 24 bytes
        self.assertEqual(len(blob), 24 * 3)
        last = struct.unpack("=qqHHi", blob[-24:])
        self.assertEqual(last[2], pb.EV_SYN)
        self.assertEqual(last[3], pb.SYN_REPORT)

    def test_matches_event_encoding(self):
        blob = pb.encode_report([(pb.EV_KEY, pb.BTN_B, 1)])
        self.assertEqual(blob[:24], pb.encode_event(pb.EV_KEY, pb.BTN_B, 1))


class _CaptureSink(pb.GamepadSink):
    def __init__(self):
        self.writes = []

    def open(self):
        pass

    def _write(self, blob):
        self.writes.append(blob)


class TestSinkEmit(unittest.TestCase):

    def test_emit_writes_one_report_per_call(self):
        sink = _CaptureSink()
        sink.emit(pb.state_to_events({
            "index": 0, "connected": True, "buttons": 0x1000,
            "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
        self.assertEqual(len(sink.writes), 1)
        # Report ends with a SYN_REPORT event.
        tail = struct.unpack("=qqHHi", sink.writes[0][-24:])
        self.assertEqual((tail[2], tail[3]), (pb.EV_SYN, pb.SYN_REPORT))

    def test_default_tmp_path_constant(self):
        self.assertEqual(pb.DEFAULT_TMP_PAD, "/tmp/com2pad0")
        # TmpStreamGamepad and UinputGamepad share the event report format.
        self.assertTrue(issubclass(pb.TmpStreamGamepad, pb.GamepadSink))
        self.assertTrue(issubclass(pb.UinputGamepad, pb.GamepadSink))


class TestTmpStreamGamepad(unittest.TestCase):
    """The FIFO sink uses Linux syscalls; mock os.* so it runs anywhere.

    os.O_NONBLOCK and os.mkfifo do not exist on Windows, so they are patched
    with create=True to let these tests run on any platform.
    """

    @patch("com2tty.pad_bridge.os.O_NONBLOCK", 2048, create=True)
    @patch("com2tty.pad_bridge.os.open", return_value=7)
    @patch("com2tty.pad_bridge.os.mkfifo", create=True)
    @patch("com2tty.pad_bridge.os.path.exists", return_value=False)
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=False)
    def test_open_creates_fifo(self, m_lexists, m_exists, m_mkfifo, m_open):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.open()
        m_mkfifo.assert_called_once_with("/tmp/p", 0o666)
        self.assertEqual(sink.fd, 7)

    @patch("com2tty.pad_bridge.os.O_NONBLOCK", 2048, create=True)
    @patch("com2tty.pad_bridge.os.open", return_value=7)
    @patch("com2tty.pad_bridge.os.mkfifo", create=True)
    @patch("com2tty.pad_bridge.os.path.exists", return_value=True)
    @patch("com2tty.pad_bridge.os.unlink")
    @patch("com2tty.pad_bridge.stat.S_ISFIFO", return_value=False)
    @patch("com2tty.pad_bridge.os.stat")
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=True)
    def test_open_replaces_stale_non_fifo(self, m_lex, m_stat, m_isfifo,
                                          m_unlink, m_exists, m_mkfifo, m_open):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.open()
        m_unlink.assert_called_once_with("/tmp/p")
        # exists() returned True so mkfifo is skipped.
        m_mkfifo.assert_not_called()

    @patch("com2tty.pad_bridge.os.O_NONBLOCK", 2048, create=True)
    @patch("com2tty.pad_bridge.os.open", return_value=7)
    @patch("com2tty.pad_bridge.os.mkfifo", create=True)
    @patch("com2tty.pad_bridge.os.path.exists", return_value=False)
    @patch("com2tty.pad_bridge.os.unlink")
    @patch("com2tty.pad_bridge.os.stat", side_effect=OSError("boom"))
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=True)
    def test_open_stat_oserror_unlinks(self, m_lex, m_stat, m_unlink,
                                       m_exists, m_mkfifo, m_open):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.open()
        m_unlink.assert_called_once_with("/tmp/p")
        m_mkfifo.assert_called_once()

    @patch("com2tty.pad_bridge.os.O_NONBLOCK", 2048, create=True)
    @patch("com2tty.pad_bridge.os.open", return_value=7)
    @patch("com2tty.pad_bridge.os.mkfifo", create=True)
    @patch("com2tty.pad_bridge.os.path.exists", return_value=True)
    @patch("com2tty.pad_bridge.os.unlink")
    @patch("com2tty.pad_bridge.stat.S_ISFIFO", return_value=True)
    @patch("com2tty.pad_bridge.os.stat")
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=True)
    def test_open_reuses_existing_fifo(self, m_lex, m_stat, m_isfifo,
                                       m_unlink, m_exists, m_mkfifo, m_open):
        # Existing path that is already a FIFO: no unlink, no mkfifo.
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.open()
        m_unlink.assert_not_called()
        m_mkfifo.assert_not_called()
        self.assertEqual(sink.fd, 7)

    @patch("com2tty.pad_bridge.os.write")
    def test_write_ok(self, m_write):
        sink = pb.TmpStreamGamepad()
        sink.fd = 7
        sink._write(b"abc")
        m_write.assert_called_once_with(7, b"abc")

    @patch("com2tty.pad_bridge.os.write", side_effect=BlockingIOError())
    def test_write_blocking_dropped(self, m_write):
        sink = pb.TmpStreamGamepad()
        sink.fd = 7
        sink._write(b"abc")  # must not raise

    @patch("com2tty.pad_bridge.os.write", side_effect=OSError())
    def test_write_oserror_dropped(self, m_write):
        sink = pb.TmpStreamGamepad()
        sink.fd = 7
        sink._write(b"abc")  # must not raise

    @patch("com2tty.pad_bridge.os.unlink")
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=True)
    @patch("com2tty.pad_bridge.os.close")
    def test_close_unlinks(self, m_close, m_lexists, m_unlink):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.fd = 7
        sink.close()
        m_close.assert_called_once_with(7)
        m_unlink.assert_called_once_with("/tmp/p")
        self.assertIsNone(sink.fd)

    @patch("com2tty.pad_bridge.os.close", side_effect=Exception("x"))
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=False)
    def test_close_swallows_errors(self, m_lexists, m_close):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.fd = 7
        sink.close()  # must not raise
        self.assertIsNone(sink.fd)

    def test_close_when_never_opened(self):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        with patch("com2tty.pad_bridge.os.path.lexists", return_value=False):
            sink.close()  # fd is None branch

    @patch("com2tty.pad_bridge.os.unlink", side_effect=OSError("unlink fail"))
    @patch("com2tty.pad_bridge.os.path.lexists", return_value=True)
    @patch("com2tty.pad_bridge.os.close")
    def test_close_swallows_unlink_errors(self, m_close, m_lexists, m_unlink):
        sink = pb.TmpStreamGamepad(path="/tmp/p")
        sink.fd = 7
        sink.close()  # unlink raises -> swallowed by the second try/except
        self.assertIsNone(sink.fd)


class TestUinputGamepad(unittest.TestCase):
    """uinput sink uses fcntl ioctls; inject a fake fcntl + mock os.*."""

    @patch("com2tty.pad_bridge.os.O_NONBLOCK", 2048, create=True)
    @patch("com2tty.pad_bridge.os.write")
    @patch("com2tty.pad_bridge.os.open", return_value=9)
    def test_open_creates_device(self, m_open, m_write):
        fake_fcntl = MagicMock()
        with patch("com2tty.pad_bridge.fcntl", fake_fcntl):
            sink = pb.UinputGamepad()
            sink.open()
        self.assertEqual(sink.fd, 9)
        m_open.assert_called_once()
        # uinput_user_dev written, then UI_DEV_CREATE issued.
        m_write.assert_called_once()
        called_requests = [c.args[1] for c in fake_fcntl.ioctl.call_args_list]
        self.assertIn(pb.UI_DEV_CREATE, called_requests)
        self.assertIn(pb.UI_SET_EVBIT, called_requests)

    @patch("com2tty.pad_bridge.os.open", return_value=9)
    def test_open_rejects_non_64bit_abi(self, m_open):
        """On a 32-bit ABI the struct/ioctl layout is wrong; fail clearly and
        do not open the device."""
        fake_fcntl = MagicMock()
        with patch("com2tty.pad_bridge.fcntl", fake_fcntl), \
             patch("com2tty.pad_bridge.struct.calcsize", return_value=4):
            sink = pb.UinputGamepad()
            with self.assertRaises(OSError) as ctx:
                sink.open()
        self.assertIn("64-bit", str(ctx.exception))
        m_open.assert_not_called()

    @patch("com2tty.pad_bridge.os.write")
    def test_write_delegates_to_os_write(self, m_write):
        sink = pb.UinputGamepad()
        sink.fd = 9
        sink._write(b"xyz")
        m_write.assert_called_once_with(9, b"xyz")

    @patch("com2tty.pad_bridge.os.close")
    def test_close_destroys_device(self, m_close):
        fake_fcntl = MagicMock()
        with patch("com2tty.pad_bridge.fcntl", fake_fcntl):
            sink = pb.UinputGamepad()
            sink.fd = 9
            sink.close()
        fake_fcntl.ioctl.assert_called_once_with(9, pb.UI_DEV_DESTROY)
        m_close.assert_called_once_with(9)
        self.assertIsNone(sink.fd)

    @patch("com2tty.pad_bridge.os.close", side_effect=Exception("x"))
    def test_close_swallows_errors(self, m_close):
        fake_fcntl = MagicMock()
        fake_fcntl.ioctl.side_effect = Exception("destroy fail")
        with patch("com2tty.pad_bridge.fcntl", fake_fcntl):
            sink = pb.UinputGamepad()
            sink.fd = 9
            sink.close()  # must not raise
        self.assertIsNone(sink.fd)

    def test_close_when_never_opened(self):
        sink = pb.UinputGamepad()
        sink.close()  # fd is None branch, no-op


class TestOpenSink(unittest.TestCase):

    def test_default_uses_tmp_stream(self):
        fake = MagicMock()
        with patch("com2tty.pad_bridge.TmpStreamGamepad", return_value=fake):
            sink, msg = pb._open_sink(False, "/tmp/p", "Pad")
        self.assertIs(sink, fake)
        fake.open.assert_called_once()
        self.assertIn("/tmp/p", msg)
        self.assertIn("no root", msg)

    def test_uinput_success(self):
        fake = MagicMock()
        with patch("com2tty.pad_bridge.UinputGamepad", return_value=fake):
            sink, msg = pb._open_sink(True, "/tmp/p", "Pad")
        self.assertIs(sink, fake)
        fake.open.assert_called_once()
        self.assertIn("real device", msg)

    def test_uinput_permission_error_falls_back_to_tmp(self):
        bad = MagicMock()
        bad.open.side_effect = PermissionError("denied")
        good = MagicMock()
        with patch("com2tty.pad_bridge.UinputGamepad", return_value=bad), \
             patch("com2tty.pad_bridge.TmpStreamGamepad", return_value=good):
            sink, msg = pb._open_sink(True, "/tmp/p", "Pad")
        self.assertIs(sink, good)        # fell back
        good.open.assert_called_once()
        self.assertIn("/tmp/p", msg)

    def test_uinput_oserror_falls_back(self):
        bad = MagicMock()
        bad.open.side_effect = OSError("ENODEV")
        good = MagicMock()
        with patch("com2tty.pad_bridge.UinputGamepad", return_value=bad), \
             patch("com2tty.pad_bridge.TmpStreamGamepad", return_value=good):
            sink, msg = pb._open_sink(True, "/tmp/p", "Pad")
        self.assertIs(sink, good)


if __name__ == "__main__":
    unittest.main()
