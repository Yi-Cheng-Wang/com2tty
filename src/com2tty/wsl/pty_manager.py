"""The pseudo-terminal that impersonates a serial device inside WSL.

``os.openpty()`` gives the bridge a master/slave pair: the slave side is
symlinked to a stable path (``/tmp/ttyUSB0`` by default) and opened by user
programs as if it were a real serial device; the master side is what the
bridge pumps to and from the Windows COM port. This module owns the pty
primitives -- creation, the user-writable ``/tmp`` symlink fallback, and
the termios polling that detects when a client reconfigures the "port".
"""
import os
import sys
import termios

#: termios speed constant -> integer baud rate (B115200 -> 115200, ...).
baud_map = {getattr(termios, k): int(k[1:]) for k in dir(termios)
            if k.startswith('B') and k[1:].isdigit()}


def get_pty_settings(fd):
    try:
        attrs = termios.tcgetattr(fd)
        speed = attrs[5]
        # B0 is the termios hangup pseudo-rate; report it as "no baud" so the
        # host never tries to apply a 0 baudrate to the Windows port.
        baud = baud_map.get(speed) or None
        cflag = attrs[2]
        cs_mask = termios.CS5 | termios.CS6 | termios.CS7 | termios.CS8
        cs_val = cflag & cs_mask
        bytesize_map = {termios.CS5: 5, termios.CS6: 6, termios.CS7: 7, termios.CS8: 8}
        bytesize = bytesize_map.get(cs_val, 8)
        if cflag & termios.PARENB:
            parity = 'O' if (cflag & termios.PARODD) else 'E'
        else:
            parity = 'N'
        stopbits = '2' if (cflag & termios.CSTOPB) else '1'
        return baud, bytesize, parity, stopbits
    except Exception:
        return None, None, None, None


def open_pty():
    """Create the pty pair; returns ``(master_fd, slave_fd, slave_name)``.

    Both descriptors must stay open for the bridge's lifetime: keeping the
    slave open prevents EIO errors on the master side when WSL clients open
    and close the virtual serial port.
    """
    master_fd, slave_fd = os.openpty()
    slave_name = os.ttyname(slave_fd)
    sys.stderr.write(f"Created pseudo-terminal: master_fd={master_fd}, slave={slave_name}\n")
    sys.stderr.flush()
    return master_fd, slave_fd, slave_name


def _refuse_if_foreign_live_pty(path, our_slave):
    """Refuse to replace a link pointing at another live session's pty slave.

    The tty path (default /tmp/ttyUSB0) is shared across sessions. Without this
    guard a second session would unlink the first's link and repoint it at its
    own pty, silently stealing the first session's serial endpoint. A dangling
    link (the previous owner's pty is gone) is fair game and is left for the
    normal unlink/symlink below to replace.
    """
    if not os.path.islink(path):
        return
    try:
        dest = os.readlink(path)
    except OSError:
        return
    if dest != our_slave and dest.startswith("/dev/pts/") and os.path.exists(dest):
        raise FileExistsError(
            f"{path} is already bound to another live com2tty session's serial "
            f"device ({dest}); refusing to hijack it. Use a different --wsl-tty "
            f"path for this session.")


def _link_fallback(slave_name, fallback_path, basename):
    """Create the /tmp fallback link, dodging the sticky-bit ownership trap.

    /tmp is world-writable but sticky (+t): a pre-existing ``/tmp/ttyUSB0``
    owned by another user cannot be unlinked, so ``os.unlink`` raises
    ``PermissionError``. Rather than crash, retreat to a user-scoped path
    (``/tmp/ttyUSB0_<user>``, then ``..._<pid>``) that this process owns.
    """
    candidates = [fallback_path]
    try:
        import getpass
        # getpass.getuser() trusts user-controllable env vars (USER/LOGNAME);
        # take only the final path component so a value like "../x" cannot
        # steer the symlink out of /tmp.
        username = os.path.basename(getpass.getuser())
        if username:
            candidates.append(f"/tmp/{basename}_{username}")
    except Exception:
        pass
    candidates.append(f"/tmp/{basename}_{os.getpid()}")

    last_err = None
    for path in candidates:
        try:
            _refuse_if_foreign_live_pty(path, slave_name)
            if os.path.lexists(path):
                os.unlink(path)
            os.symlink(slave_name, path)
            return path
        except PermissionError as exc:
            last_err = exc
            sys.stderr.write(
                f"Warning: cannot use {path} (permission denied, sticky /tmp?); "
                "trying a user-scoped path.\n")
            sys.stderr.flush()
            continue
    raise last_err


def create_symlink_with_fallback(slave_name, target_path):
    """Symlink the pty slave at ``target_path``, falling back to /tmp.

    The desired path may live somewhere only root can write (e.g. /dev);
    rather than demand sudo, fall back to a user-writable /tmp path and
    print the one-time command that creates the privileged alias.

    Returns the path actually created. Raises when even the fallback fails, or
    when the target already belongs to another live session (anti-hijack).
    """
    # Checked before the try so the anti-hijack error propagates instead of
    # being swallowed into the /tmp fallback below.
    _refuse_if_foreign_live_pty(target_path, slave_name)
    try:
        if os.path.lexists(target_path):
            os.unlink(target_path)
        os.symlink(slave_name, target_path)
        sys.stderr.write(f"Successfully symlinked {target_path} -> {slave_name}\n")
        sys.stderr.flush()
        return target_path
    except OSError:
        # Fall back to /tmp for any filesystem reason the target path is
        # unusable -- not just permission denied, but also a read-only
        # filesystem (EROFS) or a missing parent directory (ENOENT).
        basename = os.path.basename(target_path)
        fallback_path = f"/tmp/{basename}"
        sys.stderr.write(f"Warning: Permission denied creating symlink at {target_path}.\n")
        sys.stderr.write(f"Attempting fallback to user-writable path: {fallback_path}...\n")
        sys.stderr.flush()

        fallback_path = _link_fallback(slave_name, fallback_path, basename)

        sys.stderr.write(f"Fallback successful: {fallback_path} -> {slave_name}\n")
        sys.stderr.write("--------------------------------------------------\n")
        sys.stderr.write(f"To use the desired device path '{target_path}', please run this command ONCE in WSL:\n")
        sys.stderr.write(f"  sudo ln -sf {fallback_path} {target_path}\n")
        sys.stderr.write("--------------------------------------------------\n")
        sys.stderr.flush()
        return fallback_path


def cleanup_symlink(path):
    try:
        if os.path.lexists(path):
            os.unlink(path)
            sys.stderr.write(f"Removed symlink {path}\n")
            sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to remove symlink {path}: {e}\n")
        sys.stderr.flush()
