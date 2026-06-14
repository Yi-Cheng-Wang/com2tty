"""Argument-parser declaration for the com2tty CLI.

Kept separate from dispatch and from ``main`` so the flag surface -- which is
a compatibility contract -- can be read and tested in isolation. Defaults come
from ``com2tty.core.constants`` so the documented values and the implementation
cannot drift apart.
"""
import argparse

from com2tty import __version__
from com2tty.core.boards import BOARD_CHOICES
from com2tty.core.constants import (
    DEFAULT_PAD_FIFO,
    DEFAULT_PAD_NAME,
    DEFAULT_POLL_HZ,
    DEFAULT_RFC2217_PORT,
    DEFAULT_WSL_TTY,
)


def build_parser():
    """Create and return the CLI argument parser (pure declaration)."""
    parser = argparse.ArgumentParser(
        description="com2tty: Forward Windows COM ports (or XInput gamepads) to WSL."
    )

    parser.add_argument(
        "port",
        nargs="*",
        help="Windows COM port(s) to bridge (e.g. COM3, or COM3 COM5 to "
             "bridge several at once). Not required in --gamepad or "
             "--list mode."
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"com2tty {__version__}"
    )

    parser.add_argument(
        "-l", "--list",
        dest="list_ports",
        action="store_true",
        help="List the serial ports Windows can see (device, VID:PID, USB bus "
             "id, serial number, detected board) and exit."
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="With --list: print the port list as JSON instead of a table."
    )

    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run an environment self-check (WSL, python3, ports, leftovers) "
             "and exit."
    )

    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch the interactive management dashboard (TUI). This is also "
             "the default when com2tty is run with no COM port and no other "
             "mode flag."
    )

    parser.add_argument(
        "--wait",
        action="store_true",
        help="Serial mode: if the COM port is not present yet, wait for it "
             "to appear instead of failing."
    )

    parser.add_argument(
        "--auto-respawn",
        action="store_true",
        help="Rebuild the bridge automatically when the WSL helper dies "
             "(e.g. after 'wsl --shutdown' or a WSL update). In serial mode "
             "this implies --wait."
    )

    parser.add_argument(
        "--gamepad",
        action="store_true",
        help="Gamepad mode: forward a Windows XInput controller into WSL as a "
             "virtual uinput gamepad (no COM port / usbipd needed)."
    )

    parser.add_argument(
        "--pad-index",
        type=int,
        nargs="+",
        choices=[0, 1, 2, 3],
        default=[0],
        help="XInput controller slot(s) to forward in --gamepad mode "
             "(default: 0). Several slots may be given (e.g. --pad-index 0 1) "
             "to forward multiple controllers at once; each gets its own WSL "
             "endpoint."
    )

    parser.add_argument(
        "--pad-name",
        default=DEFAULT_PAD_NAME,
        help="Virtual device name advertised to WSL in --gamepad mode."
    )

    parser.add_argument(
        "--uinput",
        action="store_true",
        help="Gamepad mode: create a real system-wide /dev/input device via "
             "/dev/uinput (needs one-time root setup). Default is a root-free "
             "evdev stream at a /tmp FIFO."
    )

    parser.add_argument(
        "--wsl-pad",
        default=DEFAULT_PAD_FIFO,
        help="Target FIFO path inside WSL for the default /tmp gamepad stream "
             f"(default: {DEFAULT_PAD_FIFO})."
    )

    parser.add_argument(
        "--poll-hz",
        type=int,
        default=DEFAULT_POLL_HZ,
        help="XInput polling rate in Hz for --gamepad mode "
             f"(default: {DEFAULT_POLL_HZ})."
    )

    parser.add_argument(
        "-b", "--baud",
        type=str,
        default="auto",
        help="Baud rate for the serial port or 'auto' to match Windows (default: auto)."
    )

    parser.add_argument(
        "-w", "--wsl-tty",
        default=DEFAULT_WSL_TTY,
        help=f"Target symlink path inside WSL (default: {DEFAULT_WSL_TTY})."
    )

    parser.add_argument(
        "--rfc2217-port",
        type=int,
        default=DEFAULT_RFC2217_PORT,
        help="TCP port for RFC 2217 server "
             f"(default: {DEFAULT_RFC2217_PORT})."
    )

    parser.add_argument(
        "--distro",
        default=None,
        help="WSL distribution to use (default: the WSL default distro). "
             "Useful when the default distro lacks python3 (e.g. docker-desktop)."
    )

    parser.add_argument(
        "--board",
        choices=BOARD_CHOICES,
        default="auto",
        help="Override USB VID board detection for reset/upload handling "
             "(default: auto). Use 'none' to disable board-specific resets."
    )

    parser.add_argument(
        "--bytesize",
        type=int,
        choices=[5, 6, 7, 8],
        default=8,
        help="Serial byte size (default: 8)."
    )

    parser.add_argument(
        "--parity",
        choices=["N", "E", "O", "S", "M"],
        default="N",
        help="Serial parity: None, Even, Odd, Space, Mark (default: N)."
    )

    parser.add_argument(
        "--stopbits",
        type=float,
        choices=[1, 1.5, 2],
        default=1,
        help="Serial stop bits: 1, 1.5, or 2 (default: 1)."
    )

    parser.add_argument(
        "--xonxoff",
        action="store_true",
        help="Enable software flow control (XON/XOFF)."
    )

    parser.add_argument(
        "--rtscts",
        action="store_true",
        help="Enable hardware flow control (RTS/CTS)."
    )

    parser.add_argument(
        "--dsrdtr",
        action="store_true",
        help="Enable hardware flow control (DSR/DTR)."
    )

    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="Enable debug logging output."
    )

    return parser
