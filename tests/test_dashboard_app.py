"""Tests for the dashboard TUI (com2tty.windows.dashboard.app) and the
``run_dashboard`` entry point.

The view is exercised through Textual's headless ``run_test`` pilot. Each
scenario builds a ``DashboardApp`` with a fake ``BridgeManager`` (so no real
WSL helper is spawned) and patches port/distro discovery, then drives the same
handlers the UI invokes. Async scenarios are wrapped in ``asyncio.run`` so the
file stays plain ``unittest`` like the rest of the suite.
"""
import asyncio
import logging
import os
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from textual import events
from textual.geometry import Size
from textual.widgets import Button, DataTable, MarkdownViewer, Select, Static

from com2tty.windows.dashboard.app import (
    PAD_SLOTS,
    DashboardApp,
    ReadmeScreen,
    _local_readme,
    _readme_text,
)
from com2tty.windows.dashboard.manager import BridgeManager

PORTS = [
    {"device": "COM3", "board": "rp2040", "vid_pid": "2E8A:0005",
     "busid": "2-6", "serial_number": "X", "description": "Pico"},
    {"device": "COM5", "board": "esp32", "vid_pid": "10C4:EA60",
     "busid": "2-7", "serial_number": "Y", "description": "ESP"},
]


class _FakeRunner:
    """Blocks on the stop event like the real bridges so an attached device
    stays "live" for the duration of a scenario."""

    def __init__(self):
        self.calls = []

    def __call__(self, *, stop_event=None, **kwargs):
        self.calls.append(kwargs)
        if stop_event is not None:
            stop_event.wait(timeout=5.0)
        return "stop"


def _build_app():
    app = DashboardApp(rfc2217_port=4000)
    app.manager = BridgeManager(
        serial_runner=_FakeRunner(), gamepad_runner=_FakeRunner(),
        respawn_runner=lambda target, **kw: target(**kw), rfc2217_base=4000)
    return app


@asynccontextmanager
async def _running(ports=PORTS, distros=("Ubuntu",), size=(100, 30)):
    with patch("com2tty.windows.dashboard.app.collect_ports",
               side_effect=lambda: list(ports)), \
         patch("com2tty.windows.dashboard.app.list_wsl_distros",
               return_value=list(distros)):
        app = _build_app()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            yield app, pilot


def _static_text(widget):
    """Read a Static's current content (Textual stores it name-mangled)."""
    return str(getattr(widget, "_Static__content", ""))


def _summary(app):
    return _static_text(app.query_one("#attach-summary", Static))


def _doctor_summary(app):
    return _static_text(app.query_one("#doctor-summary", Static))


# -- module-level helpers ---------------------------------------------------

class TestReadmeHelpers(unittest.TestCase):

    def test_local_readme_found_in_checkout(self):
        path = _local_readme()
        self.assertIsNotNone(path)
        self.assertTrue(str(path).endswith("README.md"))

    def test_readme_text_reads_local_file(self):
        text = _readme_text()
        self.assertTrue(text)
        self.assertGreater(len(text), 100)

    def test_readme_text_falls_back_to_package_metadata(self):
        with patch("com2tty.windows.dashboard.app._local_readme",
                   return_value=None):
            text = _readme_text()
        self.assertTrue(text)

    def test_readme_text_none_when_unavailable(self):
        import importlib.metadata as md
        with patch("com2tty.windows.dashboard.app._local_readme",
                   return_value=None), \
             patch.object(md, "metadata", side_effect=Exception("boom")):
            self.assertIsNone(_readme_text())


# -- mount / tables / summary ----------------------------------------------

