"""Command-line entry point: argument parsing and mode dispatch.

This package is deliberately thin -- it parses arguments (after expanding
@profile tokens from com2tty.ini) and hands off to the Windows-side facades
in ``com2tty.windows``. The flag surface lives in ``com2tty.cli.parser`` and
mode selection in ``com2tty.cli.dispatch``; this module only wires them
together and owns the single top-level error boundary.

``run_bridge`` and ``run_gamepad_bridge`` are imported here (and injected into
the dispatcher) so they remain patchable at ``com2tty.cli.<name>`` and so the
heavier bridge plumbing is only touched when a bridge actually runs.
"""
import logging
import sys
import traceback

from com2tty.windows.bridge_app import run_bridge
from com2tty.windows.gamepad_app import run_gamepad_bridge


def main():
    from com2tty.cli.dispatch import configure_logging, dispatch
    from com2tty.cli.parser import build_parser

    parser = build_parser()

    # Expand @profile tokens (saved argument sets from com2tty.ini) before
    # parsing; arguments given after the token override the profile's values.
    from com2tty.cli.profiles import ProfileError, expand_profiles
    try:
        argv = expand_profiles(sys.argv[1:])
    except ProfileError as e:
        parser.error(str(e))

    parsed_args = parser.parse_args(argv)
    configure_logging(parsed_args)

    # One error boundary for every mode: argparse/dashboard/doctor exits
    # (SystemExit) propagate, Ctrl-C is a clean exit, and any other failure is
    # logged once -- with a traceback under --debug -- and exits non-zero.
    try:
        dispatch(parser, parsed_args, run_bridge, run_gamepad_bridge)
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting.")
        sys.exit(0)
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        if parsed_args.debug:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
