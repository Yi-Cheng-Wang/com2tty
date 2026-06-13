"""The UF2 relay: receives firmware images from the picotool wrapper.

The intercepted picotool sends the raw .uf2 bytes to this loopback port;
the relay announces the upload on stderr (size and MD5), waits for the
host's acknowledgement on stdin, and streams the image to the host over
stdout -- which writes it onto the BOOTSEL mass-storage drive that only
Windows can see.
"""
import os
import select
import sys
import time

from .base import LoopbackTcpServer, _wait_until_clear


def md5_hexdigest(data):
    import hashlib
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    except TypeError:  # Python < 3.9 has no usedforsecurity flag
        return hashlib.md5(data).hexdigest()


class Uf2Relay(LoopbackTcpServer):
    """TCP server inside WSL that receives UF2 data from the picotool wrapper
    and relays it to the Windows host through the stdout pipe with control
    messages."""

    #: How long to wait for the host's UF2_ACK on stdin.
    ACK_TIMEOUT = 5.0

    def __init__(self, port, uf2_active, rfc2217_active=None):
        super().__init__(port)
        self._uf2_active = uf2_active
        self._rfc2217_active = rfc2217_active

    def ready_line(self):
        return f"[CONTROL] UF2_READY:{self.port}\n"

    def bind_error_line(self, exc):
        return (f"[CONTROL] UF2_ERROR: bind failed on port {self.port}: "
                f"{exc}. The UF2 relay uses --rfc2217-port + 1; choose "
                f"another --rfc2217-port.\n")

    @staticmethod
    def _drain_upload(conn):
        """Read the complete UF2 image the picotool wrapper sends."""
        uf2_data = bytearray()
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                uf2_data.extend(chunk)
        except Exception:
            pass
        finally:
            conn.close()
        return uf2_data

    def _wait_for_ack(self):
        """Scan stdin for the host's UF2_ACK; True when it arrived in time."""
        timeout_time = time.time() + self.ACK_TIMEOUT
        buffer = b""
        while time.time() < timeout_time:
            r, _, _ = select.select([0], [], [], 0.1)
            if 0 in r:
                try:
                    chunk = os.read(0, 1024)
                    if not chunk:
                        break
                    buffer += chunk
                    if b"[CONTROL] UF2_ACK" in buffer:
                        return True
                except Exception:
                    break
        return False

    def handle_connection(self, conn):
        # Read all UF2 data from the picotool wrapper
        uf2_data = self._drain_upload(conn)

        md5_hash = md5_hexdigest(uf2_data)

        # Never start the upload while an RFC 2217 session owns stdin;
        # the ACK wait below would steal bytes from that session.
        _wait_until_clear(self._rfc2217_active)

        sys.stderr.write(f"[CONTROL] UF2_UPLOAD_START:{len(uf2_data)}:{md5_hash}\n")
        sys.stderr.flush()

        # Pause the PTY main loop so we own stdout exclusively
        self._uf2_active.set()
        time.sleep(0.3)

        if self._wait_for_ack():
            # Send UF2 binary data through stdout pipe to Windows host
            try:
                sys.stdout.buffer.write(uf2_data)
                sys.stdout.buffer.flush()
            except Exception as e:
                sys.stderr.write(f"[CONTROL] UF2_ERROR: Failed to write to stdout: {e}\n")
                sys.stderr.flush()
        else:
            sys.stderr.write("[CONTROL] UF2_ERROR: Timeout waiting for host UF2_ACK\n")
            sys.stderr.flush()

        sys.stderr.write("[CONTROL] UF2_UPLOAD_END\n")
        sys.stderr.flush()

        self._uf2_active.clear()


def run_uf2_relay_thread(port, uf2_active, rfc2217_active=None):
    """Thread entry point: serve picotool-wrapper uploads until process exit."""
    Uf2Relay(port, uf2_active, rfc2217_active).serve_forever()
