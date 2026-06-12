"""
Pytest configuration for com2tty tests.

Provides cross-platform compatibility by mocking the Unix-only ``termios``
module on Windows so that ``bridge.py`` can be imported for testing.
"""
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _no_real_device_watcher(monkeypatch):
    """Keep host tests from spinning up a real WM_DEVICECHANGE message pump.

    host.py reaches the watcher through its module-level `devnotify` import;
    replacing that attribute with an inert stub makes run_bridge tests use
    the plain-sleep fallback. devnotify's own tests import the real module
    directly and are unaffected. Individual tests can override the stub's
    `get_watcher` to exercise the event-driven branch of _poll_wait.
    """
    stub = types.SimpleNamespace(
        start_device_watcher=lambda: None,
        get_watcher=lambda: None,
    )
    import com2tty.host
    monkeypatch.setattr(com2tty.host, "devnotify", stub)
    return stub


@pytest.fixture(autouse=True)
def _isolate_autoplay_marker(tmp_path, monkeypatch):
    """Redirect the temp directory to a throwaway path for every test.

    run_bridge() calls restore_orphaned_autoplay() at startup, which reads a
    marker file from the system temp directory. Without isolation a real marker
    left by an actual crashed session could make tests touch the live registry.
    Patching tempfile.gettempdir keeps _autoplay_marker_path itself exercised
    while pointing it at a per-test scratch directory.
    """
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

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
