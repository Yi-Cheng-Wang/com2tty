"""Binary frame formats of the gamepad bridge pipe.

Two fixed-length, magic-prefixed frame types flow through the helper's
stdin/stdout pipes:

* **Controller frame (16 bytes, host -> WSL):** one XInput state snapshot::

      <BBBBHBBhhhh>
       |||| | || \\__ sThumbLX/LY/RX/RY  (int16)
       |||| | \\_____ bLeftTrigger / bRightTrigger (uint8)
       |||| \\_______ wButtons (uint16, XInput bitmask)
       |||\\_________ flags  (bit0 = controller connected)
       ||\\__________ pad index (0-3)
       \\\\__________ magic 0xAB 0xCD

* **Rumble frame (6 bytes, WSL -> host):** force-feedback magnitudes::

      <BBHH>  magic 0xFB 0xFE, strong/left motor, weak/right motor

Both readers resynchronise on the magic prefix, so partial reads and stray
bytes on the pipe are tolerated. This module is the single source of truth;
``pad_bridge.py`` (which runs standalone inside WSL) carries a synchronised
copy until it can import the package, and the test suite pins the two
together.
"""
import struct

FRAME_MAGIC0 = 0xAB
FRAME_MAGIC1 = 0xCD
FRAME_FORMAT = "<BBBBHBBhhhh"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)  # 16

RUMBLE_MAGIC0 = 0xFB
RUMBLE_MAGIC1 = 0xFE
RUMBLE_FORMAT = "<BBHH"
RUMBLE_SIZE = struct.calcsize(RUMBLE_FORMAT)  # 6


def pack_frame(index, connected, buttons=0, lt=0, rt=0,
               lx=0, ly=0, rx=0, ry=0):
    """Build a 16-byte controller frame. Pure function, testable anywhere."""
    flags = 0x01 if connected else 0x00
    # Clamp the stick axes to the signed 16-bit range. XInput already reports
    # values in range, but an out-of-range caller would otherwise make
    # struct.pack raise and crash the host instead of degrading gracefully
    # (consistent with the masking applied to the other fields).
    def _clip16(v):
        return max(-32768, min(32767, v))
    lx, ly, rx, ry = _clip16(lx), _clip16(ly), _clip16(rx), _clip16(ry)
    return struct.pack(
        FRAME_FORMAT, FRAME_MAGIC0, FRAME_MAGIC1, index & 0xFF, flags,
        buttons & 0xFFFF, lt & 0xFF, rt & 0xFF,
        lx, ly, rx, ry,
    )


def parse_frame(frame):
    """Parse a 16-byte controller frame into a state dict, or ``None``."""
    if len(frame) != FRAME_SIZE:
        return None
    (m0, m1, index, flags, buttons, lt, rt,
     lx, ly, rx, ry) = struct.unpack(FRAME_FORMAT, frame)
    if m0 != FRAME_MAGIC0 or m1 != FRAME_MAGIC1:
        return None
    return {
        "index": index,
        "connected": bool(flags & 0x01),
        "buttons": buttons,
        "lt": lt,
        "rt": rt,
        "lx": lx,
        "ly": ly,
        "rx": rx,
        "ry": ry,
    }


def pack_rumble(strong, weak):
    """Build a rumble frame: strong (left/low-freq) and weak (right/high-freq)
    motor magnitudes, 0-65535 each."""
    return struct.pack(RUMBLE_FORMAT, RUMBLE_MAGIC0, RUMBLE_MAGIC1,
                       strong & 0xFFFF, weak & 0xFFFF)


class _ResyncReader:
    """Shared resynchronising scan for a fixed-length magic-prefixed frame."""

    _magic0 = None
    _magic1 = None
    _size = None

    def __init__(self):
        self._buf = bytearray()

    def _decode(self, frame):  # pragma: no cover - overridden
        raise NotImplementedError

    def feed(self, data):
        """Add raw bytes, return every complete decoded frame in order."""
        self._buf.extend(data)
        frames = []
        while True:
            start = self._buf.find(self._magic0)
            if start == -1:
                self._buf.clear()
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < self._size:
                break
            if self._buf[1] != self._magic1:
                # False positive on magic0; skip it and keep scanning.
                del self._buf[0]
                continue
            frame = bytes(self._buf[:self._size])
            del self._buf[:self._size]
            decoded = self._decode(frame)
            if decoded is not None:
                frames.append(decoded)
        return frames


class FrameReader(_ResyncReader):
    """Yields parsed controller-state dicts from the 16-byte frame stream."""

    _magic0 = FRAME_MAGIC0
    _magic1 = FRAME_MAGIC1
    _size = FRAME_SIZE

    def _decode(self, frame):
        return parse_frame(frame)


class RumbleReader(_ResyncReader):
    """Yields ``(strong, weak)`` motor pairs from the 6-byte rumble stream."""

    _magic0 = RUMBLE_MAGIC0
    _magic1 = RUMBLE_MAGIC1
    _size = RUMBLE_SIZE

    def _decode(self, frame):
        _, _, strong, weak = struct.unpack(RUMBLE_FORMAT, frame)
        return (strong, weak)