class TestDashboardMount(unittest.TestCase):

    def test_mount_builds_tables_and_distros(self):
        async def scenario():
            async with _running() as (app, _):
                serial = app.query_one("#serial-table", DataTable)
                self.assertEqual(serial.row_count, 2)
                self.assertEqual(len(serial.columns), 5)
                gamepad = app.query_one("#gamepad-table", DataTable)
                self.assertEqual(gamepad.row_count, 4)
                distro = app.query_one("#distro", Select)
                values = [v for _, v in distro._options]
                self.assertIn("Ubuntu", values)
                self.assertIn("", values)  # the (default) sentinel
                self.assertIn("no devices", _summary(app))
        asyncio.run(scenario())

    def test_mount_with_no_ports_shows_placeholder(self):
        async def scenario():
            async with _running(ports=[]) as (app, _):
                serial = app.query_one("#serial-table", DataTable)
                self.assertEqual(serial.row_count, 1)
                self.assertIn("no serial ports", str(serial.get_row_at(0)[0]))
        asyncio.run(scenario())

    def test_unmount_restores_log_handlers(self):
        async def scenario():
            root = logging.getLogger()
            before = list(root.handlers)
            async with _running() as (app, _):
                self.assertIn(app._log_handler, root.handlers)
            # After the context exits the app has unmounted.
            self.assertNotIn(app._log_handler, root.handlers)
            self.assertEqual(list(root.handlers), before)
        asyncio.run(scenario())


# -- serial attach / detach -------------------------------------------------

