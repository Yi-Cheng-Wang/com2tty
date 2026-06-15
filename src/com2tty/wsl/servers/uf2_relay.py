"""The UF2 relay: receives firmware images from the picotool wrapper.

The intercepted picotool sends this session's token followed by the raw .uf2
bytes to this loopback port; the relay verifies the token (so another local
user cannot inject firmware during an upload), announces the upload on stderr
(size and MD5), waits for the host's acknowledgement on stdin, and streams the
image to the host over stdout -- which writes it onto the BOOTSEL mass-storage
drive that only Windows can see.
"""
import hmac
import os
import select
import sys
import time

from ...core.util import md5_hexdigest
from .base import LoopbackTcpServer, _wait_until_clear


class Uf2Relay(LoopbackTcpServer):
    """TCP server inside WSL that receives UF2 data from the picotool wrapper
    and relays it to the Windows host through the stdout pipe with control
    messages."""

    #: How long to wait for the host's UF2_ACK on stdin.
    ACK_TIMEOUT = 5.0

    def __init__(self, port, uf2_active, rfc2217_active=None, token=""):
        super().__init__(port)
        self._uf2_active = uf2_active
        self._rfc2217_active = rfc2217_active
        # Per-session token the picotool wrapper must present before its image
        # is accepted. Empty means "no token configured" (the wrapper was not
        # installed, e.g. a secondary bridge), in which case every upload is
        # rejected because no legitimate client can authenticate.
        self._token = token.encode() if isinstance(token, str) else token

    def ready_line(self):
        return f"[CONTROL] UF2_READY:{self.port}\n"

    def bind_error_line(self, exc):
        return (f"[CONTROL] UF2_ERROR: bind failed on port {self.port}: "
                f"{exc}. The UF2 relay uses --rfc2217-port + 1; choose "
                f"another --rfc2217-port.\n")

    def _drain_upload(self, conn):
        """Authenticate the wrapper, then read the complete UF2 image.

        The wrapper prefixes this session's token before the image bytes. An
        upload with no token configured, or a mismatched token (e.g. an
        unrelated local process probing the relay port), is rejected and
        ``None`` is returned so the caller relays nothing.
        """
        token = self._token
        if not token:
            conn.close()
            return None
        uf2_data = bytearray()
        try:
            prefix = bytearray()
            while len(prefix) < len(token):
                chunk = conn.recv(len(token) - len(prefix))
                if not chunk:
                    break
                prefix.extend(chunk)
            if not hmac.compare_digest(bytes(prefix), token):
                sys.stderr.write(
                    "[CONTROL] UF2_ERROR: rejected an unauthenticated UF2 "
                    "upload (token mismatch)\n")
                sys.stderr.flush()
                return None
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
        # Authenticate, then read all UF2 data from the picotool wrapper.
        uf2_data = self._drain_upload(conn)
        if uf2_data is None:
            return  # unauthenticated / aborted; nothing to relay

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


def run_uf2_relay_thread(port, uf2_active, rfc2217_active=None, token=""):
    """Thread entry point: serve picotool-wrapper uploads until process exit."""
    Uf2Relay(port, uf2_active, rfc2217_active, token).serve_forever()
