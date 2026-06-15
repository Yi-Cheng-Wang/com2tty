"""Session liveness markers.

Several resources a bridge session creates (the TCP listeners, the rc-file
environment block, the picotool interception) used to be reclaimed blindly
on the next startup, which destroyed them for a *concurrently running*
session as well. Liveness is tracked two ways: a per-port heartbeat file
under /tmp refreshed by the main loop (so a SIGKILLed session goes stale
within ALIVE_TTL), and the owning PID recorded in shared artifacts, checked
against /proc.
"""
import os
import time

from ..core.constants import (  # noqa: F401 (re-exported for callers)
    ALIVE_FILE_TEMPLATE,
    ALIVE_TOUCH_INTERVAL,
    ALIVE_TTL,
    PICOTOOL_OWNER_FILE,
)
from .secure_io import secure_write


def alive_file_path(port):
    return ALIVE_FILE_TEMPLATE % int(port)


def touch_alive_files(ports):
    """Refresh the heartbeat files that mark this session's ports as live.

    Written via secure_write so a symlink planted at the fixed heartbeat path
    under sticky /tmp cannot redirect the write (see wsl.secure_io).
    """
    for port in ports:
        try:
            secure_write(alive_file_path(port), str(os.getpid()), mode=0o600)
        except Exception:
            pass


def remove_alive_files(ports):
    for port in ports:
        try:
            os.remove(alive_file_path(port))
        except OSError:
            pass


def is_port_session_alive(port, ttl=ALIVE_TTL):
    """True when a *different*, live com2tty session is heartbeating this port.

    A fresh mtime alone is not enough to declare a port held: the marker may
    be our own (we register the heartbeat from the same process that reclaims
    the port), or it may have been left moments ago by a session that has
    since crashed. The marker records the owning PID, so confirm it belongs to
    a live com2tty process other than ourselves before reporting it held.
    """
    path = alive_file_path(port)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    if (time.time() - mtime) >= ttl:
        return False
    owner = read_pid_file(path)
    if owner is None:
        return True  # legacy marker without a PID: trust freshness alone
    if owner == os.getpid():
        return False  # our own heartbeat is not "another live session"
    return pid_alive(owner)


def pid_alive(pid):
    """True when the given PID is a running *com2tty* process in this distro.

    Checking only that ``/proc/<pid>`` exists would be fooled by PID
    recycling: once a crashed session's PID is reused by an unrelated
    process, orphan cleanup would treat it as still live and refuse to
    reclaim the leftover resources. Confirm the command line still looks
    like com2tty's WSL helper before trusting the PID.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read()
    except OSError:
        return False
    # Require BOTH markers: the helper is always launched by path as
    # ``python3 -u .../com2tty/bridge.py`` (or pad_bridge.py, which also
    # contains "bridge.py"). The previous OR matched any unrelated process
    # whose command line merely mentioned "com2tty" or some other bridge.py,
    # which could wrongly keep a recycled PID "alive" and block reclamation.
    return b"com2tty" in cmdline and b"bridge.py" in cmdline


def read_pid_file(path):
    """Read an integer PID from a marker file, or None."""
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None
