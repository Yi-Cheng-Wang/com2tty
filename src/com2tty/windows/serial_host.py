"""The Windows COM port: settings, baud detection, and hot-plug recovery.

Everything that touches the pyserial handle's lifecycle lives here -- the
mapping of CLI values to pyserial constants, locale-independent baud rate
detection via Win32 ``GetCommState``, and the reconnect strategies that
follow a device across unplug/replug and bootloader re-enumeration.
"""
import logging
import os
import subprocess
import threading
import time

import serial
import serial.tools.list_ports

from ..core.boards import classify_vid
from .os_hacks import device_watcher as devnotify


def get_serial_settings(bytesize, parity, stopbits):
    bytesize_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS
    }
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "S": serial.PARITY_SPACE,
        "M": serial.PARITY_MARK
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO
    }

    return (
        bytesize_map.get(bytesize, serial.EIGHTBITS),
        parity_map.get(parity, serial.PARITY_NONE),
        stopbits_map.get(stopbits, serial.STOPBITS_ONE)
    )


def get_commstate_baudrate(port, _kernel32=None):
    """Read the configured baud rate straight from Win32 ``GetCommState``.

    Locale-independent, unlike parsing mode.com output, whose field labels
    are translated on non-English Windows ("Bits par seconde", "波特率", ...)
    and defeat any keyword regex. ``_kernel32`` is injectable for tests.
    """
    import ctypes

    class _DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", ctypes.c_uint32),
            ("BaudRate", ctypes.c_uint32),
            ("fFlags", ctypes.c_uint32),
            ("wReserved", ctypes.c_uint16),
            ("XonLim", ctypes.c_uint16),
            ("XoffLim", ctypes.c_uint16),
            ("ByteSize", ctypes.c_ubyte),
            ("Parity", ctypes.c_ubyte),
            ("StopBits", ctypes.c_ubyte),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", ctypes.c_uint16),
        ]

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    invalid_handle = ctypes.c_void_p(-1).value

    try:
        kernel32 = _kernel32 if _kernel32 is not None else ctypes.windll.kernel32
        # Declare the Win32 signatures so 64-bit handles/pointers are not
        # truncated to ctypes' default 32-bit int.
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.GetCommState.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.GetCommState.restype = ctypes.c_int  # BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int  # BOOL
        # The \\.\ prefix is required for COM10 and above, harmless below.
        handle = kernel32.CreateFileW(
            "\\\\.\\" + port, GENERIC_READ | GENERIC_WRITE, 0, None,
            OPEN_EXISTING, 0, None)
        if not handle or handle == invalid_handle:
            return None
        try:
            dcb = _DCB()
            dcb.DCBlength = ctypes.sizeof(_DCB)
            if kernel32.GetCommState(handle, ctypes.byref(dcb)) and dcb.BaudRate:
                return int(dcb.BaudRate)
        finally:
            kernel32.CloseHandle(handle)
    except Exception as e:
        logging.debug(f"GetCommState baud detection failed for {port}: {e}")
    return None


def get_system_baudrate(port):
    import re

    baud = get_commstate_baudrate(port)
    if baud:
        return baud

    try:
        # Fallback: parse mode.com output. The console codepage may not match
        # Python's locale decoding; the digits we need are ASCII, so replace
        # anything undecodable.
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        res = subprocess.run(["mode.com", port], capture_output=True, text=True,
                             errors="replace", creationflags=creationflags)
        if res.returncode == 0:
            # Prefer the number on the line that names the baud field. Most
            # locales still print the English word "Baud", but other numbers
            # (e.g. a localized date or the COM index) can precede it, so a
            # bare "first large number" scan would mis-detect on those systems.
            match = re.search(r"[Bb]aud[^0-9]*(\d{3,7})", res.stdout)
            if not match:
                match = re.search(r"\b(\d{3,7})\b", res.stdout)
            if match:
                return int(match.group(1))
    except Exception as e:
        logging.debug(f"Failed to auto-detect baudrate for {port}: {e}")
    return None


def detect_board_type(port_name):
    """Detect board type from USB VID/PID at startup. Much more reliable than runtime detection."""
    for port in serial.tools.list_ports.comports():
        if port.device == port_name:
            return classify_vid(port.vid)
    return "unknown"


def get_usb_serial_number(port_name):
    """Find the USB Serial Number (hwid SER=...) for a given COM port."""
    for port in serial.tools.list_ports.comports():
        if port.device == port_name:
            # hwid format example: USB VID:PID=2E8A:F00F SER=98C4FFA253A63FB7 LOCATION=1-6:x.0
            if 'SER=' in port.hwid:
                parts = port.hwid.split()
                for part in parts:
                    if part.startswith('SER='):
                        return part[4:]
    return None


def _poll_wait(interval):
    """Sleep one poll interval, waking early on a device-change event.

    Used by the device-polling loops (hot-plug reconnect, bootloader-port
    acquisition, --wait). When the WM_DEVICECHANGE watcher is running, a
    plug/unplug wakes the loop immediately; otherwise this is a plain sleep.
    """
    watcher = devnotify.get_watcher()
    if watcher is not None:
        watcher.wait(interval)
    else:
        time.sleep(interval)


