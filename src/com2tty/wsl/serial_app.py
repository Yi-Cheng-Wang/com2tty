"""Entry point of the WSL serial-bridge helper (spawned via ``bridge.py``).

Wires the pieces together for one session: the pty endpoint user programs
talk to, the RFC 2217 forwarder and UF2 relay server threads, the
PlatformIO shell-environment injection, and the central select loop that
pumps bytes between the pty master and the stdio pipes to the Windows host
-- yielding those pipes whenever an upload session owns them.
"""
import argparse
import os
import select
import sys
import threading
import time
import traceback

from .integrations.picotool import (
    cleanup_picotool_interceptor,
    restore_orphaned_picotools,
    setup_picotool_interceptor,
)
from .integrations.shell_env import clean_rc, inject_rc
from .liveness import (
    ALIVE_TOUCH_INTERVAL,
    remove_alive_files,
    touch_alive_files,
)
from .pty_manager import (
    cleanup_symlink,
    create_symlink_with_fallback,
    get_pty_settings,
    open_pty,
)
from .servers.rfc2217_forwarder import run_rfc2217_server_thread
from .servers.uf2_relay import run_uf2_relay_thread


def build_arg_parser():
    parser = argparse.ArgumentParser(description="com2tty WSL Bridge Helper")
    parser.add_argument(
        "-s", "--symlink",
        required=True,
        help="Target symlink path for the pseudo-terminal device."
    )
    parser.add_argument(
        "-r", "--rfc2217-port",
        type=int,
        help="TCP port for RFC 2217 server to inject into bashrc and listen on"
    )
    parser.add_argument(
        "--no-env-setup",
        action="store_true",
        help="Skip PlatformIO env-var injection and picotool interception. "
             "Used for secondary bridges in multi-port mode so they do not "
             "overwrite the primary bridge's shell configuration."
    )
    return parser


def main():
    args = build_arg_parser().parse_args()
    env_setup = args.rfc2217_port and not args.no_env_setup

    target_path = args.symlink
    created_symlink = None
    alive_ports = []

    # We must keep both master and slave descriptors open.
    # Keeping slave_fd open prevents EIO errors on the master side when
    # WSL clients open and close the virtual serial port.
    master_fd = None
    slave_fd = None

    if env_setup:
        # Self-heal anything a previous, crashed session left behind before we
        # set up our own interceptors. inject_rc already clears stale rc blocks.
        restore_orphaned_picotools()
        inject_rc(args.rfc2217_port, args.symlink)

    # Events to coordinate stdin/stdout access between PTY bridge, RFC 2217, and UF2 relay
    rfc2217_active = threading.Event()
    uf2_active = threading.Event()

    try:
        master_fd, slave_fd, slave_name = open_pty()

        created_symlink = create_symlink_with_fallback(slave_name, target_path)

        # Start RFC 2217 server thread if port is specified
        if args.rfc2217_port:
            uf2_port = args.rfc2217_port + 1
            alive_ports = [args.rfc2217_port, uf2_port]
            touch_alive_files(alive_ports)
            if env_setup:
                setup_picotool_interceptor(uf2_port)
            t_rfc2217 = threading.Thread(
                target=run_rfc2217_server_thread,
                args=(args.rfc2217_port, rfc2217_active, uf2_active),
                daemon=True
            )
            t_rfc2217.start()
            t_uf2_relay = threading.Thread(
                target=run_uf2_relay_thread,
                args=(uf2_port, uf2_active, rfc2217_active),
                daemon=True
            )
            t_uf2_relay.start()

        # Select loop
        # 0 is stdin, master_fd is the pseudo-terminal master
        sys.stderr.write("WSL bridge enter main loop.\n")
        sys.stderr.flush()

        _run_select_loop(master_fd, rfc2217_active, uf2_active, alive_ports)

    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("WSL bridge interrupted by signal.\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover
        sys.stderr.write(f"WSL bridge error: {traceback.format_exc()}\n")
        sys.stderr.flush()
    finally:
        # Clean up symlink and file descriptors
        if env_setup:
            clean_rc(own_pid=os.getpid())
            cleanup_picotool_interceptor()
        remove_alive_files(alive_ports)
        if created_symlink:
            cleanup_symlink(created_symlink)
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
        sys.stderr.write("WSL bridge shut down.\n")
        sys.stderr.flush()


def _run_select_loop(master_fd, rfc2217_active, uf2_active, alive_ports):
    """Pump stdin <-> pty master until EOF, yielding to upload sessions.

    Also reports pty termios changes to the host as ``[CONTROL] SETTINGS``
    lines and refreshes the session's per-port heartbeat files.
    """
    last_settings = None
    last_alive_touch = time.time()

    while True:
        # Heartbeat: keep the per-port liveness markers fresh so another
        # session can tell this one apart from a crashed leftover.
        if alive_ports and time.time() - last_alive_touch >= ALIVE_TOUCH_INTERVAL:
            touch_alive_files(alive_ports)
            last_alive_touch = time.time()

        # Yield stdin/stdout to RFC 2217 forwarder or UF2 relay when active
        if rfc2217_active.is_set() or uf2_active.is_set():
            time.sleep(0.1)
            continue

        current_settings = get_pty_settings(master_fd)
        if current_settings != last_settings and current_settings[0] is not None:
            baud, bytesize, parity, stopbits = current_settings
            sys.stderr.write(f"[CONTROL] SETTINGS: baud={baud} bytesize={bytesize} parity={parity} stopbits={stopbits}\n")
            sys.stderr.flush()
            last_settings = current_settings

        # select blocks until data is available on stdin or master_fd
        r, w, x = select.select([0, master_fd], [], [], 0.5)

        # Re-check after select returns (RFC 2217 or UF2 might have activated during select)
        if rfc2217_active.is_set() or uf2_active.is_set():
            continue

        if 0 in r:
            data = os.read(0, 4096)
            if not data:
                sys.stderr.write("EOF on stdin. Exiting.\n")
                sys.stderr.flush()
                break
            os.write(master_fd, data)

        if master_fd in r:
            # Read from the virtual serial port
            try:
                data = os.read(master_fd, 4096)
                if not data:
                    # Should not happen typically while slave_fd is kept open,
                    # but handle it gracefully if it does.
                    sys.stderr.write("EOF on PTY master. Exiting.\n")
                    sys.stderr.flush()
                    break
                os.write(1, data)
            except OSError as e:
                # In case of EIO (Input/output error) when slave closes,
                # just ignore it and continue since we keep slave_fd open.
                if e.errno == 5:  # EIO
                    continue
                else:
                    raise e


if __name__ == "__main__":  # pragma: no cover
    main()
