"""
Pytest configuration for com2tty tests.

Provides cross-platform compatibility by mocking the Unix-only ``termios``
module on Windows so that ``bridge.py`` can be imported for testing.
"""
import sys
import types

# ---------------------------------------------------------------------------
# Mock ``termios`` on platforms where it is unavailable (Windows).
# This MUST happen at module level – before any test file imports bridge.py.
# ---------------------------------------------------------------------------
try:
    import termios  # noqa: F401
except ImportError:
    _termios = types.ModuleType("termios")

    # Baud-rate constants  (B<rate> → speed_t value, matching glibc)
    for _name, _val in [
        ("B0", 0), ("B50", 1), ("B75", 2), ("B110", 3), ("B134", 4),
        ("B150", 5), ("B200", 6), ("B300", 7), ("B600", 8), ("B1200", 9),
        ("B1800", 10), ("B2400", 11), ("B4800", 12), ("B9600", 13),
        ("B19200", 14), ("B38400", 15),
        ("B57600", 4097), ("B115200", 4098), ("B230400", 4099),
    ]:
        setattr(_termios, _name, _val)

    # Character-size masks
    _termios.CS5 = 0x00
    _termios.CS6 = 0x10
    _termios.CS7 = 0x20
    _termios.CS8 = 0x30

    # Parity / stop-bit flags
    _termios.PARENB = 0x100
    _termios.PARODD = 0x200
    _termios.CSTOPB = 0x40

    def _tcgetattr(fd):
        raise OSError("mock termios – no real terminal")

    _termios.tcgetattr = _tcgetattr

    sys.modules["termios"] = _termios
