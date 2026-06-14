"""Command-line entry point: argument parsing and mode dispatch.

This package is deliberately thin -- it parses arguments (after expanding
@profile tokens from com2tty.ini) and hands off to the Windows-side
facades in ``com2tty.windows``. The CLI surface (flags, defaults, help
text) is a compatibility contract; defaults come from
``com2tty.core.constants`` so the documented values and the implementation
cannot drift apart.
"""
import argparse
import sys
import logging
from com2tty import __version__
from com2tty.core.boards import BOARD_CHOICES
from com2tty.core.constants import (
    DEFAULT_PAD_FIFO,
    DEFAULT_PAD_NAME,
    DEFAULT_POLL_HZ,
    DEFAULT_RFC2217_PORT,
    DEFAULT_WSL_TTY,
)
from com2tty.windows.bridge_app import run_bridge
from com2tty.windows.gamepad_app import run_gamepad_bridge

def main():
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
    
    # Expand @profile tokens (saved argument sets from com2tty.ini) before
    # parsing; arguments given after the token override the profile's values.
    from com2tty.cli.profiles import ProfileError, expand_profiles
    try:
        argv = expand_profiles(sys.argv[1:])
    except ProfileError as e:
        parser.error(str(e))

    parsed_args = parser.parse_args(argv)

    # Configure logging
    log_level = logging.DEBUG if parsed_args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr
    )

    # Dashboard is the zero-config default: with no COM port and no other
    # mode flag (or an explicit --dashboard), drop into the interactive TUI
    # instead of erroring on the missing positional argument. Explicit
    # command-line modes below still win when their flags/ports are given.
    wants_cli_mode = (parsed_args.doctor or parsed_args.list_ports
                      or parsed_args.gamepad or bool(parsed_args.port))
    if parsed_args.dashboard or not wants_cli_mode:
        from com2tty.windows.dashboard import run_dashboard
        sys.exit(run_dashboard(distro=parsed_args.distro,
                               rfc2217_port=parsed_args.rfc2217_port,
                               debug=parsed_args.debug))

    if parsed_args.doctor:
        from com2tty.windows.doctor import run_doctor
        sys.exit(run_doctor(distro=parsed_args.distro,
                            rfc2217_port=parsed_args.rfc2217_port))

    if parsed_args.list_ports and parsed_args.port:
        parser.error("--list does not take a COM port; remove the positional "
                     "argument (it would be silently ignored)")

    if parsed_args.list_ports:
        from com2tty.windows.discovery import print_port_list
        print_port_list(as_json=parsed_args.json)
        return

    if parsed_args.gamepad and parsed_args.port:
        parser.error("--gamepad does not take a COM port; remove the "
                     "positional argument (it would be silently ignored)")

    if parsed_args.gamepad:
        pad_indices = parsed_args.pad_index
        if len(set(pad_indices)) != len(pad_indices):
            parser.error("--pad-index values must be unique")
        try:
            pad_kwargs = dict(
                poll_hz=parsed_args.poll_hz,
                name=parsed_args.pad_name,
                use_uinput=parsed_args.uinput,
                tmp_path=parsed_args.wsl_pad,
                distro=parsed_args.distro,
            )
            if len(pad_indices) > 1:
                from com2tty.windows.gamepad_app import run_multi_gamepad_bridge
                run_multi_gamepad_bridge(
                    pad_indices,
                    auto_respawn=parsed_args.auto_respawn,
                    **pad_kwargs)
            elif parsed_args.auto_respawn:
                from com2tty.windows.bridge_app import run_with_respawn
                run_with_respawn(run_gamepad_bridge,
                                 pad_index=pad_indices[0], **pad_kwargs)
            else:
                run_gamepad_bridge(pad_index=pad_indices[0], **pad_kwargs)
        except KeyboardInterrupt:
            logging.info("Interrupted by user. Exiting.")
            sys.exit(0)
        except Exception as e:
            logging.error(f"Fatal error: {e}")
            if parsed_args.debug:
                import traceback
                traceback.print_exc()
            sys.exit(1)
        return

    # Defensive guard: the no-port case is already handled by the dashboard
    # dispatch above (no port and no mode flag enters the dashboard), so by the
    # time control reaches here a port has always been supplied. Kept against
    # future changes to that dispatch; it is therefore intentionally uncovered.
    if not parsed_args.port:  # pragma: no cover
        parser.error("the 'port' argument is required unless --gamepad, "
                     "--list, or --dashboard is used")

    # A respawned bridge re-opens the COM port from scratch; the device may
    # re-enumerate while WSL restarts, so waiting for it is implied.
    if parsed_args.auto_respawn:
        parsed_args.wait = True

    try:
        if len(parsed_args.port) > 1:
            from com2tty.windows.bridge_app import run_multi_bridge
            run_multi_bridge(
                ports=parsed_args.port,
                baud=parsed_args.baud,
                wsl_tty=parsed_args.wsl_tty,
                bytesize=parsed_args.bytesize,
                parity=parsed_args.parity,
                stopbits=parsed_args.stopbits,
                xonxoff=parsed_args.xonxoff,
                rtscts=parsed_args.rtscts,
                dsrdtr=parsed_args.dsrdtr,
                rfc2217_port=parsed_args.rfc2217_port,
                distro=parsed_args.distro,
                board=parsed_args.board,
                wait=parsed_args.wait,
                auto_respawn=parsed_args.auto_respawn
            )
        else:
            bridge_kwargs = dict(
                port=parsed_args.port[0],
                baud=parsed_args.baud,
                wsl_tty=parsed_args.wsl_tty,
                bytesize=parsed_args.bytesize,
                parity=parsed_args.parity,
                stopbits=parsed_args.stopbits,
                xonxoff=parsed_args.xonxoff,
                rtscts=parsed_args.rtscts,
                dsrdtr=parsed_args.dsrdtr,
                rfc2217_port=parsed_args.rfc2217_port,
                distro=parsed_args.distro,
                board=parsed_args.board,
                wait=parsed_args.wait
            )
            if parsed_args.auto_respawn:
                from com2tty.windows.bridge_app import run_with_respawn
                run_with_respawn(run_bridge, **bridge_kwargs)
            else:
                run_bridge(**bridge_kwargs)
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting.")
        sys.exit(0)
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        if parsed_args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__": # pragma: no cover
    main()