class TestSerialFlow(unittest.TestCase):

    def test_attach_serial_updates_table_and_summary(self):
        async def scenario():
            async with _running() as (app, pilot):
                table = app.query_one("#serial-table", DataTable)
                table.move_cursor(row=0)
                app._attach_serial()
                await pilot.pause()
                self.assertTrue(app.manager.is_attached("serial", "COM3"))
                self.assertEqual(str(table.get_row_at(0)[3]), "/tmp/ttyUSB0")
                self.assertIn("ATTACHED", str(table.get_row_at(0)[4]))
                self.assertIn("1 serial", _summary(app))
                # An attach raises the action-required notice.
                self.assertEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_attach_serial_forwards_all_options(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-baud").value = "115200"
                app.query_one("#serial-board", Select).value = "esp32"
                app.query_one("#serial-bytesize", Select).value = 7
                app.query_one("#serial-parity", Select).value = "E"
                app.query_one("#serial-stopbits", Select).value = 2.0
                app.query_one("#serial-xonxoff").value = True
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                app._attach_serial()
                await pilot.pause()
                call = app.manager._serial_runner.calls[0]
                self.assertEqual(call["baud"], "115200")
                self.assertEqual(call["board"], "esp32")
                self.assertEqual(call["bytesize"], 7)
                self.assertEqual(call["parity"], "E")
                self.assertEqual(call["stopbits"], 2.0)
                self.assertTrue(call["xonxoff"])
        asyncio.run(scenario())

    def test_attach_serial_without_selection_warns(self):
        async def scenario():
            async with _running(ports=[]) as (app, pilot):
                # The placeholder row maps to no device.
                app._attach_serial()
                await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertEqual(app.manager.list_bridges(), [])
        asyncio.run(scenario())

    def test_double_attach_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                app._attach_serial()
                await pilot.pause()
                app._attach_serial()  # COM3 already attached
                await pilot.pause()
                # First notice = attach instructions; second = "already attached".
                self.assertEqual(len(app._notices), 2)
        asyncio.run(scenario())

    def test_detach_serial(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                app._attach_serial()
                await pilot.pause()
                app._detach_serial()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.manager.is_attached("serial", "COM3"))
        asyncio.run(scenario())

    def test_detach_serial_when_idle_notifies(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                app._detach_serial()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_refresh_action_and_periodic(self):
        async def scenario():
            ports = list(PORTS)
            with patch("com2tty.windows.dashboard.app.collect_ports",
                       side_effect=lambda: list(ports)), \
                 patch("com2tty.windows.dashboard.app.list_wsl_distros",
                       return_value=[]):
                app = _build_app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    table = app.query_one("#serial-table", DataTable)
                    self.assertEqual(table.row_count, 2)
                    ports.pop()  # unplug COM5
                    app.action_refresh()
                    await pilot.pause()
                    self.assertEqual(table.row_count, 1)
                    # Unchanged signature -> periodic refresh is a no-op.
                    app._periodic_refresh()
                    await pilot.pause()
                    self.assertEqual(table.row_count, 1)
        asyncio.run(scenario())


# -- gamepad ----------------------------------------------------------------

class TestGamepadFlow(unittest.TestCase):

    def test_attach_and_detach_gamepad(self):
        async def scenario():
            async with _running() as (app, pilot):
                table = app.query_one("#gamepad-table", DataTable)
                table.move_cursor(row=1)
                app._attach_gamepad()
                await pilot.pause()
                self.assertTrue(app.manager.is_attached("gamepad", 1))
                self.assertEqual(str(table.get_row_at(1)[1]), "/tmp/com2pad1")
                self.assertIn("1 pad", _summary(app))
                app._detach_gamepad()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.manager.is_attached("gamepad", 1))
        asyncio.run(scenario())

    def test_attach_gamepad_invalid_poll_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                app.query_one("#gamepad-poll").value = "fast"
                app._attach_gamepad()
                await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertFalse(app.manager.is_attached("gamepad", 0))
        asyncio.run(scenario())

    def test_attach_gamepad_uinput_permission_warning(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                app.query_one("#gamepad-uinput").value = True
                with patch("com2tty.windows.doctor.check_uinput",
                           return_value=("WARN", "/dev/uinput", "not accessible")):
                    app._attach_gamepad()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                warnings = [n for n in app._notices
                            if "uinput" in getattr(n, "notice_message", "")]
                self.assertTrue(warnings)
        asyncio.run(scenario())

    def test_detach_gamepad_when_idle_notifies(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                app._detach_gamepad()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())


# -- doctor -----------------------------------------------------------------

class TestDoctor(unittest.TestCase):

    def test_run_doctor_renders_results(self):
        results = [("OK", "wsl.exe", ""), ("WARN", "uinput", "no"),
                   ("FAIL", "python3", "missing"), ("SKIP", "x", "")]

        async def scenario():
            async with _running() as (app, pilot):
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           return_value=results):
                    app.action_run_doctor()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                table = app.query_one("#doctor-table", DataTable)
                self.assertEqual(table.row_count, 4)
                self.assertIn("failed",
                              _doctor_summary(app))
        asyncio.run(scenario())

    def test_render_doctor_summary_variants(self):
        async def scenario():
            async with _running() as (app, _):
                app._render_doctor_results([("OK", "a", "")])
                self.assertIn("All checks passed",
                              _doctor_summary(app))
                app._render_doctor_results([("OK", "a", ""), ("WARN", "b", "x")])
                self.assertIn("warning",
                              _doctor_summary(app))
        asyncio.run(scenario())

    def test_run_doctor_handles_exception(self):
        async def scenario():
            async with _running() as (app, pilot):
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           side_effect=RuntimeError("kaboom")):
                    app.action_run_doctor()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertIn("Doctor failed",
                              _doctor_summary(app))
        asyncio.run(scenario())


# -- notices ----------------------------------------------------------------

class TestNotices(unittest.TestCase):

    def test_dedupe_cap_and_close(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.emit_notice("same", "T", "information")
                app.emit_notice("same", "T", "information")  # dedupe
                await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertTrue(app.query_one("#notices").display)
                for i in range(7):
                    app.emit_notice(f"m{i}", "", "warning")
                await pilot.pause()
                self.assertEqual(len(app._notices), 5)  # capped
                close = app._notices[0].query_one(".notice-close", Button)
                await pilot.click(close)
                await pilot.pause()
                self.assertEqual(len(app._notices), 4)
                for row in list(app._notices):
                    app._dismiss_notice(row)
                await pilot.pause()
                self.assertFalse(app.query_one("#notices").display)
                # Dismissing an unknown row is a no-op, not an error.
                app._dismiss_notice(app._notices)
        asyncio.run(scenario())

    def test_error_log_raises_notice(self):
        async def scenario():
            async with _running() as (app, pilot):
                logging.getLogger().error("disaster")
                await pilot.pause()
                msgs = [getattr(n, "notice_message", "") for n in app._notices]
                self.assertIn("disaster", msgs)
                # An info record does not raise a notice.
                before = len(app._notices)
                logging.getLogger().info("calm")
                await pilot.pause()
                self.assertEqual(len(app._notices), before)
        asyncio.run(scenario())

    def test_log_handler_marshals_from_thread(self):
        async def scenario():
            async with _running() as (app, pilot):
                # Emit from a non-UI (executor) thread, awaited so the event
                # loop keeps running and the handler's cross-thread
                # call_from_thread path can complete. Calling Event.wait() on
                # the loop thread instead would block the loop and deadlock the
                # marshaling.
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: logging.getLogger().error("from-thread"))
                await pilot.pause()
                msgs = [getattr(n, "notice_message", "") for n in app._notices]
                self.assertIn("from-thread", msgs)
        asyncio.run(scenario())


