"""
Windows-side XInput controller reader for com2tty's gamepad mode.

Polls a physical Xbox/XInput controller through the native Windows driver
(no usbipd, no kernel driver needed in WSL) and packs each state snapshot
into a fixed 16-byte frame that the WSL gamepad helper decodes; the helper
sends 6-byte rumble frames back over its stdout. Both formats are defined
in ``com2tty.core.frames``.
"""
import ctypes
import os

from ..core.frames import (  # noqa: F401 (re-exports kept for compatibility)
    FRAME_FORMAT,
    FRAME_MAGIC0,
    FRAME_MAGIC1,
    FRAME_SIZE,
    RUMBLE_FORMAT,
    RUMBLE_MAGIC0,
    RUMBLE_MAGIC1,
    RUMBLE_SIZE,
    RumbleReader,
    pack_frame,
)

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


def _load_xinput():
    # Load by absolute System32 path rather than bare name so a stray
    # ``xinput1_4.dll`` in the current working directory cannot be loaded
    # in preference to the system copy (DLL hijacking).
    system_dir = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    last_err = None
    for name in _XINPUT_DLLS:
        dll_path = os.path.join(system_dir, name + ".dll")
        try:
            return ctypes.WinDLL(dll_path)
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
        # Declare the C signatures explicitly. Without .argtypes/.restype
        # ctypes guesses int-sized arguments and a c_int return, which is
        # wrong for the pointer arguments on 64-bit and discards the unsigned
        # DWORD return; the resulting truncation is undefined behaviour.
        self._get_state.argtypes = [
            ctypes.c_ulong, ctypes.POINTER(_XINPUT_STATE)]
        self._get_state.restype = ctypes.c_ulong
        self._set_state = self._xinput.XInputSetState
        self._set_state.argtypes = [
            ctypes.c_ulong, ctypes.POINTER(_XINPUT_VIBRATION)]
        self._set_state.restype = ctypes.c_ulong
        self._last_packet = None
        self._last_connected = None

    def set_rumble(self, left, right):
        """Drive the controller motors (0-65535 each; left is the heavy,
        low-frequency motor). Returns True when XInput accepted the state."""
        vib = _XINPUT_VIBRATION(left & 0xFFFF, right & 0xFFFF)
        try:
            return self._set_state(
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