def snapshot_ports():
    """Set of COM port device names currently enumerated by Windows."""
    return {p.device for p in serial.tools.list_ports.comports()}


def reopen_serial_port(ser, usb_serial, shutdown_event, abort_events=(),
                       max_attempts=None, poll_interval=0.5, ser_lock=None):
    """Reopen a dead COM handle, following the device across re-enumeration.

    Windows invalidates the open handle when a device is unplugged or reboots
    (e.g. into a bootloader), and may assign a different COM number when it
    returns. This closes the stale handle, then retries the original name and
    falls back to locating the device by its USB serial number.

    ``ser_lock`` serialises the close/open/rename steps against the other
    threads that touch the same port (the dynamic-SETTINGS handler reopens it
    too); without it the two can interleave close/open on one handle.

    Returns True once the port is open again. Returns False when
    ``max_attempts`` (None = unlimited) is exhausted, ``shutdown_event`` is
    set, or any event in ``abort_events`` becomes set (used to yield to the
    UF2 upload path, which manages its own reopen).
    """
    if ser_lock is None:
        ser_lock = threading.Lock()
    original_port = ser.port
    with ser_lock:
        try:
            ser.close()
        except Exception:
            pass
        # Never reopen at 1200 baud: that would immediately re-trigger the
        # bootloader touch on boards that interpret it.
        if getattr(ser, 'baudrate', 115200) == 1200:
            ser.baudrate = 115200

    attempts = 0
    while not shutdown_event.is_set():
        if any(evt.is_set() for evt in abort_events):
            return False
        if max_attempts is not None and attempts >= max_attempts:
            return False
        attempts += 1
        _poll_wait(poll_interval)

        # First try the port under its current name.
        with ser_lock:
            try:
                ser.open()
                logging.info(f"{ser.port} reopened successfully.")
                return True
            except Exception:
                pass

        # The port number may have changed after re-enumeration; locate the
        # device by USB serial number.
        if usb_serial:
            for p in serial.tools.list_ports.comports():
                if p.serial_number == usb_serial and p.device != ser.port:
                    logging.info(f"Device reappeared as {p.device} (was {original_port}).")
                    with ser_lock:
                        ser.port = p.device
                        try:
                            ser.open()
                            logging.info(f"{p.device} opened successfully.")
                            return True
                        except Exception:
                            pass
    return False


def acquire_new_port(ser, before_ports, shutdown_event, max_attempts=40,
                     poll_interval=0.25):
    """Open the port that newly appears after a bootloader touch.

    A SAMD/Leonardo 1200-baud touch re-enumerates the board's bootloader as a
    *new* COM port, typically with a different VID:PID and USB serial number
    than the application port, so matching by serial number (as
    ``reopen_serial_port`` does) is unreliable. Instead this diffs the live
    port list against ``before_ports`` (snapshotted before the touch) and
    opens whatever appeared -- the approach the Arduino tooling uses.

    Leaves ``ser`` open on the new port and returns its name on success, or
    restores ``ser.port`` and returns None when no new port appears.
    """
    original_port = ser.port
    # Never reopen at 1200 baud: that would immediately re-trigger the touch.
    if getattr(ser, 'baudrate', 115200) == 1200:
        ser.baudrate = 115200

    attempts = 0
    while not shutdown_event.is_set() and attempts < max_attempts:
        attempts += 1
        _poll_wait(poll_interval)
        for device in sorted(snapshot_ports() - before_ports):
            ser.port = device
            try:
                ser.open()
                logging.info(f"[SAMD] Bootloader port appeared as {device} "
                             f"(was {original_port}).")
                return device
            except Exception:
                ser.port = original_port
    return None


class ResetProofSerial:
    """
    Wraps a serial port to block DTR/RTS changes from the RFC 2217 client.

    Both ESP32 and Pico resets are handled manually by the host, so we
    silently absorb all DTR/RTS commands from the RFC 2217 protocol
    to prevent them from interfering with our controlled sequences.

    All other attribute access (baudrate, timeout, read, write, etc.) is
    transparently forwarded to the real serial port via __getattr__/__setattr__.
    """
    def __init__(self, ser):
        object.__setattr__(self, '_ser', ser)
        object.__setattr__(self, '_dtr', False)
        object.__setattr__(self, '_rts', False)

    def __getattr__(self, name):
        if name == 'dtr':
            return object.__getattribute__(self, '_dtr')
        if name == 'rts':
            return object.__getattribute__(self, '_rts')
        return getattr(object.__getattribute__(self, '_ser'), name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        elif name in ('dtr', 'rts'):
            # Silently cache value without applying to hardware
            object.__setattr__(self, f'_{name}', value)
            logging.debug(f"[ResetProof] Blocked {name.upper()} = {value}")
        else:
            # Forward everything else (baudrate, timeout, etc.) to real serial
            setattr(object.__getattribute__(self, '_ser'), name, value)
