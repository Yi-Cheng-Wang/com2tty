"""Symlink-safe writes for the predictable ``/tmp`` paths the bridge creates.

The WSL helper writes several artifacts at fixed paths under the world-writable
``/tmp`` (the picotool wrapper, its owner file, the per-port heartbeats). On a
shared WSL instance another local user can pre-create those paths as symlinks
(the sticky bit stops them deleting *our* files, not creating their own); a
naive ``open(path, "w")`` would then follow the link and write through it.

This mirrors the hardening ``evdev_sink._open_fifo`` already applies to the
gamepad FIFO: drop any pre-existing entry, then create the file with
``O_EXCL | O_NOFOLLOW`` so a link planted in the race window is rejected
(``ELOOP``) rather than followed, and a foreign regular file left in sticky
``/tmp`` (which we cannot unlink) makes the open fail loudly instead of letting
us write through someone else's file.
"""
import os

# O_NOFOLLOW is POSIX-only; on the Windows test interpreter it is absent, and
# these /tmp paths are never used there anyway, so degrade to 0 (no-op).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def secure_write(path, data, mode=0o600):
    """Write ``data`` (str or bytes) to ``path`` without ever following a link.

    Unlinks any pre-existing entry first (our own stale file, or an attacker's
    planted symlink), then creates a fresh file with ``O_EXCL | O_NOFOLLOW``.
    ``mode`` is applied to the freshly created file. A ``PermissionError`` (a
    foreign file we cannot remove from sticky ``/tmp``) or ``FileExistsError``/
    ``OSError`` (a link planted in the race window) propagates to the caller,
    which treats the artifact as unavailable rather than risking a write to a
    file it does not control.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
