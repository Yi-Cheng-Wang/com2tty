"""
com2tty WSL Gamepad Bridge Helper.

Runs *inside* WSL. Reads fixed-length controller frames from stdin (fed by the
Windows host, which polls XInput) and turns them into a Linux evdev
``input_event`` stream representing a "Microsoft X-Box 360 pad".

It supports two sinks, mirroring com2tty's serial bridge philosophy of
defaulting to a user-writable ``/tmp`` endpoint and only touching ``/dev`` when
the user opts in and grants access:

* **TmpStreamGamepad (default, no root):** writes the evdev event stream to a
  FIFO under ``/tmp`` (e.g. ``/tmp/com2pad0``). Identical bytes to what a real
  ``/dev/input/eventN`` would emit, so a single reader works against both.
  A second FIFO at ``<path>.ff`` accepts 6-byte rumble frames (see
  ``pack_rumble``) from the consumer, giving this tier the same force
  feedback the uinput tier gets from the kernel.
* **UinputGamepad (``--uinput``, one-time root):** creates a real, system-wide
  ``/dev/input/event*`` device via ``/dev/uinput`` so SDL2 games and emulators
  see a normally-inserted controller. Falls back to the ``/tmp`` stream if
  ``/dev/uinput`` is not accessible.

This module deliberately uses *only* the Python standard library (ctypes,
struct, fcntl, os) so that the WSL guest needs no extra packages.

Frame format (16 bytes, little-endian), see ``xinput.py`` on the host side::

    <BBBBHBBhhhh>
     |||| | || \\\\__ sThumbLX/LY/RX/RY  (int16)
     |||| | \\______ bLeftTrigger / bRightTrigger (uint8)
     |||| \\________ wButtons (uint16, XInput bitmask)
     |||\\__________ flags  (bit0 = controller connected)
     ||\\___________ pad index (0-3)
     \\\\___________ magic 0xAB 0xCD
"""
import os
import select
import sys
import stat
import struct
import argparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard for unit tests
    fcntl = None

DEFAULT_TMP_PAD = "/tmp/com2pad0"


