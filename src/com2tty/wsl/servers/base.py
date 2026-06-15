"""Shared lifecycle for the bridge's loopback TCP servers.

``LoopbackTcpServer`` owns the socket lifecycle every server needs: liveness-
aware reclamation of a leftover listener, bind with an actionable control
message on failure, a READY announcement, and an accept loop with a timeout
so the (daemon) server thread never blocks unkillably. Subclasses implement
``handle_connection`` only.
"""
import os
import signal
import socket
import subprocess
import sys
import time

from ..liveness import is_port_session_alive


def kill_leftover_listener(port):
    """Reclaim a TCP port held by a *com2tty* listener from a previous session.

    Only processes whose command line references this bridge are killed, so an
    unrelated service that happens to use the same port is never terminated.
    (The previous implementation ran ``fuser -k`` which killed any owner.)

    A port whose heartbeat file is fresh belongs to a *live* com2tty session
    (most likely a second invocation that forgot --rfc2217-port); killing that
    would tear down the user's other bridge, so it is left alone and the bind
    below fails with an actionable message instead.
    """
    if is_port_session_alive(port):
        sys.stderr.write(
            f"Note: port {port} is held by another live com2tty session; "
            f"not killing it. Choose a different --rfc2217-port.\n")
        sys.stderr.flush()
        return
    try:
        res = subprocess.run(["fuser", f"{port}/tcp"], capture_output=True, timeout=3)
    except FileNotFoundError:
        # Minimal distros ship without psmisc; the bind below will then fail
        # loudly if a leftover listener is still holding the port.
        sys.stderr.write(
            f"Note: 'fuser' not found (install package 'psmisc'); cannot "
            f"auto-clean leftover listeners on port {port}.\n")
        sys.stderr.flush()
        return
    except Exception:
        return

    pids = res.stdout.decode("utf-8", "replace").split()
    killed = False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        # Reclaim the port only from another com2tty bridge instance: require
        # both markers (the helper runs as ".../com2tty/bridge.py") so an
        # unrelated process that merely mentions one of them is never killed.
        if "com2tty" in cmdline and "bridge.py" in cmdline:
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed = True
            except Exception:
                pass
        else:
            sys.stderr.write(
                f"Note: port {port} is held by an unrelated process (PID {pid}); "
                f"not killing it. Choose a different --rfc2217-port if bind fails.\n")
            sys.stderr.flush()
    if killed:
        time.sleep(0.3)


def _wait_until_clear(event, timeout=30.0):
    """Block until ``event`` clears (or the timeout lapses).

    Used to serialise stdin/stdout ownership between the RFC 2217 forwarder
    and the UF2 relay: both read fd 0, and starting one session while the
    other is active would interleave their reads.
    """
    if event is None:
        return
    deadline = time.time() + timeout
    while event.is_set() and time.time() < deadline:
        time.sleep(0.05)


class LoopbackTcpServer:
    """Single-client 127.0.0.1 listener with the bridge's standard lifecycle.

    Subclasses define the READY/bind-error control lines and implement
    ``handle_connection(conn)``; one connection is serviced at a time, which
    is exactly the concurrency the stdio-pipe transport can offer anyway.
    """

    #: accept() timeout; keeps the loop responsive without busy-waiting.
    ACCEPT_TIMEOUT = 1.0

    def __init__(self, port):
        self.port = port

    # -- control-line hooks (subclass responsibility) ----------------------

    def ready_line(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def bind_error_line(self, exc):  # pragma: no cover - overridden
        raise NotImplementedError

    def handle_connection(self, conn):  # pragma: no cover - overridden
        raise NotImplementedError

    # -- lifecycle ----------------------------------------------------------

    def serve_forever(self):
        kill_leftover_listener(self.port)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', self.port))
        except Exception as e:
            sys.stderr.write(self.bind_error_line(e))
            sys.stderr.flush()
            return
        s.listen(1)
        s.settimeout(self.ACCEPT_TIMEOUT)

        sys.stderr.write(self.ready_line())
        sys.stderr.flush()

        try:
            while True:
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                self.handle_connection(conn)
        finally:
            s.close()
