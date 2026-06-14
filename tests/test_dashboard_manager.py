import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.windows.dashboard.manager import (
    FAILED,
    RUNNING,
    STOPPED,
    BridgeManager,
)


class _FakeRunner:
    """A stand-in for run_bridge/run_gamepad_bridge.

    Blocks on the supplied ``stop_event`` exactly like the real bridges, and
    records the kwargs it was called with so tests can assert wiring.
    """

    def __init__(self, exit_reason="stop"):
        self.exit_reason = exit_reason
        self.started = threading.Event()
        self.calls = []

    def __call__(self, *, stop_event=None, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if stop_event is not None:
            stop_event.wait(timeout=5.0)
        return self.exit_reason


def _make_manager(serial_runner=None, gamepad_runner=None, respawn_runner=None):
    # Provide no-op defaults so the manager never imports the real bridges.
    noop = _FakeRunner()
    return BridgeManager(
        serial_runner=serial_runner or noop,
        gamepad_runner=gamepad_runner or noop,
        respawn_runner=respawn_runner or (lambda target, **kw: target(**kw)),
    )


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def _call_for(runner, port):
    for call in runner.calls:
        if call.get("port") == port:
            return call
    return None


class TestBridgeManagerSerial(unittest.TestCase):

    def test_start_serial_marks_attached_and_running(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner)

        bridge_id = mgr.start_serial_bridge("COM3")

        self.assertEqual(bridge_id, "serial:COM3")
        self.assertTrue(runner.started.wait(timeout=2.0))
        self.assertTrue(mgr.is_attached("serial", "COM3"))
        snap = mgr.get(bridge_id)
        self.assertEqual(snap["state"], RUNNING)
        self.assertEqual(snap["kind"], "serial")

        mgr.stop_bridge(bridge_id)
        self.assertFalse(mgr.is_attached("serial", "COM3"))
        self.assertIsNone(mgr.get(bridge_id))

    def test_serial_options_forwarded(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner)

        mgr.start_serial_bridge("COM7", baud="115200", board="esp32",
                                distro="Ubuntu", bytesize=7, parity="E",
                                stopbits=2, xonxoff=True)
        runner.started.wait(timeout=2.0)

        call = _call_for(runner, "COM7")
        self.assertEqual(call["baud"], "115200")
        self.assertEqual(call["board"], "esp32")
        self.assertEqual(call["distro"], "Ubuntu")
        self.assertEqual(call["bytesize"], 7)
        self.assertEqual(call["parity"], "E")
        self.assertEqual(call["stopbits"], 2)
        self.assertTrue(call["xonxoff"])
        self.assertTrue(call["wait"])
        mgr.stop_all()

    def test_serial_auto_allocates_distinct_slots(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner)  # base 4000, /tmp/ttyUSB0

        id1 = mgr.start_serial_bridge("COM3")
        self.assertTrue(_wait_for(lambda: _call_for(runner, "COM3")))
        id2 = mgr.start_serial_bridge("COM5")
        self.assertTrue(_wait_for(lambda: _call_for(runner, "COM5")))

        c1, c2 = _call_for(runner, "COM3"), _call_for(runner, "COM5")
        # First slot keeps the base endpoint/port and owns the env setup.
        self.assertEqual(c1["wsl_tty"], "/tmp/ttyUSB0")
        self.assertEqual(c1["rfc2217_port"], 4000)
        self.assertTrue(c1["env_setup"])
        # Second slot is shifted so the two bridges never collide.
        self.assertEqual(c2["wsl_tty"], "/tmp/ttyUSB1")
        self.assertEqual(c2["rfc2217_port"], 4002)
        self.assertFalse(c2["env_setup"])
        # The allocation is also visible on the snapshots for the UI.
        self.assertEqual(mgr.get(id1)["endpoint"], "/tmp/ttyUSB0")
        self.assertEqual(mgr.get(id2)["rfc2217_port"], 4002)
        mgr.stop_all()

    def test_serial_index_reclaimed_after_detach(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner)

        id1 = mgr.start_serial_bridge("COM3")  # slot 0
        self.assertTrue(_wait_for(lambda: _call_for(runner, "COM3")))
        mgr.stop_bridge(id1)  # frees slot 0
        mgr.start_serial_bridge("COM5")  # should reuse slot 0
        self.assertTrue(_wait_for(lambda: _call_for(runner, "COM5")))

        self.assertEqual(_call_for(runner, "COM5")["rfc2217_port"], 4000)
        self.assertEqual(_call_for(runner, "COM5")["wsl_tty"], "/tmp/ttyUSB0")
        mgr.stop_all()

    def test_double_attach_same_port_raises(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner)

        mgr.start_serial_bridge("COM3")
        runner.started.wait(timeout=2.0)
        with self.assertRaises(ValueError):
            mgr.start_serial_bridge("COM3")
        mgr.stop_all()

    def test_failed_runner_marked_failed(self):
        def boom(*, stop_event=None, **kwargs):
            raise RuntimeError("no WSL")

        mgr = _make_manager(serial_runner=boom)
        bridge_id = mgr.start_serial_bridge("COM3")

        self.assertTrue(_wait_for(
            lambda: (mgr.get(bridge_id) or {}).get("state") == FAILED))
        snap = mgr.get(bridge_id)
        self.assertIn("no WSL", snap["error"])
        # A failed (terminal) bridge is no longer considered attached.
        self.assertFalse(mgr.is_attached("serial", "COM3"))

    def test_natural_exit_marks_stopped(self):
        def quick(*, stop_event=None, **kwargs):
            return "wsl-exited"

        mgr = _make_manager(serial_runner=quick)
        bridge_id = mgr.start_serial_bridge("COM3")

        self.assertTrue(_wait_for(
            lambda: (mgr.get(bridge_id) or {}).get("state") == STOPPED))
        self.assertEqual(mgr.get(bridge_id)["exit_reason"], "wsl-exited")


class TestBridgeManagerGamepad(unittest.TestCase):

    def test_start_gamepad_attached(self):
        runner = _FakeRunner()
        mgr = _make_manager(gamepad_runner=runner)

        bridge_id = mgr.start_gamepad_bridge(1, poll_hz=500, name="Pad")

        self.assertEqual(bridge_id, "gamepad:1")
        runner.started.wait(timeout=2.0)
        self.assertTrue(mgr.is_attached("gamepad", 1))
        call = runner.calls[0]
        self.assertEqual(call["pad_index"], 1)
        self.assertEqual(call["poll_hz"], 500)
        self.assertEqual(call["name"], "Pad")
        mgr.stop_bridge(bridge_id)
        self.assertFalse(mgr.is_attached("gamepad", 1))


class TestBridgeManagerRespawn(unittest.TestCase):

    def test_auto_respawn_uses_respawn_runner(self):
        runner = _FakeRunner()
        seen = {}

        def fake_respawn(target, *, stop_event=None, **kwargs):
            seen["target"] = target
            return target(stop_event=stop_event, **kwargs)

        mgr = _make_manager(serial_runner=runner, respawn_runner=fake_respawn)
        bridge_id = mgr.start_serial_bridge("COM3", auto_respawn=True)
        runner.started.wait(timeout=2.0)
        self.assertIs(seen["target"], runner)
        mgr.stop_bridge(bridge_id)


class TestBridgeManagerBulk(unittest.TestCase):

    def test_stop_all_clears_everything(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner, gamepad_runner=runner)

        mgr.start_serial_bridge("COM3")
        mgr.start_serial_bridge("COM5")
        self.assertEqual(len(mgr.list_bridges()), 2)

        mgr.stop_all()
        self.assertEqual(mgr.list_bridges(), [])

    def test_attached_counts_by_kind(self):
        runner = _FakeRunner()
        mgr = _make_manager(serial_runner=runner, gamepad_runner=runner)

        self.assertEqual(mgr.attached_counts(), {})
        mgr.start_serial_bridge("COM3")
        mgr.start_serial_bridge("COM5")
        gid = mgr.start_gamepad_bridge(0)
        self.assertTrue(_wait_for(
            lambda: mgr.attached_counts() == {"serial": 2, "gamepad": 1}))

        mgr.stop_bridge(gid)
        self.assertEqual(mgr.attached_counts(), {"serial": 2})
        mgr.stop_all()
        self.assertEqual(mgr.attached_counts(), {})


class TestBridgeManagerMisc(unittest.TestCase):

    def test_default_runners_resolved_from_bridge_modules(self):
        # With no runners injected, the manager wires up the real entry points
        # (imported lazily) rather than leaving them None.
        from com2tty.windows.bridge_app import run_bridge, run_with_respawn
        from com2tty.windows.gamepad_app import run_gamepad_bridge

        mgr = BridgeManager()
        self.assertIs(mgr._serial_runner, run_bridge)
        self.assertIs(mgr._gamepad_runner, run_gamepad_bridge)
        self.assertIs(mgr._respawn_runner, run_with_respawn)

    def test_stop_unknown_bridge_returns_false(self):
        mgr = _make_manager()
        self.assertFalse(mgr.stop_bridge("serial:NOPE"))

    def test_indexed_path_without_trailing_number_appends_index(self):
        # /tmp/com2pad has no trailing digit on slot 0's base, so the index is
        # appended instead of incremented.
        self.assertEqual(BridgeManager._indexed_path("/tmp/pad", 0), "/tmp/pad")
        self.assertEqual(BridgeManager._indexed_path("/tmp/pad", 2), "/tmp/pad2")
        self.assertEqual(BridgeManager._indexed_path("/tmp/ttyUSB0", 3),
                         "/tmp/ttyUSB3")


if __name__ == "__main__":
    unittest.main()