# --------------------------------------------------------------------------- #
# ioctl number construction (Linux asm-generic, valid on x86_64 WSL2)
# --------------------------------------------------------------------------- #
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS      # 8
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS  # 16
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS   # 30

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, typ, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (typ << _IOC_TYPESHIFT) |
            (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _IO(typ, nr):
    return _IOC(_IOC_NONE, typ, nr, 0)


def _IOW(typ, nr, size):
    return _IOC(_IOC_WRITE, typ, nr, size)


def _IOWR(typ, nr, size):
    return _IOC(_IOC_READ | _IOC_WRITE, typ, nr, size)


# struct sizes on a 64-bit (LP64) kernel ABI, the only one we support
# (see UinputGamepad.open): sizeof(struct ff_effect) == 48, so
# uinput_ff_upload = u32 + s32 + 2 * ff_effect and uinput_ff_erase =
# u32 request_id + s32 retval + u32 effect_id.
INPUT_EVENT_FORMAT = "=qqHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)  # 24
FF_EFFECT_SIZE = 48
UINPUT_FF_UPLOAD_SIZE = 4 + 4 + FF_EFFECT_SIZE * 2  # 104
UINPUT_FF_ERASE_SIZE = 12
# Offsets inside uinput_ff_upload: the ff_effect starts after request_id and
# retval; its union (ff_rumble_effect for FF_RUMBLE) starts 16 bytes in,
# after type/id/direction/trigger/replay plus alignment padding.
FF_EFFECT_OFFSET = 8
FF_RUMBLE_UNION_OFFSET = 16

UINPUT_IOCTL_BASE = ord('U')
UI_DEV_CREATE = _IO(UINPUT_IOCTL_BASE, 1)
UI_DEV_DESTROY = _IO(UINPUT_IOCTL_BASE, 2)
UI_SET_EVBIT = _IOW(UINPUT_IOCTL_BASE, 100, 4)
UI_SET_KEYBIT = _IOW(UINPUT_IOCTL_BASE, 101, 4)
UI_SET_ABSBIT = _IOW(UINPUT_IOCTL_BASE, 103, 4)
UI_SET_FFBIT = _IOW(UINPUT_IOCTL_BASE, 107, 4)
UI_BEGIN_FF_UPLOAD = _IOWR(UINPUT_IOCTL_BASE, 200, UINPUT_FF_UPLOAD_SIZE)
UI_END_FF_UPLOAD = _IOW(UINPUT_IOCTL_BASE, 201, UINPUT_FF_UPLOAD_SIZE)
UI_BEGIN_FF_ERASE = _IOWR(UINPUT_IOCTL_BASE, 202, UINPUT_FF_ERASE_SIZE)
UI_END_FF_ERASE = _IOW(UINPUT_IOCTL_BASE, 203, UINPUT_FF_ERASE_SIZE)

# Codes the kernel sends on the uinput fd to drive FF effect management.
EV_UINPUT = 0x0101
UI_FF_UPLOAD = 1
UI_FF_ERASE = 2


# --------------------------------------------------------------------------- #
# Linux input event constants (from linux/input-event-codes.h)
# --------------------------------------------------------------------------- #
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
EV_FF = 0x15
SYN_REPORT = 0x00

FF_RUMBLE = 0x50
FF_GAIN = 0x60
FF_AUTOCENTER = 0x61
FF_MAX_EFFECTS = 16

BTN_A = 0x130
BTN_B = 0x131
BTN_X = 0x133
BTN_Y = 0x134
BTN_TL = 0x136
BTN_TR = 0x137
BTN_SELECT = 0x13a
BTN_START = 0x13b
BTN_MODE = 0x13c
BTN_THUMBL = 0x13d
BTN_THUMBR = 0x13e

ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02
ABS_RX = 0x03
ABS_RY = 0x04
ABS_RZ = 0x05
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

ABS_CNT = 64
UINPUT_MAX_NAME_SIZE = 80

# XInput button bitmasks -> Linux button codes.
XINPUT_BUTTON_MAP = [
    (0x1000, BTN_A),
    (0x2000, BTN_B),
    (0x4000, BTN_X),
    (0x8000, BTN_Y),
    (0x0100, BTN_TL),       # Left shoulder (LB)
    (0x0200, BTN_TR),       # Right shoulder (RB)
    (0x0020, BTN_SELECT),   # Back
    (0x0010, BTN_START),    # Start
    (0x0040, BTN_THUMBL),   # Left stick click
    (0x0080, BTN_THUMBR),   # Right stick click
    (0x0400, BTN_MODE),     # Guide (only set when the host polls via
                            # XInputGetStateEx; zero otherwise)
]

# XInput D-pad bitmasks (synthesised into HAT axes).
XI_DPAD_UP = 0x0001
XI_DPAD_DOWN = 0x0002
XI_DPAD_LEFT = 0x0004
XI_DPAD_RIGHT = 0x0008

# All buttons we advertise (declared as capabilities).
DECLARED_BUTTONS = [
    BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR,
    BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR,
]

# Axis -> (min, max, fuzz, flat) matching a real Xbox 360 pad.
AXIS_INFO = {
    ABS_X:    (-32768, 32767, 16, 128),
    ABS_Y:    (-32768, 32767, 16, 128),
    ABS_RX:   (-32768, 32767, 16, 128),
    ABS_RY:   (-32768, 32767, 16, 128),
    ABS_Z:    (0, 255, 0, 0),
    ABS_RZ:   (0, 255, 0, 0),
    ABS_HAT0X: (-1, 1, 0, 0),
    ABS_HAT0Y: (-1, 1, 0, 0),
}

FRAME_MAGIC0 = 0xAB
FRAME_MAGIC1 = 0xCD
FRAME_FORMAT = "<BBBBHBBhhhh"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)  # 16

# Reverse-channel rumble frame (this helper -> Windows host over stdout).
# Mirrored by RumbleReader in xinput.py; kept in sync by tests.
RUMBLE_MAGIC0 = 0xFB
RUMBLE_MAGIC1 = 0xFE
RUMBLE_FORMAT = "<BBHH"
RUMBLE_SIZE = struct.calcsize(RUMBLE_FORMAT)  # 6


def pack_rumble(strong, weak):
    """Build a rumble frame: strong (left/low-freq) and weak (right/high-freq)
    motor magnitudes, 0-65535 each."""
    return struct.pack(RUMBLE_FORMAT, RUMBLE_MAGIC0, RUMBLE_MAGIC1,
                       strong & 0xFFFF, weak & 0xFFFF)


