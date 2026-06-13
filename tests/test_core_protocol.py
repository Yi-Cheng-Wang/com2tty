import io
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.core import protocol


class TestFormatAndEmit(unittest.TestCase):

    def test_format_line_without_payload(self):
        self.assertEqual(protocol.format_line(protocol.RFC2217_CONNECT),
                         "[CONTROL] RFC2217_CONNECT")

    def test_format_line_with_payload(self):
        self.assertEqual(
            protocol.format_line(protocol.UF2_UPLOAD_START, "128:abc"),
            "[CONTROL] UF2_UPLOAD_START:128:abc")

    def test_emit_writes_line_and_flushes(self):
        stream = io.StringIO()
        flushed = []
        stream.flush = lambda: flushed.append(True)
        protocol.emit(stream, protocol.RFC2217_READY, 4000)
        self.assertEqual(stream.getvalue(), "[CONTROL] RFC2217_READY:4000\n")
        self.assertTrue(flushed)

    def test_uf2_ack_line_matches_catalogue(self):
        # The host writes UF2_ACK_LINE verbatim; it must equal the formatted
        # message plus newline, encoded.
        self.assertEqual(
            protocol.UF2_ACK_LINE,
            (protocol.format_line(protocol.UF2_ACK) + "\n").encode())

    def test_wire_literals_are_pinned(self):
        # These strings are the cross-pipe wire protocol; changing any of
        # them breaks mixed-version host/guest pairs.
        self.assertEqual(protocol.CONTROL_PREFIX, "[CONTROL]")
        self.assertEqual(protocol.SETTINGS, "SETTINGS")
        self.assertEqual(protocol.RFC2217_READY, "RFC2217_READY")
        self.assertEqual(protocol.RFC2217_CONNECT, "RFC2217_CONNECT")
        self.assertEqual(protocol.RFC2217_DISCONNECT, "RFC2217_DISCONNECT")
        self.assertEqual(protocol.RFC2217_ERROR, "RFC2217_ERROR")
        self.assertEqual(protocol.UF2_READY, "UF2_READY")
        self.assertEqual(protocol.UF2_UPLOAD_START, "UF2_UPLOAD_START")
        self.assertEqual(protocol.UF2_UPLOAD_END, "UF2_UPLOAD_END")
        self.assertEqual(protocol.UF2_ERROR, "UF2_ERROR")
        self.assertEqual(protocol.UF2_ACK_LINE, b"[CONTROL] UF2_ACK\n")
        self.assertEqual(protocol.PAD_READY, "PAD_READY")
        self.assertEqual(protocol.PAD_ERROR, "PAD_ERROR")
        self.assertEqual(protocol.PAD_UINPUT_UNAVAILABLE,
                         "PAD_UINPUT_UNAVAILABLE")
        self.assertEqual(protocol.PAD_PERMISSION_ERROR,
                         "PAD_PERMISSION_ERROR")


class TestControlDispatcher(unittest.TestCase):

    def setUp(self):
        self.dispatcher = protocol.ControlDispatcher()
        self.calls = []

    def _record(self, name):
        def handler(msg):
            self.calls.append((name, msg))
            return name
        return handler

    def test_dispatch_message_without_payload(self):
        self.dispatcher.register(protocol.RFC2217_CONNECT,
                                 self._record("connect"))
        result = self.dispatcher.dispatch("[CONTROL] RFC2217_CONNECT")
        self.assertEqual(result, "connect")
        ((_, msg),) = self.calls
        self.assertEqual(msg.name, "RFC2217_CONNECT")
        self.assertIsNone(msg.payload)
        self.assertEqual(msg.raw, "[CONTROL] RFC2217_CONNECT")

    def test_dispatch_message_with_payload(self):
        self.dispatcher.register(protocol.UF2_UPLOAD_START,
                                 self._record("start"))
        self.dispatcher.dispatch("[CONTROL] UF2_UPLOAD_START:128:abc")
        ((_, msg),) = self.calls
        self.assertEqual(msg.payload, "128:abc")

    def test_settings_payload_preserves_key_values(self):
        self.dispatcher.register(protocol.SETTINGS, self._record("settings"))
        self.dispatcher.dispatch(
            "[CONTROL] SETTINGS: baud=115200 bytesize=8 parity=N stopbits=1")
        ((_, msg),) = self.calls
        self.assertEqual(msg.payload.strip(),
                         "baud=115200 bytesize=8 parity=N stopbits=1")

    def test_longest_name_wins(self):
        self.dispatcher.register("UF2_UPLOAD", self._record("short"))
        self.dispatcher.register("UF2_UPLOAD_START", self._record("long"))
        self.dispatcher.dispatch("[CONTROL] UF2_UPLOAD_START:1")
        self.assertEqual(self.calls[0][0], "long")

    def test_prefix_is_not_misrouted_to_shorter_handler(self):
        # A registered name that is a prefix of the incoming name, with no
        # ":" separator after it, must NOT capture the line; it falls through
        # to the fallback rather than being misrouted with a None payload.
        seen = []
        self.dispatcher.set_fallback(seen.append)
        self.dispatcher.register("SETTINGS", self._record("settings"))
        self.dispatcher.dispatch("[CONTROL] SETTINGS_RESET")
        self.assertEqual(seen, ["[CONTROL] SETTINGS_RESET"])
        self.assertEqual(self.calls, [])

    def test_non_control_line_goes_to_fallback(self):
        seen = []
        self.dispatcher.set_fallback(seen.append)
        self.dispatcher.register(protocol.SETTINGS, self._record("settings"))
        self.dispatcher.dispatch("WSL bridge enter main loop.")
        self.assertEqual(seen, ["WSL bridge enter main loop."])
        self.assertEqual(self.calls, [])

    def test_unregistered_control_line_goes_to_fallback(self):
        # Mirrors the historical if/elif chain: an unknown [CONTROL] message
        # was logged like any other WSL stderr line.
        seen = []
        self.dispatcher.set_fallback(seen.append)
        self.dispatcher.dispatch("[CONTROL] FUTURE_MESSAGE:1")
        self.assertEqual(seen, ["[CONTROL] FUTURE_MESSAGE:1"])

    def test_no_fallback_returns_none(self):
        self.assertIsNone(self.dispatcher.dispatch("plain line"))

    def test_fallback_via_constructor(self):
        seen = []
        d = protocol.ControlDispatcher(fallback=seen.append)
        d.dispatch("hello")
        self.assertEqual(seen, ["hello"])


if __name__ == "__main__":
    unittest.main()
