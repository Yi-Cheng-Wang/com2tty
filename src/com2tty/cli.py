import argparse
import sys
import logging
from com2tty import __version__
from com2tty.boards import BOARD_CHOICES
from com2tty.host import run_bridge, run_gamepad_bridge

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
        "--wait",
        action="store_true",
        help="Serial mode: if the COM port is not present yet, wait for it "
             "to appear instead of failing."
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
        choices=[0, 1, 2, 3],
        default=0,
        help="XInput controller slot to forward in --gamepad mode (default: 0)."
    )

    parser.add_argument(
        "--pad-name",
        default="Microsoft X-Box 360 pad",
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
        default="/tmp/com2pad0",
        help="Target FIFO path inside WSL for the default /tmp gamepad stream "
             "(default: /tmp/com2pad0)."
    )

    parser.add_argument(
        "--poll-hz",
        type=int,
        default=250,
        help="XInput polling rate in Hz for --gamepad mode (default: 250)."
    )
    
    parser.add_argument(
        "-b", "--baud",
        type=str,
        default="auto",
        help="Baud rate for the serial port or 'auto' to match Windows (default: auto)."
    )
    
    parser.add_argument(
        "-w", "--wsl-tty",
        default="/tmp/ttyUSB0",
        help="Target symlink path inside WSL (default: /tmp/ttyUSB0)."
    )
    
    parser.add_argument(
        "--rfc2217-port",
        type=int,
        default=4000,
        help="TCP port for RFC 2217 server (default: 4000)."
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
    from com2tty.profiles import ProfileError, expand_profiles
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

    if parsed_args.doctor:
        from com2tty.doctor import run_doctor
        sys.exit(run_doctor(distro=parsed_args.distro,
                            rfc2217_port=parsed_args.rfc2217_port))

    if parsed_args.list_ports:
        from com2tty.discovery import print_port_list
        print_port_list(as_json=parsed_args.json)
        return

    if parsed_args.gamepad and parsed_args.port:
        parser.error("--gamepad does not take a COM port; remove the "
                     "positional argument (it would be silently ignored)")

    if parsed_args.gamepad:
        try:
            run_gamepad_bridge(
                pad_index=parsed_args.pad_index,
                poll_hz=parsed_args.poll_hz,
                name=parsed_args.pad_name,
                use_uinput=parsed_args.uinput,
                tmp_path=parsed_args.wsl_pad,
                distro=parsed_args.distro,
            )
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

    if not parsed_args.port:
        parser.error("the 'port' argument is required unless --gamepad or --list is used")

    try:
        if len(parsed_args.port) > 1:
            from com2tty.host import run_multi_bridge
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
                wait=parsed_args.wait
            )
        else:
            run_bridge(
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
