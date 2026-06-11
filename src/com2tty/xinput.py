"""
Windows-side XInput controller reader for com2tty's gamepad mode.

Polls a physical Xbox/XInput controller through the native Windows driver
(no usbipd, no kernel driver needed in WSL) and packs each state snapshot
into a fixed 16-byte frame that ``pad_bridge.py`` decodes inside WSL.

Frame format (little-endian, 16 bytes)::

    <BBBBHBBhhhh>
      magic0, magic1, pad_index, flags,
      wButtons, bLeftTrigger, bRightTrigger,
      sThumbLX, sThumbLY, sThumbRX, sThumbRY
"""
import ctypes
import struct

FRAME_MAGIC0 = 0xAB
FRAME_MAGIC1 = 0xCD
FRAME_FORMAT = "<BBBBHBBhhhh"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)  # 16

# Reverse-channel rumble frame (WSL -> Windows), see pad_bridge.py.
RUMBLE_MAGIC0 = 0xFB
RUMBLE_MAGIC1 = 0xFE
RUMBLE_FORMAT = "<BBHH"
RUMBLE_SIZE = struct.calcsize(RUMBLE_FORMAT)  # 6

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

# The Guide (Xbox logo) button is only reported by the undocumented
# XInputGetStateEx, exported by ordinal 100 from xinput1_3/xinput1_4.
GUIDE_BUTTON_MASK = 0x0400
_GET_STATE_EX_ORDINAL = 100

# Candidate XInput DLLs, newest first.
_XINPUT_DLLS = ("xinput1_4", "xinput1_3", "xinput9_1_0")


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint),
        ("Gamepad", _XINPUT_GAMEPAD),
    ]


class _XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [
        ("wLeftMotorSpeed", ctypes.c_ushort),
        ("wRightMotorSpeed", ctypes.c_ushort),
    ]


class RumbleReader:
    """Resynchronising parser for the 6-byte rumble frames coming back from
    the WSL helper's stdout (the reverse channel of the gamepad bridge)."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Add raw bytes, yield every complete (left, right) rumble pair."""
        self._buf.extend(data)
        frames = []
        while True:
            start = self._buf.find(RUMBLE_MAGIC0)
            if start == -1:
                self._buf.clear()
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < RUMBLE_SIZE:
                break
            if self._buf[1] != RUMBLE_MAGIC1:
                del self._buf[0]
                continue
            _, _, left, right = struct.unpack(
                RUMBLE_FORMAT, bytes(self._buf[:RUMBLE_SIZE]))
            del self._buf[:RUMBLE_SIZE]
            frames.append((left, right))
        return frames


def pack_frame(index, connected, buttons=0, lt=0, rt=0,
               lx=0, ly=0, rx=0, ry=0):
    """Build a 16-byte controller frame. Pure function, testable anywhere."""
    flags = 0x01 if connected else 0x00
    return struct.pack(
        FRAME_FORMAT, FRAME_MAGIC0, FRAME_MAGIC1, index & 0xFF, flags,
        buttons & 0xFFFF, lt & 0xFF, rt & 0xFF,
        lx, ly, rx, ry,
    )


def _load_xinput():
    last_err = None
    for name in _XINPUT_DLLS:
        try:
            return getattr(ctypes.windll, name)
        except OSError as exc:  # pragma: no cover - depends on host DLLs
            last_err = exc
            continue
    raise OSError(
        "No XInput DLL found (tried %s). Is this a Windows host with "
        "DirectX installed?" % ", ".join(_XINPUT_DLLS)
    ) from last_err


class GamepadSource:
    """Polls one XInput controller slot and produces frames on demand."""

    def __init__(self, index=0):
        if not 0 <= index <= 3:
            raise ValueError("pad index must be 0-3")
        self.index = index
        self._xinput = _load_xinput()
        # Prefer the hidden XInputGetStateEx (ordinal 100), which also
        # reports the Guide button; fall back to the documented call when
        # the loaded DLL does not export it (xinput9_1_0).
        try:
            self._get_state = self._xinput[_GET_STATE_EX_ORDINAL]
        except Exception:
            self._get_state = self._xinput.XInputGetState
        self._last_packet = None
        self._last_connected = None

    def set_rumble(self, left, right):
        """Drive the controller motors (0-65535 each; left is the heavy,
        low-frequency motor). Returns True when XInput accepted the state."""
        vib = _XINPUT_VIBRATION(left & 0xFFFF, right & 0xFFFF)
        try:
            return self._xinput.XInputSetState(
                self.index, ctypes.byref(vib)) == ERROR_SUCCESS
        except Exception:
            return False

    def poll(self):
        """Read current state.

        Returns ``(changed, frame_bytes)`` where ``changed`` is True when the
        controller's packet number or connection status differs from the last
        poll (so the host can send-on-change and avoid flooding the pipe).
        """
        st = _XINPUT_STATE()
        res = self._get_state(self.index, ctypes.byref(st))
        connected = (res == ERROR_SUCCESS)

        if connected:
            gp = st.Gamepad
            changed = (self._last_packet != st.dwPacketNumber or
                       self._last_connected is not True)
            self._last_packet = st.dwPacketNumber
            frame = pack_frame(
                self.index, True, gp.wButtons,
                gp.bLeftTrigger, gp.bRightTrigger,
                gp.sThumbLX, gp.sThumbLY, gp.sThumbRX, gp.sThumbRY,
            )
        else:
            changed = (self._last_connected is not False)
            frame = pack_frame(self.index, False)

        self._last_connected = connected
        return changed, frame
