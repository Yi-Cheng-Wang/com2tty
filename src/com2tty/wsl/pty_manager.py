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


def create_symlink_with_fallback(slave_name, target_path):
    """Symlink the pty slave at ``target_path``, falling back to /tmp.

    The desired path may live somewhere only root can write (e.g. /dev);
    rather than demand sudo, fall back to a user-writable /tmp path and
    print the one-time command that creates the privileged alias.

    Returns the path actually created. Raises when even the fallback fails.
    """
    try:
        if os.path.lexists(target_path):
            os.unlink(target_path)
        os.symlink(slave_name, target_path)
        sys.stderr.write(f"Successfully symlinked {target_path} -> {slave_name}\n")
        sys.stderr.flush()
        return target_path
    except PermissionError:
        # Fallback to /tmp if write permission to dev is denied
        basename = os.path.basename(target_path)
        fallback_path = f"/tmp/{basename}"
        sys.stderr.write(f"Warning: Permission denied creating symlink at {target_path}.\n")
        sys.stderr.write(f"Attempting fallback to user-writable path: {fallback_path}...\n")
        sys.stderr.flush()

        if os.path.lexists(fallback_path):
            os.unlink(fallback_path)
        os.symlink(slave_name, fallback_path)

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