# -- responsive / distro / readme ------------------------------------------

class TestResponsiveAndChrome(unittest.TestCase):

    def test_breakpoint_classes(self):
        async def scenario():
            async with _running(size=(130, 40)) as (app, _):
                self.assertTrue(app.screen.has_class("-wide"))
            async with _running(size=(60, 18)) as (app, _):
                self.assertTrue(app.screen.has_class("-narrow"))
                self.assertTrue(app.screen.has_class("-short"))
        asyncio.run(scenario())

    def test_on_resize_handler(self):
        async def scenario():
            async with _running(size=(100, 30)) as (app, _):
                app.on_resize(events.Resize(Size(130, 40), Size(130, 40)))
                self.assertTrue(app.screen.has_class("-wide"))
        asyncio.run(scenario())

    def test_distro_selection_updates_target(self):
        async def scenario():
            async with _running() as (app, pilot):
                select = app.query_one("#distro", Select)
                select.value = "Ubuntu"
                await pilot.pause()
                self.assertEqual(app.distro, "Ubuntu")
                select.value = ""  # the (default) sentinel
                await pilot.pause()
                self.assertIsNone(app.distro)
        asyncio.run(scenario())

    def test_distro_refresh_button(self):
        async def scenario():
            async with _running() as (app, pilot):
                await pilot.click("#distro-refresh")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.distro, None)
        asyncio.run(scenario())

    def test_constructor_distro_kept_in_options(self):
        async def scenario():
            with patch("com2tty.windows.dashboard.app.collect_ports",
                       return_value=[]), \
                 patch("com2tty.windows.dashboard.app.list_wsl_distros",
                       return_value=["Ubuntu"]):
                app = DashboardApp(distro="Custom", rfc2217_port=4000)
                app.manager = BridgeManager(
                    serial_runner=_FakeRunner(), gamepad_runner=_FakeRunner(),
                    respawn_runner=lambda t, **k: t(**k))
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    values = [v for _, v in app.query_one("#distro", Select)._options]
                    self.assertIn("Custom", values)
        asyncio.run(scenario())

    def test_open_readme_pushes_screen(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                self.assertIsInstance(app.screen, ReadmeScreen)
                self.assertIsNotNone(app.screen.query_one(MarkdownViewer))
                app.screen.action_close()
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ReadmeScreen)
        asyncio.run(scenario())

    def test_open_readme_when_unavailable_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                with patch("com2tty.windows.dashboard.app._readme_text",
                           return_value=None):
                    app.action_open_readme()
                    await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertNotIsInstance(app.screen, ReadmeScreen)
        asyncio.run(scenario())


