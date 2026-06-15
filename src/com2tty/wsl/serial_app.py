"""Entry point of the WSL serial-bridge helper (spawned via ``bridge.py``).

Wires the pieces together for one session: the pty endpoint user programs
talk to, the RFC 2217 forwarder and UF2 relay server threads, the
PlatformIO shell-environment injection, and the central select loop that
pumps bytes between the pty master and the stdio pipes to the Windows host
-- yielding those pipes whenever an upload session owns them.
"""
import argparse
import os
import secrets
import select
import signal
import socket
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


def _install_signal_handlers():
    """Route terminating signals through the normal (finally) shutdown path.

    The Windows host tears the helper down with ``proc.terminate()``, and the
    kill-on-close Job Object reaps it when the console window is closed; inside
    WSL both surface as SIGTERM/SIGHUP. Their default action terminates the
    process *without* unwinding, so the finally cleanup (the ~/.bashrc block,
    picotool interception, tty symlink, heartbeats) would be skipped and leak.
    Raising KeyboardInterrupt instead routes them through the same teardown as
    Ctrl+C, so every injection is cleaned up exactly as on a normal exit.
    """
    def _graceful(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue  # e.g. SIGHUP is absent on Windows
        try:
            signal.signal(sig, _graceful)
        except (OSError, ValueError):
            pass  # signals can only be installed from the main thread


def _tcp_port_in_use(port):
    """True when 127.0.0.1:port already has an active listener inside WSL.

    Mirrors the servers' bind (SO_REUSEADDR) so the probe agrees with what
    their real bind would do: it tolerates a TIME_WAIT leftover but reports a
    live listener as in-use.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


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
    # Catch the host's terminate / Job-Object kill so cleanup always runs.
    _install_signal_handlers()
    env_setup = args.rfc2217_port and not args.no_env_setup

    target_path = args.symlink
    created_symlink = None
    alive_ports = []

    # We must keep both master and slave descriptors open.
    # Keeping slave_fd open prevents EIO errors on the master side when
    # WSL clients open and close the virtual serial port.
    master_fd = None
    slave_fd = None
    exit_code = 0

    # Fail fast on a same-port conflict BEFORE any shared-state side effects
    # (rc injection, picotool interception, the tty symlink). Starting anyway
    # would hijack the other live session's shell config and serial endpoint
    # and silently cross-wire uploads to its board.
    if args.rfc2217_port:
        uf2_port = args.rfc2217_port + 1
        busy = next((p for p in (args.rfc2217_port, uf2_port)
                     if _tcp_port_in_use(p)), None)
        if busy is not None:
            sys.stderr.write(
                f"Refusing to start: TCP port {busy} is already in use inside "
                f"WSL (another com2tty session on --rfc2217-port "
                f"{args.rfc2217_port}?). Starting would hijack that session's "
                f"shell config and tty link. Choose a different "
                f"--rfc2217-port.\n")
            sys.stderr.flush()
            return 1

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
            # Per-session secret shared only between the picotool wrapper (which
            # embeds it in its owner-readable /tmp script) and the UF2 relay, so
            # another local user cannot push firmware to the relay during an
            # upload. Generated unconditionally: a secondary bridge installs no
            # wrapper, so its relay holds a token no client can present and thus
            # rejects every upload.
            uf2_token = secrets.token_hex(16)
            # NOTE: do not write the heartbeat here. The per-port reclaim
            # (kill_leftover_listener) runs moments later and must not see this
            # session's own freshly-written marker and mistake it for another
            # live session holding the port. The select loop below registers
            # the heartbeat once the ports are actually reclaimed/bound.
            if env_setup:
                setup_picotool_interceptor(uf2_port, uf2_token)
            t_rfc2217 = threading.Thread(
                target=run_rfc2217_server_thread,
                args=(args.rfc2217_port, rfc2217_active, uf2_active),
                daemon=True
            )
            t_rfc2217.start()
            t_uf2_relay = threading.Thread(
                target=run_uf2_relay_thread,
                args=(uf2_port, uf2_active, rfc2217_active, uf2_token),
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
        # Propagate a non-zero status so the host can tell a crashed helper
        # apart from a clean shutdown; the finally block still runs cleanup.
        exit_code = 1
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
    return exit_code


def _run_select_loop(master_fd, rfc2217_active, uf2_active, alive_ports):
    """Pump stdin <-> pty master until EOF, yielding to upload sessions.

    Also reports pty termios changes to the host as ``[CONTROL] SETTINGS``
    lines and refreshes the session's per-port heartbeat files.
    """
    last_settings = None
    # 0.0 so the first loop pass registers the heartbeat immediately (the
    # premature startup touch was removed to avoid self-detection); from then
    # on it refreshes every ALIVE_TOUCH_INTERVAL.
    last_alive_touch = 0.0

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
