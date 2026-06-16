"""Mode dispatch for the com2tty CLI.

``main`` (in ``com2tty.cli``) handles parsing, profile expansion, and logging;
it then hands the parsed namespace here. ``dispatch`` selects exactly one mode
-- dashboard, doctor, list, gamepad, or serial -- and runs it.

The two primary entry points (``run_bridge`` and ``run_gamepad_bridge``) are
injected by ``main`` rather than imported here, so the package-level patch
points (``com2tty.cli.run_bridge``/``run_gamepad_bridge``) keep working and the
heavier bridge modules are not imported until a bridge actually runs. The
remaining run-functions are lazy-imported at their source so startup stays
cheap and mode selection has no import side effects.
"""
import logging
import sys


def dispatch(parser, args, run_bridge, run_gamepad_bridge):
    """Select and run the single mode implied by ``args``.

    May raise ``SystemExit`` (argparse ``parser.error`` and the dashboard/
    doctor exit-code propagation). ``KeyboardInterrupt`` and other exceptions
    propagate to ``main``'s unified wrapper.
    """
    # Dashboard is the zero-config default: with no COM port and no other mode
    # flag (or an explicit --dashboard), drop into the interactive TUI instead
    # of erroring on the missing positional argument. Explicit command-line
    # modes below still win when their flags/ports are given.
    wants_cli_mode = (args.doctor or args.list_ports
                      or args.gamepad or bool(args.port))
    if args.dashboard or not wants_cli_mode:
        from com2tty.windows.dashboard import run_dashboard
        sys.exit(run_dashboard(distro=args.distro,
                               rfc2217_port=args.rfc2217_port,
                               debug=args.debug))

    if args.doctor:
        from com2tty.windows.doctor import run_doctor
        sys.exit(run_doctor(distro=args.distro,
                            rfc2217_port=args.rfc2217_port))

    if args.list_ports and args.port:
        parser.error("--list does not take a COM port; remove the positional "
                     "argument (it would be silently ignored)")

    if args.list_ports:
        from com2tty.windows.discovery import print_port_list
        print_port_list(as_json=args.json)
        return

    if args.gamepad and args.port:
        parser.error("--gamepad does not take a COM port; remove the "
                     "positional argument (it would be silently ignored)")

    if args.gamepad:
        handle_gamepad_mode(parser, args, run_gamepad_bridge)
        return

    # Defensive guard: the no-port case is already handled by the dashboard
    # dispatch above (no port and no mode flag enters the dashboard), so by the
    # time control reaches here a port has always been supplied. Kept against
    # future changes to that dispatch; it is therefore intentionally uncovered.
    if not args.port:  # pragma: no cover
        parser.error("the 'port' argument is required unless --gamepad, "
                     "--list, or --dashboard is used")

    handle_serial_mode(args, run_bridge)


def handle_gamepad_mode(parser, args, run_gamepad_bridge):
    """Forward one or more XInput controllers into WSL."""
    pad_indices = args.pad_index
    if len(set(pad_indices)) != len(pad_indices):
        parser.error("--pad-index values must be unique")

    pad_kwargs = dict(
        poll_hz=args.poll_hz,
        name=args.pad_name,
        use_uinput=args.uinput,
        tmp_path=args.wsl_pad,
        distro=args.distro,
    )
    if len(pad_indices) > 1:
        from com2tty.windows.gamepad_app import run_multi_gamepad_bridge
        run_multi_gamepad_bridge(
            pad_indices, auto_respawn=args.auto_respawn, **pad_kwargs)
    elif args.auto_respawn:
        from com2tty.windows.bridge_app import run_with_respawn
        run_with_respawn(run_gamepad_bridge,
                         pad_index=pad_indices[0], **pad_kwargs)
    else:
        run_gamepad_bridge(pad_index=pad_indices[0], **pad_kwargs)


def handle_serial_mode(args, run_bridge):
    """Bridge one or several Windows COM ports to WSL."""
    # A respawned bridge re-opens the COM port from scratch; the device may
    # re-enumerate while WSL restarts, so waiting for it is implied.
    if args.auto_respawn:
        args.wait = True

    if len(args.port) > 1:
        from com2tty.windows.bridge_app import run_multi_bridge
        run_multi_bridge(
            ports=args.port,
            baud=args.baud,
            wsl_tty=args.wsl_tty,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
            xonxoff=args.xonxoff,
            rtscts=args.rtscts,
            dsrdtr=args.dsrdtr,
            rfc2217_port=args.rfc2217_port,
            distro=args.distro,
            board=args.board,
            wait=args.wait,
            auto_respawn=args.auto_respawn,
        )
        return

    bridge_kwargs = dict(
        port=args.port[0],
        baud=args.baud,
        wsl_tty=args.wsl_tty,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
        xonxoff=args.xonxoff,
        rtscts=args.rtscts,
        dsrdtr=args.dsrdtr,
        rfc2217_port=args.rfc2217_port,
        distro=args.distro,
        board=args.board,
        wait=args.wait,
    )
    if args.auto_respawn:
        from com2tty.windows.bridge_app import run_with_respawn
        run_with_respawn(run_bridge, **bridge_kwargs)
    else:
        run_bridge(**bridge_kwargs)


def configure_logging(args):
    """Set up the root logger to the requested verbosity."""
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
