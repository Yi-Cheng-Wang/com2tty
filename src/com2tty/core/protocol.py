"""The line-oriented ``[CONTROL]`` protocol spoken over the helper's stderr.

The WSL helper signals session state to the Windows host by writing control
lines to its stderr (and one acknowledgement, ``UF2_ACK``, travels the other
way over stdin). Every line has the shape::

    [CONTROL] <NAME>            e.g.  [CONTROL] RFC2217_CONNECT
    [CONTROL] <NAME>:<payload>  e.g.  [CONTROL] UF2_UPLOAD_START:163840:9f8e...

These strings are a wire protocol between two interpreters (the Windows
host and the WSL guest may even run different com2tty versions during an
upgrade), so the literals defined here must never change.

``emit``/``emit_line`` are the WSL-side producers; ``ControlDispatcher`` is
the Windows-side consumer that replaces the historical if/elif chain with a
handler registry (Command pattern).
"""
import collections

#: Prefix of every control line.
CONTROL_PREFIX = "[CONTROL]"

# --------------------------------------------------------------------------
# Message catalogue (WSL helper -> Windows host, over stderr)
# --------------------------------------------------------------------------

#: Dynamic serial settings detected on the PTY: ``baud=.. bytesize=..
#: parity=.. stopbits=..`` (space-separated ``k=v`` payload).
SETTINGS = "SETTINGS"

#: RFC 2217 forwarder lifecycle. READY carries the TCP port; ERROR carries a
#: human-readable reason (``bind failed: ...`` / ``session: ...``).
RFC2217_READY = "RFC2217_READY"
RFC2217_CONNECT = "RFC2217_CONNECT"
RFC2217_DISCONNECT = "RFC2217_DISCONNECT"
RFC2217_ERROR = "RFC2217_ERROR"

#: UF2 relay lifecycle. UPLOAD_START carries ``<size>:<md5>``.
UF2_READY = "UF2_READY"
UF2_UPLOAD_START = "UF2_UPLOAD_START"
UF2_UPLOAD_END = "UF2_UPLOAD_END"
UF2_ERROR = "UF2_ERROR"

#: Gamepad helper lifecycle.
PAD_READY = "PAD_READY"
PAD_ERROR = "PAD_ERROR"
PAD_UINPUT_UNAVAILABLE = "PAD_UINPUT_UNAVAILABLE"
PAD_PERMISSION_ERROR = "PAD_PERMISSION_ERROR"

# --------------------------------------------------------------------------
# Host -> WSL acknowledgement (over the helper's stdin)
# --------------------------------------------------------------------------

UF2_ACK = "UF2_ACK"

#: Exact bytes the host writes on the helper's stdin to acknowledge a UF2
#: transfer; the relay scans its stdin buffer for this byte sequence.
UF2_ACK_LINE = b"[CONTROL] UF2_ACK\n"


def format_line(name, payload=None):
    """Render one control line (without trailing newline)."""
    if payload is None:
        return "%s %s" % (CONTROL_PREFIX, name)
    return "%s %s:%s" % (CONTROL_PREFIX, name, payload)


def emit(stream, name, payload=None):
    """Write one control line to ``stream`` and flush it immediately.

    Flushing per line matters: the host reads stderr line-by-line to drive
    session state, and a buffered control message would stall the handshake.
    """
    stream.write(format_line(name, payload) + "\n")
    stream.flush()


#: A parsed control line. ``payload`` is the text after the first ``:`` of
#: the line (None when the line carries no payload); ``raw`` is the original
#: line for handlers that log or re-emit it verbatim.
ControlMessage = collections.namedtuple("ControlMessage",
                                        ["name", "payload", "raw"])


class ControlDispatcher:
    """Routes control lines to registered handlers; everything else falls
    through to a catch-all (typically plain WSL log forwarding).

    Matching reproduces the historical ``line.startswith("[CONTROL] NAME")``
    semantics: the longest registered name wins, so registering both ``X``
    and ``X_LONGER`` is safe.
    """

    def __init__(self, fallback=None):
        self._handlers = {}
        self._fallback = fallback

    def register(self, name, handler):
        """Register ``handler(msg: ControlMessage)`` for a message name."""
        self._handlers[name] = handler
        return handler

    def set_fallback(self, handler):
        """Handler for non-control lines, called as ``handler(line_str)``."""
        self._fallback = handler

    def dispatch(self, line_str):
        """Parse and route one stderr line. Returns the handler's result."""
        body = None
        if line_str.startswith(CONTROL_PREFIX + " "):
            body = line_str[len(CONTROL_PREFIX) + 1:]
        if body is not None:
            # Longest-name-first so e.g. UF2_UPLOAD_START is not shadowed by
            # a hypothetical UF2_UPLOAD registration.
            for name in sorted(self._handlers, key=len, reverse=True):
                if body.startswith(name):
                    payload = None
                    rest = body[len(name):]
                    if rest.startswith(":"):
                        payload = rest[1:]
                    return self._handlers[name](
                        ControlMessage(name, payload, line_str))
        if self._fallback is not None:
            return self._fallback(line_str)
        return None
