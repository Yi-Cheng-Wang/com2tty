"""The RFC 2217 forwarder: upload tools' TCP doorway to the bridged port.

esptool, bossac and friends connect to ``rfc2217://127.0.0.1:<port>``; this
server relays their byte stream over the bridge's stdin/stdout pipes to the
Windows host, where the pyserial RFC 2217 machinery drives the real COM
port. While a client is connected the PTY main loop yields the stdio pipes
(coordinated through ``rfc2217_active``).
"""
import os
import select
import socket
import sys
import time

from .base import LoopbackTcpServer, _wait_until_clear


class Rfc2217Forwarder(LoopbackTcpServer):
    """Long-lived TCP forwarder run as a thread inside the main bridge process.

    Accepts esptool connections and relays data through stdin/stdout (shared
    with the PTY bridge, coordinated by the ``rfc2217_active`` event).
    """

    def __init__(self, port, rfc2217_active, uf2_active=None):
        super().__init__(port)
        self._rfc2217_active = rfc2217_active
        self._uf2_active = uf2_active

    def ready_line(self):
        return f"[CONTROL] RFC2217_READY:{self.port}\n"

    def bind_error_line(self, exc):
        return (f"[CONTROL] RFC2217_ERROR: bind failed: {exc}. Port "
                f"{self.port} may be in use; choose another with "
                f"--rfc2217-port.\n")

    def handle_connection(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Never start an RFC 2217 session while a UF2 transfer owns the
        # stdio pipes; both would read stdin and corrupt each other.
        _wait_until_clear(self._uf2_active)

        # Signal connection to Windows side and pause PTY bridge
        sys.stderr.write("[CONTROL] RFC2217_CONNECT\n")
        sys.stderr.flush()
        self._rfc2217_active.set()
        time.sleep(0.3)  # Wait for main loop to yield stdin/stdout

        conn.setblocking(False)

        try:
            while True:
                r, _, _ = select.select([0, conn], [], [], 0.5)
                if 0 in r:
                    data = os.read(0, 4096)
                    if not data:
                        break
                    conn.sendall(data)
                if conn in r:
                    try:
                        data = conn.recv(4096)
                        if not data:
                            break
                        os.write(1, data)
                    except BlockingIOError:
                        continue
                    except ConnectionResetError:
                        break
        except Exception as e:
            sys.stderr.write(f"[CONTROL] RFC2217_ERROR: session: {e}\n")
            sys.stderr.flush()
        finally:
            conn.close()

        # Signal disconnection and resume PTY bridge
        self._rfc2217_active.clear()
        sys.stderr.write("[CONTROL] RFC2217_DISCONNECT\n")
        sys.stderr.flush()


def run_rfc2217_server_thread(port, rfc2217_active, uf2_active=None):
    """Thread entry point: serve RFC 2217 clients until the process exits."""
    Rfc2217Forwarder(port, rfc2217_active, uf2_active).serve_forever()
