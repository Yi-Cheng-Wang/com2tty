"""COM port discovery for ``com2tty --list``.

Enumerates the serial ports Windows can see -- including the USB bus
location ("bus id", e.g. ``2-6``) read straight from the device descriptor,
so no usbipd is needed -- and classifies the board family by USB VID.
"""
import shutil
import subprocess

import serial.tools.list_ports

from ..core.boards import classify_vid
from ..core.constants import CREATE_NO_WINDOW


def list_wsl_distros():
    """Return the names of installed WSL distributions (best-effort).

    ``wsl --list --quiet`` prints one distribution name per line. On Windows
    the output is UTF-16LE (often with a BOM), so it is decoded as such and
    the trailing NUL/whitespace noise is stripped. Any failure -- WSL not
    installed, the call erroring or timing out -- yields an empty list, so the
    dashboard can treat "no distributions" and "could not ask" identically and
    simply fall back to the default distribution.
    """
    if shutil.which("wsl") is None:
        return []
    try:
        res = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    if res.returncode != 0:
        return []
    # Drop NULs and any byte-order mark before splitting; str.strip() leaves
    # a leading U+FEFF in place, which would corrupt the first distro name.
    text = (res.stdout or b"").decode("utf-16-le", errors="replace")
    text = text.replace("\x00", "").replace("﻿", "")
    distros = []
    for line in text.splitlines():
        name = line.strip()
        if name:
            distros.append(name)
    return distros


def _busid(location):
    """Reduce pyserial's USB location ('1-6:x.0') to the bare bus id ('1-6')."""
    if not location:
        return ""
    return location.split(":", 1)[0]


def _device_sort_key(device):
    """Sort COM3 before COM17 (numeric, not lexicographic, port order)."""
    return (len(device), device)


def collect_ports():
    """Return a list of dicts describing every serial port on the system."""
    rows = []
    for p in sorted(serial.tools.list_ports.comports(),
                    key=lambda p: _device_sort_key(p.device)):
        if p.vid is not None and p.pid is not None:
            vid_pid = "%04X:%04X" % (p.vid, p.pid)
        else:
            vid_pid = ""
        rows.append({
            "device": p.device,
            "description": p.description or "",
            "vid_pid": vid_pid,
            "busid": _busid(p.location),
            "serial_number": p.serial_number or "",
            "board": classify_vid(p.vid),
        })
    return rows


def format_port_table(rows):
    """Render the port list as aligned text lines (list of str)."""
    if not rows:
        return ["No serial ports found."]
    headers = ("Device", "VID:PID", "Bus ID", "Serial number", "Board", "Description")
    table = [headers]
    for r in rows:
        table.append((
            r["device"],
            r["vid_pid"] or "-",
            r["busid"] or "-",
            r["serial_number"] or "-",
            r["board"],
            r["description"],
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    lines = []
    for n, row in enumerate(table):
        lines.append("  ".join(cell.ljust(widths[i])
                               for i, cell in enumerate(row)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return lines


def print_port_list(as_json=False):
    """Print the discovered ports; the implementation behind ``--list``.

    With ``as_json`` the same rows are emitted as a JSON array for scripts
    and IDE integrations (``com2tty --list --json``).
    """
    rows = collect_ports()
    if as_json:
        import json
        print(json.dumps(rows, indent=2))
        return
    for line in format_port_table(rows):
        print(line)