class RumbleFrameReader:
    """Resynchronising parser for the 6-byte rumble frames.

    Consumers of the /tmp stream tier write these frames into the ``.ff``
    FIFO to drive the physical controller's motors. This mirrors
    ``xinput.RumbleReader`` on the Windows side; it is duplicated here
    because this helper runs standalone inside WSL and must not import
    other com2tty modules.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Add raw bytes, yield every complete (strong, weak) rumble pair."""
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
            _, _, strong, weak = struct.unpack(
                RUMBLE_FORMAT, bytes(self._buf[:RUMBLE_SIZE]))
            del self._buf[:RUMBLE_SIZE]
            frames.append((strong, weak))
        return frames


def _clamp(value, lo, hi):
    return lo if value < lo else (hi if value > hi else value)


def build_uinput_user_dev(name, vendor, product, version, bustype=0x03,
                          ff_effects_max=0):
    """Pack a ``struct uinput_user_dev`` (legacy device-creation method).

    Layout (no padding inserted on x86_64)::

        char  name[80];
        struct input_id { u16 bustype, vendor, product, version; };
        u32   ff_effects_max;
        s32   absmax[64]; absmin[64]; absfuzz[64]; absflat[64];
    """
    name_bytes = name.encode("utf-8")[:UINPUT_MAX_NAME_SIZE - 1]

    absmax = [0] * ABS_CNT
    absmin = [0] * ABS_CNT
    absfuzz = [0] * ABS_CNT
    absflat = [0] * ABS_CNT
    for code, (mn, mx, fz, fl) in AXIS_INFO.items():
        absmin[code] = mn
        absmax[code] = mx
        absfuzz[code] = fz
        absflat[code] = fl

    return struct.pack(
        "=%dsHHHHI%di" % (UINPUT_MAX_NAME_SIZE, ABS_CNT * 4),
        name_bytes,
        bustype, vendor, product, version,
        ff_effects_max,
        *(absmax + absmin + absfuzz + absflat),
    )


def encode_event(etype, code, value):
    """Pack a ``struct input_event`` for a 64-bit kernel.

    ``struct timeval`` is two 64-bit longs on x86_64; the kernel timestamps
    uinput writes itself, so zeros are fine.
    """
    return struct.pack("=qqHHi", 0, 0, etype, code, value)


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


def state_to_events(state):
    """Translate a parsed controller state into a list of (type, code, value).

    When the controller is disconnected, everything is reported as neutral so
    the virtual device stays present but idle (avoids the device vanishing
    mid-game).
    """
    events = []
    connected = state["connected"]
    buttons = state["buttons"] if connected else 0

    for mask, code in XINPUT_BUTTON_MAP:
        events.append((EV_KEY, code, 1 if (buttons & mask) else 0))

    # D-pad -> HAT axes (Linux convention: up/left are negative).
    hat_x = 0
    hat_y = 0
    if buttons & XI_DPAD_LEFT:
        hat_x = -1
    elif buttons & XI_DPAD_RIGHT:
        hat_x = 1
    if buttons & XI_DPAD_UP:
        hat_y = -1
    elif buttons & XI_DPAD_DOWN:
        hat_y = 1
    events.append((EV_ABS, ABS_HAT0X, hat_x))
    events.append((EV_ABS, ABS_HAT0Y, hat_y))

    if connected:
        lx, ly, rx, ry = state["lx"], state["ly"], state["rx"], state["ry"]
        lt, rt = state["lt"], state["rt"]
    else:
        lx = ly = rx = ry = 0
        lt = rt = 0

    # Y axes are inverted: XInput up is positive, Linux ABS down is positive.
    events.append((EV_ABS, ABS_X, _clamp(lx, -32768, 32767)))
    events.append((EV_ABS, ABS_Y, _clamp(-ly, -32768, 32767)))
    events.append((EV_ABS, ABS_RX, _clamp(rx, -32768, 32767)))
    events.append((EV_ABS, ABS_RY, _clamp(-ry, -32768, 32767)))
    events.append((EV_ABS, ABS_Z, _clamp(lt, 0, 255)))
    events.append((EV_ABS, ABS_RZ, _clamp(rt, 0, 255)))
    return events


