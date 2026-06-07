import argparse
import sys
import logging
from com2tty.host import run_bridge

def main():
    parser = argparse.ArgumentParser(
        description="com2tty: Forward Windows COM ports to WSL virtual ttyUSB devices."
    )
    
    parser.add_argument(
        "port",
        help="Windows COM port to connect to (e.g. COM3 or COM1)."
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
            dsrdtr=parsed_args.dsrdtr
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
