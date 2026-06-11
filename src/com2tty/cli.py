import argparse
import sys
import logging
from com2tty.host import run_bridge, run_gamepad_bridge

def main():
    parser = argparse.ArgumentParser(
        description="com2tty: Forward Windows COM ports (or XInput gamepads) to WSL."
    )

    parser.add_argument(
        "port",
        nargs="?",
        help="Windows COM port to connect to (e.g. COM3 or COM1). "
             "Not required in --gamepad mode."
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
        choices=["auto", "esp32", "pico", "none"],
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
    
    args = parser.add_argument_group("advanced")
    
    parsed_args = parser.parse_args()
    
    # Configure logging
    log_level = logging.DEBUG if parsed_args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr
    )
    
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
        parser.error("the 'port' argument is required unless --gamepad is used")

    try:
        run_bridge(
            port=parsed_args.port,
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
            board=parsed_args.board
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