class FrameReader:
    """Resynchronising parser for the fixed-length frame stream.

    Tolerates partial reads and stray bytes by scanning for the magic prefix.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Add raw bytes, yield every complete, magic-aligned frame."""
        self._buf.extend(data)
        frames = []
        while True:
            # Drop bytes until the buffer starts with a plausible magic byte.
            start = self._buf.find(FRAME_MAGIC0)
            if start == -1:
                self._buf.clear()
                break
            if start > 0:
                del self._buf[:start]
            if len(self._buf) < FRAME_SIZE:
                break
            if self._buf[1] != FRAME_MAGIC1:
                # False positive on magic0; skip it and keep scanning.
                del self._buf[0]
                continue
            frame = bytes(self._buf[:FRAME_SIZE])
            del self._buf[:FRAME_SIZE]
            parsed = parse_frame(frame)
            if parsed is not None:
                frames.append(parsed)
        return frames


SETUP_INSTRUCTIONS = """\
[CONTROL] PAD_PERMISSION_ERROR
--------------------------------------------------------------------
com2tty gamepad bridge cannot access /dev/uinput.

This needs a ONE-TIME setup with root (com2tty itself never needs
administrator at runtime). Run these once inside WSL:

  sudo modprobe uinput
  sudo chmod 0666 /dev/uinput

To make it persist across `wsl --shutdown`, add to /etc/wsl.conf:

  [boot]
  command = modprobe uinput && chmod 0666 /dev/uinput

then run `wsl --shutdown` from Windows and start com2tty again.
--------------------------------------------------------------------"""


def encode_report(events):
    """Serialise a list of (type, code, value) plus a trailing SYN_REPORT.

    This is exactly the byte stream a real ``/dev/input/eventN`` emits, so both
    sinks (uinput and the /tmp FIFO) share it and a single reader is portable.
    """
    blob = bytearray()
    for etype, code, value in events:
        blob += encode_event(etype, code, value)
    blob += encode_event(EV_SYN, SYN_REPORT, 0)
    return bytes(blob)


