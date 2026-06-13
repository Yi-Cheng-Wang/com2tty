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


def alive_file_path(port):
    return ALIVE_FILE_TEMPLATE % int(port)


def touch_alive_files(ports):
    """Refresh the heartbeat files that mark this session's ports as live."""
    for port in ports:
        try:
            with open(alive_file_path(port), "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass


def remove_alive_files(ports):
    for port in ports:
        try:
            os.remove(alive_file_path(port))
        except OSError:
            pass


def is_port_session_alive(port, ttl=ALIVE_TTL):
    """True when a live com2tty session is heartbeating this port."""
    try:
        mtime = os.stat(alive_file_path(port)).st_mtime
    except OSError:
        return False
    return (time.time() - mtime) < ttl


def pid_alive(pid):
    """True when the given PID is a running process in this distro."""
    try:
        return os.path.isdir("/proc/%d" % int(pid))
    except (TypeError, ValueError):
        return False


def read_pid_file(path):
    """Read an integer PID from a marker file, or None."""
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None
