"""Interception of PlatformIO's picotool binary.

UF2-family boards are flashed by copying a .uf2 file onto a mass-storage
bootloader drive -- which only Windows can see. PlatformIO inside WSL calls
``picotool`` to do that copy, so the bridge renames the real binary to
``picotool.real`` and symlinks a small wrapper in its place; the wrapper
ships the UF2 image to the bridge's relay port instead. The wrapper script
itself is a packaged asset template with the relay port substituted in.

The swap is undone on shutdown, recorded with the owning session's PID, and
self-healed on the next start if a crashed session left it behind.
"""
import glob
import os
import sys

from ...core.constants import PICOTOOL_OWNER_FILE, PICOTOOL_WRAPPER_PATH
from ..liveness import pid_alive, read_pid_file
from ..secure_io import secure_write

_TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, "assets", "picotool_wrapper.py.in")


def _load_wrapper_template(path=_TEMPLATE_FILE):
    with open(path, "r") as f:
        return f.read()


# Loaded once at import time: setup_picotool_interceptor runs in contexts
# where file I/O is mocked, and a packaged asset that cannot be read is a
# broken installation worth failing on early.
PICOTOOL_WRAPPER_CONTENT = _load_wrapper_template()

intercepted_picotools = []


def render_wrapper_script(uf2_port, token=""):
    """The wrapper script text with the UF2 relay port and session token in."""
    return (PICOTOOL_WRAPPER_CONTENT
            .replace("{port}", str(uf2_port))
            .replace("{token}", token))


def setup_picotool_interceptor(uf2_port, token=""):
    wrapper_path = PICOTOOL_WRAPPER_PATH
    try:
        # Owner-only (0o700): the wrapper embeds this session's UF2 token, so a
        # world-readable wrapper would leak it and defeat the relay auth.
        # secure_write refuses to follow a symlink planted at this fixed /tmp
        # path (see wsl.secure_io).
        secure_write(wrapper_path, render_wrapper_script(uf2_port, token),
                     mode=0o700)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to create picotool wrapper: {e}\n")
        return

    home = os.path.expanduser("~")
    search_pattern = os.path.join(home, ".platformio", "packages", "tool-picotool*", "picotool")
    for picotool_path in glob.glob(search_pattern):
        if os.path.islink(picotool_path) or not os.path.isfile(picotool_path):
            continue
        real_path = picotool_path + ".real"
        try:
            if not os.path.exists(real_path):
                os.rename(picotool_path, real_path)
            if os.path.lexists(picotool_path):
                os.remove(picotool_path)
            os.symlink(wrapper_path, picotool_path)
            intercepted_picotools.append((picotool_path, real_path))
            sys.stderr.write(f"Intercepted picotool at {picotool_path}\n")
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to intercept {picotool_path}: {e}\n")
    if intercepted_picotools:
        # Record ownership so a later session's orphan recovery does not
        # restore the binaries out from under this still-running one.
        try:
            secure_write(PICOTOOL_OWNER_FILE, str(os.getpid()), mode=0o600)
        except Exception:
            pass


def cleanup_picotool_interceptor():
    for picotool_path, real_path in intercepted_picotools:
        try:
            if os.path.lexists(picotool_path):
                os.remove(picotool_path)
            if os.path.exists(real_path):
                os.rename(real_path, picotool_path)
            sys.stderr.write(f"Restored picotool at {picotool_path}\n")
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to restore {picotool_path}: {e}\n")
    if intercepted_picotools:
        try:
            os.remove(PICOTOOL_OWNER_FILE)
        except OSError:
            pass


def restore_orphaned_picotools():
    """Restore picotool binaries left intercepted by a previous, crashed session.

    setup_picotool_interceptor renames the real binary to ``picotool.real`` and
    replaces ``picotool`` with a symlink to our wrapper, relying on the in-process
    cleanup to undo it. If the bridge is killed (e.g. ``wsl --shutdown`` or a host
    ``proc.kill()``) before cleanup runs, that swap persists and the user's
    PlatformIO uploads silently break. This runs on startup and reverses any such
    orphaned swap so the tool self-heals on the next launch.
    """
    owner = read_pid_file(PICOTOOL_OWNER_FILE)
    if owner is not None:
        if owner != os.getpid() and pid_alive(owner):
            # Another live session owns the interception; leave it intact.
            sys.stderr.write(
                f"Note: picotool is intercepted by a live com2tty session "
                f"(PID {owner}); leaving it in place.\n")
            sys.stderr.flush()
            return
        try:
            os.remove(PICOTOOL_OWNER_FILE)
        except OSError:
            pass
    home = os.path.expanduser("~")
    search_pattern = os.path.join(home, ".platformio", "packages", "tool-picotool*", "picotool.real")
    for real_path in glob.glob(search_pattern):
        picotool_path = real_path[:-len(".real")]
        try:
            # Only reclaim when the live path is gone or is a symlink we left
            # behind; never clobber a genuine binary the user reinstalled.
            if os.path.islink(picotool_path) or not os.path.exists(picotool_path):
                if os.path.lexists(picotool_path):
                    os.remove(picotool_path)
                os.rename(real_path, picotool_path)
                sys.stderr.write(f"Restored orphaned picotool at {picotool_path}\n")
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Warning: could not restore orphaned picotool {picotool_path}: {e}\n")
            sys.stderr.flush()