class GamepadSink:
    """Common base: turns controller events into an evdev report and writes it."""

    label = "gamepad"

    def open(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def _write(self, blob):  # pragma: no cover - overridden
        raise NotImplementedError

    def emit(self, events):
        self._write(encode_report(events))

    def ff_fileno(self):
        """File descriptor to watch for force-feedback requests, or None.

        Only the uinput sink has a kernel-driven reverse channel; the /tmp
        FIFO stream is one-way.
        """
        return None

    def handle_ff_io(self):  # pragma: no cover - overridden where used
        return None

    def close(self):  # pragma: no cover - overridden
        pass


class TmpStreamGamepad(GamepadSink):
    """Default, root-free sink: an evdev event stream over a ``/tmp`` FIFO.

    Mirrors the serial bridge's ``/tmp/ttyUSB0`` approach. The FIFO is opened
    O_RDWR so the bridge always holds a reader end (writes never raise on a
    missing consumer); when no real consumer is draining and the pipe buffer
    fills, writes are dropped (only the latest controller state matters).

    Force feedback: alongside the event FIFO a second FIFO is created at
    ``<path>.ff``. A consumer writes 6-byte rumble frames (see
    ``pack_rumble``) into it to drive the physical controller's motors --
    the same reverse channel the uinput tier gets from the kernel.
    """

    def __init__(self, path=DEFAULT_TMP_PAD,
                 name="Microsoft X-Box 360 pad", **_ignored):
        self.path = path
        self.ff_path = path + ".ff"
        self.name = name
        self.fd = None
        self.ff_fd = None
        self._ff_reader = RumbleFrameReader()

    @staticmethod
    def _open_fifo(path):
        # Replace a stale non-FIFO file if one is in the way.
        if os.path.lexists(path):
            try:
                if not stat.S_ISFIFO(os.stat(path).st_mode):
                    os.unlink(path)
            except OSError:
                os.unlink(path)
        if not os.path.exists(path):
            os.mkfifo(path, 0o666)
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def open(self):
        self.fd = self._open_fifo(self.path)
        self.ff_fd = self._open_fifo(self.ff_path)

    def _write(self, blob):
        try:
            os.write(self.fd, blob)
        except BlockingIOError:
            pass  # no consumer / buffer full -> drop, keep latest state policy
        except OSError:
            pass

    def ff_fileno(self):
        return self.ff_fd

    def handle_ff_io(self):
        """Read rumble frames a consumer wrote into the ``.ff`` FIFO.

        Returns the most recent complete (strong, weak) pair, or None.
        """
        try:
            data = os.read(self.ff_fd, 4096)
        except (BlockingIOError, OSError):
            return None
        if not data:
            return None
        frames = self._ff_reader.feed(data)
        return frames[-1] if frames else None

    def close(self):
        for attr in ("fd", "ff_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
                setattr(self, attr, None)
        for path in (self.path, self.ff_path):
            try:
                if os.path.lexists(path):
                    os.unlink(path)
            except Exception:
                pass


class UinputGamepad(GamepadSink):
    """Opt-in system-wide sink: a real device via ``/dev/uinput``."""

    label = "uinput"

    def __init__(self, name="Microsoft X-Box 360 pad",
                 vendor=0x045e, product=0x028e, version=0x0110, **_ignored):
        self.name = name
        self.vendor = vendor
        self.product = product
        self.version = version
        self.fd = None
        # FF effect id -> (strong, weak) rumble magnitudes, populated by the
        # kernel-driven upload handshake in handle_ff_io.
        self._effects = {}

    def open(self):
        if fcntl is None:  # pragma: no cover
            raise OSError("fcntl unavailable (not running on Linux)")
        # The ioctl numbers and struct layouts here assume a 64-bit (LP64)
        # kernel ABI (true for both x86_64 and aarch64 WSL). On a 32-bit ABI
        # the uinput_user_dev/input_event packing would be wrong; fail clearly
        # so _open_sink falls back to the portable /tmp stream instead.
        if struct.calcsize("P") != 8:
            raise OSError(
                "uinput mode requires a 64-bit (LP64) kernel ABI; this "
                "interpreter is not 64-bit. Use the default /tmp stream.")
        # O_RDWR: reads carry the kernel's force-feedback requests back to us.
        self.fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)

        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_FF)
        for code in DECLARED_BUTTONS:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in AXIS_INFO:
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
        fcntl.ioctl(self.fd, UI_SET_FFBIT, FF_RUMBLE)

        dev = build_uinput_user_dev(self.name, self.vendor,
                                    self.product, self.version,
                                    ff_effects_max=FF_MAX_EFFECTS)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def _write(self, blob):
        os.write(self.fd, blob)

    def ff_fileno(self):
        return self.fd

    def handle_ff_io(self):
        """Service one kernel force-feedback request from the uinput fd.

        Effect uploads and erasures are acknowledged via the
        UI_BEGIN/END_FF_UPLOAD/ERASE handshake and tracked in ``_effects``;
        a play/stop request returns the (strong, weak) magnitudes to forward
        to the Windows host, all other events return None.
        """
        try:
            data = os.read(self.fd, INPUT_EVENT_SIZE)
        except (BlockingIOError, OSError):
            return None
        if len(data) < INPUT_EVENT_SIZE:
            return None
        _, _, etype, code, value = struct.unpack(INPUT_EVENT_FORMAT, data)

        if etype == EV_UINPUT and code == UI_FF_UPLOAD:
            buf = bytearray(UINPUT_FF_UPLOAD_SIZE)
            struct.pack_into("<I", buf, 0, value & 0xFFFFFFFF)
            try:
                fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, buf)
                eff_type, eff_id = struct.unpack_from("<HH", buf,
                                                      FF_EFFECT_OFFSET)
                if eff_type == FF_RUMBLE:
                    strong, weak = struct.unpack_from(
                        "<HH", buf, FF_EFFECT_OFFSET + FF_RUMBLE_UNION_OFFSET)
                    self._effects[eff_id] = (strong, weak)
                struct.pack_into("<i", buf, 4, 0)  # retval = success
                fcntl.ioctl(self.fd, UI_END_FF_UPLOAD, buf)
            except OSError:
                pass
            return None

        if etype == EV_UINPUT and code == UI_FF_ERASE:
            buf = bytearray(UINPUT_FF_ERASE_SIZE)
            struct.pack_into("<I", buf, 0, value & 0xFFFFFFFF)
            try:
                fcntl.ioctl(self.fd, UI_BEGIN_FF_ERASE, buf)
                (effect_id,) = struct.unpack_from("<I", buf, 8)
                self._effects.pop(effect_id, None)
                struct.pack_into("<i", buf, 4, 0)
                fcntl.ioctl(self.fd, UI_END_FF_ERASE, buf)
            except OSError:
                pass
            return None

        if etype == EV_FF:
            if code in (FF_GAIN, FF_AUTOCENTER):
                return None  # device-level knobs; nothing to forward
            if value:
                return self._effects.get(code, (0, 0))
            return (0, 0)

        return None

    def close(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except Exception:
                pass
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


def _open_sink(use_uinput, tmp_path, name):
    """Resolve the sink, mirroring the serial bridge's /dev->/tmp fallback.

    Returns ``(sink, ready_message)``. In --uinput mode, if /dev/uinput is not
    accessible we print the one-time setup instructions and transparently fall
    back to the root-free /tmp stream so the bridge still works.
    """
    if use_uinput:
        sink = UinputGamepad(name=name)
        try:
            sink.open()
            return sink, ("[CONTROL] PAD_READY: real device '%s' created "
                          "(/dev/input/event*). Bridge active." % name)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            sys.stderr.write(
                "[CONTROL] PAD_UINPUT_UNAVAILABLE: %s\n" % exc)
            sys.stderr.write(SETUP_INSTRUCTIONS + "\n")
            sys.stderr.write(
                "Falling back to the root-free /tmp stream at %s ...\n"
                % tmp_path)
            sys.stderr.flush()

    sink = TmpStreamGamepad(path=tmp_path, name=name)
    sink.open()
    return sink, ("[CONTROL] PAD_READY: evdev stream at %s (no root, "
                  "rumble at %s.ff). Bridge active." % (tmp_path, tmp_path))


def main(argv=None):  # pragma: no cover - integration entry point
    parser = argparse.ArgumentParser(description="com2tty WSL Gamepad Bridge")
    parser.add_argument("-i", "--pad-index", type=int, default=0,
                        help="Controller index this bridge represents (0-3).")
    parser.add_argument("-n", "--name", default="Microsoft X-Box 360 pad",
                        help="Virtual device name advertised to Linux.")
    parser.add_argument("-u", "--uinput", action="store_true",
                        help="Create a real /dev/input device via /dev/uinput "
                             "(needs one-time root setup). Default is the "
                             "root-free /tmp evdev stream.")
    parser.add_argument("-p", "--tmp-path", default=DEFAULT_TMP_PAD,
                        help="FIFO path for the /tmp stream sink "
                             "(default: %s)." % DEFAULT_TMP_PAD)
    args = parser.parse_args(argv)

    try:
        pad, ready_msg = _open_sink(args.uinput, args.tmp_path, args.name)
    except Exception as exc:
        sys.stderr.write(f"[CONTROL] PAD_ERROR: cannot init sink: {exc}\n")
        sys.stderr.flush()
        return 13

    sys.stderr.write(ready_msg + "\n")
    sys.stderr.flush()

    reader = FrameReader()
    # Emit an initial neutral state so the device reads as centred/idle.
    pad.emit(state_to_events(parse_frame(struct.pack(
        FRAME_FORMAT, FRAME_MAGIC0, FRAME_MAGIC1, args.pad_index,
        0, 0, 0, 0, 0, 0, 0, 0))))

    # The uinput sink exposes a second readable fd that carries the kernel's
    # force-feedback requests; rumble magnitudes go back to the Windows host
    # over stdout (the reverse channel of the bridge pipe).
    ff_fd = pad.ff_fileno()
    watch = [0] if ff_fd is None else [0, ff_fd]

    try:
        while True:
            readable, _, _ = select.select(watch, [], [])

            if ff_fd is not None and ff_fd in readable:
                rumble = pad.handle_ff_io()
                if rumble is not None:
                    sys.stdout.buffer.write(pack_rumble(*rumble))
                    sys.stdout.buffer.flush()

            if 0 not in readable:
                continue
            data = os.read(0, 4096)
            if not data:
                sys.stderr.write("EOF on stdin. Exiting.\n")
                sys.stderr.flush()
                break
            for state in reader.feed(data):
                if state["index"] != args.pad_index:
                    continue
                pad.emit(state_to_events(state))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(f"[CONTROL] PAD_ERROR: {exc}\n")
        sys.stderr.flush()
    finally:
        pad.close()
        sys.stderr.write("WSL gamepad bridge shut down.\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