class TestCoverageFill(unittest.TestCase):
    """Targeted tests for guard clauses and dispatch branches."""

    def test_button_dispatch_routes_every_id(self):
        async def scenario():
            async with _running() as (app, pilot):
                for bid in ("serial-attach", "serial-detach", "serial-refresh",
                            "gamepad-attach", "gamepad-detach", "doctor-run",
                            "distro-refresh"):
                    btn = app.query_one(f"#{bid}", Button)
                    app.on_button_pressed(Button.Pressed(btn))
                    await pilot.pause()
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           return_value=[("OK", "x", "")]):
                    await app.workers.wait_for_complete()
                await pilot.pause()
        asyncio.run(scenario())

    def test_attach_serial_read_options_error(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                with patch.object(app, "_read_serial_options",
                                  side_effect=ValueError("bad opt")):
                    app._attach_serial()
                    await pilot.pause()
                self.assertTrue(any("bad opt" in getattr(n, "notice_message", "")
                                    for n in app._notices))
        asyncio.run(scenario())

    def test_detach_serial_no_selection(self):
        async def scenario():
            async with _running(ports=[]) as (app, pilot):
                app._detach_serial()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_gamepad_no_selection_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                table = app.query_one("#gamepad-table", DataTable)
                # Push the cursor past the real slots so the selection resolves
                # to None (clearing the table does not reset cursor_row).
                table.add_row("x", "-", "-")
                table.move_cursor(row=len(PAD_SLOTS))
                self.assertIsNone(app._selected_slot())
                app._attach_gamepad()
                await pilot.pause()
                app._detach_gamepad()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_gamepad_double_attach_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                app._attach_gamepad()
                await pilot.pause()
                app._attach_gamepad()  # slot 0 already attached
                await pilot.pause()
                self.assertTrue(any("already attached"
                                    in getattr(n, "notice_message", "")
                                    for n in app._notices))
        asyncio.run(scenario())

    def test_periodic_refresh_swallows_errors(self):
        async def scenario():
            async with _running() as (app, pilot):
                with patch("com2tty.windows.dashboard.app.collect_ports",
                           side_effect=OSError("enum failed")):
                    app._periodic_refresh()  # must not raise
                await pilot.pause()
        asyncio.run(scenario())

    def test_restore_cursor_when_selection_vanishes(self):
        async def scenario():
            ports = list(PORTS)
            with patch("com2tty.windows.dashboard.app.collect_ports",
                       side_effect=lambda: list(ports)), \
                 patch("com2tty.windows.dashboard.app.list_wsl_distros",
                       return_value=[]):
                app = _build_app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.query_one("#serial-table", DataTable).move_cursor(row=1)
                    ports.pop()  # remove the selected COM5
                    app.refresh_serial_table()
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one("#serial-table", DataTable).row_count, 1)
        asyncio.run(scenario())

    def test_invalid_theme_is_ignored(self):
        async def scenario():
            with patch("com2tty.windows.dashboard.app.THEME", "no-such-theme"), \
                 patch("com2tty.windows.dashboard.app.collect_ports",
                       return_value=[]), \
                 patch("com2tty.windows.dashboard.app.list_wsl_distros",
                       return_value=[]):
                app = _build_app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    self.assertTrue(app.is_running)
        asyncio.run(scenario())

    def test_log_handler_dispatch_swallows_callback_error(self):
        async def scenario():
            async with _running() as (app, _):
                def boom():
                    raise RuntimeError("nope")
                # On the UI thread; the handler must swallow the failure.
                app._log_handler._dispatch(boom)
        asyncio.run(scenario())

    def test_log_handler_emit_swallows_format_error(self):
        async def scenario():
            async with _running() as (app, _):
                record = logging.LogRecord("x", logging.INFO, __file__, 1,
                                           "msg", None, None)
                with patch.object(app._log_handler, "format",
                                  side_effect=ValueError("fmt")):
                    app._log_handler.emit(record)  # must not raise
        asyncio.run(scenario())

    def test_readme_text_oserror_falls_through(self):
        class _BadPath:
            def read_text(self, encoding=None):
                raise OSError("locked")
        with patch("com2tty.windows.dashboard.app._local_readme",
                   return_value=_BadPath()):
            text = _readme_text()
        self.assertTrue(text)  # fell back to package metadata

    def test_local_readme_none_without_project_root(self):
        from pathlib import Path
        with patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(_local_readme())


if __name__ == "__main__":
    unittest.main()
