"""Tests for the dashboard TUI (com2tty.windows.dashboard) and the
``run_dashboard`` entry point.

The view is exercised through Textual's headless ``run_test`` pilot. Each
scenario builds a ``DashboardApp`` with a fake ``BridgeManager`` (so no real
WSL helper is spawned) and patches port/distro discovery, then drives the same
tab-widget methods the UI invokes. Async scenarios are wrapped in
``asyncio.run`` so the file stays plain ``unittest`` like the rest of the suite.

Since the v4 refactor each pane is its own widget (``SerialTab``,
``GamepadTab``, ``DoctorTab``); the attach/detach/run logic lives on those tabs
and the shared chrome (notices, distro switch, summary) lives on the app.
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
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Select,
    Static,
    TabbedContent,
)

from com2tty.windows.dashboard._screens import _MarkdownStatic
from com2tty.windows.dashboard.app import (
    PAD_SLOTS,
    DashboardApp,
    DoctorTab,
    GamepadTab,
    ReadmeScreen,
    SerialTab,
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


def _serial_tab(app):
    return app.query_one(SerialTab)


def _gamepad_tab(app):
    return app.query_one(GamepadTab)


def _doctor_tab(app):
    return app.query_one(DoctorTab)


@asynccontextmanager
async def _running(ports=PORTS, distros=("Ubuntu",), size=(100, 30)):
    with patch("com2tty.windows.dashboard._tabs.collect_ports",
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
        with patch("com2tty.windows.dashboard._constants._local_readme",
                   return_value=None):
            text = _readme_text()
        self.assertTrue(text)

    def test_readme_text_none_when_unavailable(self):
        import importlib.metadata as md
        with patch("com2tty.windows.dashboard._constants._local_readme",
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

    def test_tab_content_is_laid_out_and_visible(self):
        # Regression: wrapping each pane in a plain Widget collapsed it to zero
        # height, hiding the table and pushing the docked action bar to a
        # negative y (off-screen). The tab widgets must be vertical containers
        # that fill their TabPane, so the active tab's content has real height
        # and its action bar stays on-screen.
        async def scenario():
            async with _running(size=(100, 40)) as (app, _):
                serial = app.query_one(SerialTab)
                self.assertGreater(serial.size.height, 0)
                self.assertGreater(
                    app.query_one("#serial-table", DataTable).size.height, 0)
                self.assertGreaterEqual(app.query_one(".actionbar").region.y, 0)
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
                _serial_tab(app).attach()
                await pilot.pause()
                self.assertTrue(app.manager.is_attached("serial", "COM3"))
                self.assertEqual(str(table.get_row_at(0)[3]), "/tmp/ttyUSB0")
                self.assertIn("ATTACHED", str(table.get_row_at(0)[4]))
                self.assertIn("1 serial", _summary(app))
                # An attach raises the action-required notice.
                self.assertEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_attach_serial_dev_checkbox_shows_link_command(self):
        # The checkbox is optional guidance: it pops the one-time link command
        # (targeting the real /tmp endpoint) so the user can copy-paste it.
        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                table = app.query_one("#serial-table", DataTable)
                table.move_cursor(row=0)
                app.query_one("#serial-dev", Checkbox).value = True
                _serial_tab(app).attach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                # The device is still served at /tmp; the alias is the user's.
                self.assertEqual(str(table.get_row_at(0)[3]), "/tmp/ttyUSB0")
                self.assertIsInstance(app.screen, CommandHelpScreen)
                self.assertEqual(
                    app.screen._commands,
                    ["sudo ln -sf /tmp/ttyUSB0 /dev/ttyUSB0"])
        asyncio.run(scenario())

    def test_attach_serial_no_command_when_checkbox_off(self):
        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                _serial_tab(app).attach()  # checkbox off by default
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertNotIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())

    def test_dev_alias_auto_detected_by_periodic_poll(self):
        # The core requirement: with no configuration the user runs
        # `sudo ln -sf /tmp/ttyUSB0 /dev/ttyACM0` themselves; the poll discovers
        # that alias (any name) and shows it -- reverting to /tmp if removed.
        async def scenario():
            async with _running() as (app, pilot):
                table = app.query_one("#serial-table", DataTable)
                table.move_cursor(row=0)
                _serial_tab(app).attach()  # plain /tmp/ttyUSB0 bridge
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(str(table.get_row_at(0)[3]), "/tmp/ttyUSB0")
                # The user aliased it to a name the dashboard never specified.
                with patch("com2tty.windows.doctor.find_dev_aliases",
                           return_value={"/tmp/ttyUSB0": "/dev/ttyACM0"}):
                    _serial_tab(app).poll_dev_aliases()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertEqual(str(table.get_row_at(0)[3]), "/dev/ttyACM0")
                # Removing the alias reverts the displayed endpoint to /tmp.
                with patch("com2tty.windows.doctor.find_dev_aliases",
                           return_value={"/tmp/ttyUSB0": None}):
                    _serial_tab(app).poll_dev_aliases()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertEqual(str(table.get_row_at(0)[3]), "/tmp/ttyUSB0")
        asyncio.run(scenario())

    def test_poll_dev_aliases_no_serial_bridges_is_noop(self):
        # With nothing attached the poll never shells into WSL.
        async def scenario():
            async with _running() as (app, pilot):
                with patch("com2tty.windows.doctor.find_dev_aliases") as m_find:
                    _serial_tab(app).poll_dev_aliases()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                m_find.assert_not_called()
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
                _serial_tab(app).attach()
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
                _serial_tab(app).attach()
                await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertEqual(app.manager.list_bridges(), [])
        asyncio.run(scenario())

    def test_double_attach_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                _serial_tab(app).attach()
                await pilot.pause()
                _serial_tab(app).attach()  # COM3 already attached
                await pilot.pause()
                # First notice = attach instructions; second = "already attached".
                self.assertEqual(len(app._notices), 2)
        asyncio.run(scenario())

    def test_detach_serial(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                _serial_tab(app).attach()
                await pilot.pause()
                _serial_tab(app).detach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.manager.is_attached("serial", "COM3"))
        asyncio.run(scenario())

    def test_detach_serial_when_idle_notifies(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                _serial_tab(app).detach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_refresh_action_and_periodic(self):
        async def scenario():
            ports = list(PORTS)
            with patch("com2tty.windows.dashboard._tabs.collect_ports",
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
                    # The periodic refresh enumerates ports in a worker thread;
                    # the signature is unchanged so the rebuild is a no-op.
                    app._periodic_refresh()
                    await app.workers.wait_for_complete()
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
                _gamepad_tab(app).attach()
                await pilot.pause()
                self.assertTrue(app.manager.is_attached("gamepad", 1))
                self.assertEqual(str(table.get_row_at(1)[1]), "/tmp/com2pad1")
                self.assertIn("1 pad", _summary(app))
                _gamepad_tab(app).detach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.manager.is_attached("gamepad", 1))
        asyncio.run(scenario())

    def test_attach_gamepad_invalid_poll_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                app.query_one("#gamepad-poll").value = "fast"
                _gamepad_tab(app).attach()
                await pilot.pause()
                self.assertEqual(len(app._notices), 1)
                self.assertFalse(app.manager.is_attached("gamepad", 0))
        asyncio.run(scenario())

    def test_attach_gamepad_uinput_ok_shows_uinput_endpoint(self):
        # When /dev/uinput is writable the bridge creates a real device, so the
        # Endpoint column must show the uinput device class -- never the /tmp
        # fallback path.
        async def scenario():
            async with _running() as (app, pilot):
                table = app.query_one("#gamepad-table", DataTable)
                table.move_cursor(row=2)
                app.query_one("#gamepad-uinput").value = True
                with patch("com2tty.windows.doctor.check_uinput",
                           return_value=("OK", "/dev/uinput", "")):
                    _gamepad_tab(app).attach()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertIn("uinput", str(table.get_row_at(2)[1]))
                self.assertNotIn("/tmp", str(table.get_row_at(2)[1]))
                # No fallback => no remediation modal was pushed.
                self.assertNotIsInstance(app.screen, ReadmeScreen)
        asyncio.run(scenario())

    def test_attach_gamepad_uinput_permission_warning(self):
        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                table = app.query_one("#gamepad-table", DataTable)
                table.move_cursor(row=0)
                app.query_one("#gamepad-uinput").value = True
                with patch("com2tty.windows.doctor.check_uinput",
                           return_value=("WARN", "/dev/uinput", "not accessible")):
                    _gamepad_tab(app).attach()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                warnings = [n for n in app._notices
                            if "uinput" in getattr(n, "notice_message", "")]
                self.assertTrue(warnings)
                # The endpoint column falls back to the /tmp stream path...
                self.assertIn("/tmp/com2pad0", str(table.get_row_at(0)[1]))
                # ...and a copy-pasteable command modal is proactively shown.
                self.assertIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())

    def test_detach_gamepad_when_idle_notifies(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                _gamepad_tab(app).detach()
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
                doctor = _doctor_tab(app)
                doctor.render_results([("OK", "a", "")])
                self.assertIn("All checks passed",
                              _doctor_summary(app))
                doctor.render_results([("OK", "a", ""), ("WARN", "b", "x")])
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
            with patch("com2tty.windows.dashboard._tabs.collect_ports",
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
                # The README is rendered by our own renderer into one selectable
                # widget (so copy is reliable), with a top-right close button and
                # a centred hint. Markup is stripped, code kept verbatim.
                md = app.screen.query_one("#readme-md", _MarkdownStatic)
                self.assertTrue(md.allow_select)
                plain = md.render().plain
                self.assertNotIn("```", plain)
                self.assertNotIn("# ", plain)
                close = app.screen.query_one("#readme-close", Button)
                self.assertEqual(str(close.label), "✕")
                # The close button sits in the right-aligned top bar.
                self.assertEqual(
                    str(app.screen.query_one("#readme-topbar")
                        .styles.align_horizontal), "right")
                hint = app.screen.query_one("#readme-hint", Static)
                self.assertEqual(str(hint.styles.text_align), "center")
                # No "copy the whole document" affordance exists.
                self.assertEqual(len(app.screen.query("#readme-copyall")), 0)
                app.screen.action_close()
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ReadmeScreen)
        asyncio.run(scenario())

    def test_readme_link_click_routes_to_follow_link(self):
        # A clickable link span triggers action_link on the widget, which the
        # screen turns into a follow_link call.
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                md = screen.query_one("#readme-md", _MarkdownStatic)
                with patch.object(screen, "follow_link") as m_follow:
                    md.action_link("#usage")
                m_follow.assert_called_once_with("#usage")
        asyncio.run(scenario())

    def test_readme_anchor_jumps_in_document_not_browser(self):
        # A table-of-contents anchor scrolls the document; never opens a browser.
        async def scenario():
            async with _running(size=(110, 40)) as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                # Use a real anchor from the rendered README.
                anchor = "dashboard-mode"
                self.assertIn(anchor, screen._anchors)
                scroll = screen.query_one("#readme-scroll")
                before = scroll.scroll_offset.y
                with patch.object(app, "open_url") as m_open:
                    screen.follow_link("#" + anchor)
                    await pilot.pause()
                m_open.assert_not_called()
                self.assertGreater(scroll.scroll_offset.y, before)
        asyncio.run(scenario())

    def test_readme_unknown_anchor_hints(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                screen.follow_link("#no-such-section")
                self.assertIn("not found", _static_text(
                    screen.query_one("#readme-hint", Static)).lower())
        asyncio.run(scenario())

    def test_readme_web_link_opens_externally(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                with patch.object(app, "open_url") as m_open:
                    screen.follow_link("https://example.com")
                m_open.assert_called_once_with("https://example.com")
        asyncio.run(scenario())

    def test_readme_relative_link_not_opened(self):
        # A relative path must NOT shell out (this tripped the browser launch
        # and the folder-access prompt before).
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                with patch.object(app, "open_url") as m_open:
                    screen.follow_link("ARCHITECTURE.md")
                m_open.assert_not_called()
                self.assertIn("outside", _static_text(
                    screen.query_one("#readme-hint", Static)).lower())
        asyncio.run(scenario())

    def test_readme_close_button_dismisses(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                self.assertIsInstance(app.screen, ReadmeScreen)
                btn = app.screen.query_one("#readme-close", Button)
                app.screen.on_button_pressed(Button.Pressed(btn))
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ReadmeScreen)
        asyncio.run(scenario())

    def test_readme_copy_routes_selection_to_win32_clipboard(self):
        # Ctrl+C copies the screen selection through the host Win32 clipboard
        # (not OSC 52, which Windows drops) -- the rebuilt, crash-free copy path.
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                with patch.object(screen, "get_selected_text",
                                  return_value="pip install com2tty"), \
                        patch.object(app, "set_clipboard_text") as m_copy:
                    screen.action_copy_selection()
                m_copy.assert_called_once_with("pip install com2tty")
                self.assertIn("Copied", _static_text(
                    screen.query_one("#readme-hint", Static)))
        asyncio.run(scenario())

    def test_readme_copy_without_selection_hints(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                with patch.object(screen, "get_selected_text",
                                  return_value=None), \
                        patch.object(app, "set_clipboard_text") as m_copy:
                    screen.action_copy_selection()
                m_copy.assert_not_called()
                self.assertIn("select", _static_text(
                    screen.query_one("#readme-hint", Static)).lower())
        asyncio.run(scenario())

    def test_readme_ctrl_c_keypress_copies_selection(self):
        # End-to-end: the key press routes to our copy action, not help_quit.
        async def scenario():
            async with _running() as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                screen = app.screen
                with patch.object(screen, "get_selected_text",
                                  return_value="X"), \
                        patch.object(app, "set_clipboard_text") as m_copy:
                    await pilot.press("ctrl+c")
                    await pilot.pause()
                m_copy.assert_called_once_with("X")
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

    def test_readme_render_line_matches_full_render_and_is_lazy(self):
        # The README renders one visible row at a time so a drag-select stays
        # fast, but every row must be byte-identical to Textual's whole-document
        # render -- selection highlight and clickable-link styling included --
        # and the (costly) wrap must not be rebuilt on the selection refresh.
        async def scenario():
            from textual.geometry import Offset
            from textual.selection import Selection
            from textual.visual import Visual
            async with _running(size=(100, 40)) as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                md = app.screen.query_one("#readme-md", _MarkdownStatic)
                width = md.content_size.width
                md.screen.selections = {md: Selection(Offset(2, 5),
                                                       Offset(8, 12))}
                await pilot.pause()
                # Reference: the whole widget rendered in one pass.
                ref = Visual.to_strips(md, md._render(), width,
                                       md.content_region.height, md.visual_style)
                self.assertTrue(len(ref) > 100)  # a real, multi-row document
                key = md._wrap_key
                self.assertIsNotNone(key)
                for y in range(len(ref)):
                    row = md.render_line(y)
                    self.assertEqual(row.text, ref[y].text)
                    self.assertEqual([seg.style for seg in row],
                                     [seg.style for seg in ref[y]])
                # The selection refresh reused the cached wrap (no rebuild).
                self.assertEqual(md._wrap_key, key)
        asyncio.run(scenario())

    def test_readme_render_line_guards_zero_width_and_out_of_range(self):
        # The two defensive guards in the lazy renderer: a zero-width widget
        # renders nothing (and never attempts to wrap), and a row index past
        # the end of the document yields a blank, full-width strip.
        async def scenario():
            from unittest.mock import PropertyMock
            async with _running(size=(100, 40)) as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                md = app.screen.query_one("#readme-md", _MarkdownStatic)
                md.render_line(0)  # build the wrap cache
                # Beyond the last row: blank, but spanning the widget width.
                past = md.render_line(len(md._rows) + 5)
                self.assertEqual(past.text.strip(), "")
                self.assertEqual(past.cell_length, md.size.width)
                # Zero content width: empty strip and no wrap attempted.
                md._wrap_key = None
                with patch.object(_MarkdownStatic, "content_size",
                                  new_callable=PropertyMock,
                                  return_value=Size(0, 0)):
                    self.assertEqual(md.render_line(0).text, "")
                self.assertIsNone(md._wrap_key)
        asyncio.run(scenario())

    def test_selection_style_mirrors_each_textual_selection_model(self):
        # _selection_style must reproduce however the *installed* Textual's
        # Visual.to_strips derives the screen--selection style, and that
        # derivation differs by version. Force each branch deterministically
        # (independent of the installed Textual) by controlling the partial
        # style's foreground:
        #   - a transparent foreground => the modern overlay model: keep only
        #     the blendable background so the text colour shows through;
        #   - an opaque foreground => older Textual flattened the component, so
        #     fall back to the pre-blended get_component_rich_style verbatim.
        from textual.color import Color
        from textual.style import Style
        from com2tty.windows.dashboard import _screens

        async def scenario():
            async with _running(size=(100, 40)) as (app, pilot):
                app.action_open_readme()
                for _ in range(4):
                    await pilot.pause()
                md = app.screen.query_one("#readme-md", _MarkdownStatic)
                background = Color(1, 2, 3)

                # Modern overlay: transparent foreground -> background only.
                modern = Style(background=background,
                               foreground=Color(9, 9, 9, a=0))
                with patch.object(_screens.Style, "from_styles",
                                  return_value=modern):
                    result = md._selection_style()
                self.assertEqual(result, Style(background=background))

                # Older flattened model: an opaque foreground means we must use
                # the pre-blended rich style verbatim (a sentinel proves it).
                flattened = Style(background=background,
                                  foreground=Color(255, 255, 255))
                sentinel = Style(background=Color(7, 7, 7))
                with patch.object(_screens.Style, "from_styles",
                                  return_value=flattened), \
                        patch.object(_screens.Style, "from_rich_style",
                                     return_value=sentinel):
                    result = md._selection_style()
                self.assertIs(result, sentinel)
        asyncio.run(scenario())

    def test_refresh_binding_switches_to_serial_tab(self):
        # The 'r' binding must surface its result: switch to the Serial tab.
        async def scenario():
            async with _running() as (app, pilot):
                tabs = app.query_one(TabbedContent)
                tabs.active = "doctor-tab"
                await pilot.pause()
                app.action_refresh()
                await pilot.pause()
                self.assertEqual(tabs.active, "serial-tab")
        asyncio.run(scenario())

    def test_doctor_binding_switches_to_doctor_tab(self):
        # The 'd' binding must switch to the Doctor tab so results are visible.
        async def scenario():
            async with _running() as (app, pilot):
                tabs = app.query_one(TabbedContent)
                self.assertEqual(tabs.active, "serial-tab")
                with patch.object(_doctor_tab(app), "run"):
                    app.action_run_doctor()
                    await pilot.pause()
                self.assertEqual(tabs.active, "doctor-tab")
        asyncio.run(scenario())


class TestPermissionRemediation(unittest.TestCase):
    """The copy-pasteable command modal and the Doctor 'Setup commands' flow."""

    def test_set_clipboard_prefers_win32_then_osc52(self):
        # The clipboard is set in-process via the Win32 API (no clip.exe
        # subprocess, which disturbed the console and crashed copy on Ctrl+C);
        # OSC 52 is used only when that call reports failure.
        async def scenario():
            async with _running() as (app, _):
                # The write runs on a worker thread (so a slow clipboard
                # listener cannot freeze the UI), so wait for it before
                # asserting -- while the patch is still in scope.
                # Win32 set succeeds -> no OSC 52 fallback.
                with patch("com2tty.windows.os_hacks.clipboard."
                           "set_windows_clipboard", return_value=True) as m_win, \
                        patch.object(app, "copy_to_clipboard") as m_osc:
                    app.set_clipboard_text("cmd")
                    await app.workers.wait_for_complete()
                m_win.assert_called_once_with("cmd")
                m_osc.assert_not_called()
                # Win32 set unavailable -> fall back to Textual's OSC 52 copy.
                with patch("com2tty.windows.os_hacks.clipboard."
                           "set_windows_clipboard", return_value=False), \
                        patch.object(app, "copy_to_clipboard") as m_osc2:
                    app.set_clipboard_text("cmd")
                    await app.workers.wait_for_complete()
                m_osc2.assert_called_once_with("cmd")
        asyncio.run(scenario())

    def test_command_help_screen_copy_and_close(self):
        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                app.show_command_help("Title", "explain", ["cmd one", "cmd two"])
                for _ in range(3):
                    await pilot.pause()
                self.assertIsInstance(app.screen, CommandHelpScreen)
                screen = app.screen
                copy_btn = screen.query_one("#cmd-copy", Button)
                with patch.object(app, "set_clipboard_text") as m_copy:
                    screen.on_button_pressed(Button.Pressed(copy_btn))
                    await pilot.pause()
                m_copy.assert_called_once_with("cmd one\ncmd two")
                # Confirmation is an in-dialog status line (no toast), so the
                # copy does not trigger a relayout/flicker.
                status = str(getattr(screen.query_one("#cmd-status", Static),
                                     "_Static__content", ""))
                self.assertIn("Copied", status)
                self.assertEqual(app._notices, [])
                close_btn = screen.query_one("#cmd-close", Button)
                screen.on_button_pressed(Button.Pressed(close_btn))
                await pilot.pause()
                self.assertNotIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())

    def test_doctor_setup_commands_single_remedy_opens_modal(self):
        results = [("WARN", "/dev/uinput writable (gamepad --uinput)", "no")]

        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           return_value=results):
                    app.action_run_doctor()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                doctor = _doctor_tab(app)
                self.assertEqual(len(doctor._remediations), 1)
                doctor.show_fixes()
                await pilot.pause()
                self.assertIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())

    def test_doctor_setup_commands_aggregates_multiple(self):
        results = [("WARN", "/dev/uinput writable", "no"),
                   ("FAIL", "python3 in WSL", "missing")]

        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           return_value=results):
                    app.action_run_doctor()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                doctor = _doctor_tab(app)
                self.assertEqual(len(doctor._remediations), 2)
                doctor.show_fixes()
                await pilot.pause()
                self.assertIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())

    def test_doctor_setup_commands_button_without_run_notifies(self):
        async def scenario():
            from com2tty.windows.dashboard.app import CommandHelpScreen
            async with _running() as (app, pilot):
                btn = app.query_one("#doctor-fix", Button)
                _doctor_tab(app).on_button_pressed(Button.Pressed(btn))
                await pilot.pause()
                self.assertTrue(any(
                    "No setup commands" in getattr(n, "notice_message", "")
                    for n in app._notices))
                self.assertNotIsInstance(app.screen, CommandHelpScreen)
        asyncio.run(scenario())


class TestCoverageFill(unittest.TestCase):
    """Targeted tests for guard clauses and dispatch branches."""

    def test_button_dispatch_routes_every_id(self):
        async def scenario():
            async with _running() as (app, pilot):
                serial = _serial_tab(app)
                gamepad = _gamepad_tab(app)
                doctor = _doctor_tab(app)
                # Each tab handles and stops its own buttons.
                tab_buttons = [
                    (serial, "serial-attach"), (serial, "serial-detach"),
                    (serial, "serial-refresh"), (gamepad, "gamepad-attach"),
                    (gamepad, "gamepad-detach"), (doctor, "doctor-run"),
                    (doctor, "doctor-fix"),
                ]
                with patch("com2tty.windows.doctor.collect_doctor_results",
                           return_value=[("OK", "x", "")]):
                    for tab, bid in tab_buttons:
                        btn = app.query_one(f"#{bid}", Button)
                        tab.on_button_pressed(Button.Pressed(btn))
                        await pilot.pause()
                    await app.workers.wait_for_complete()
                # App-level chrome buttons route through the app handler.
                app.on_button_pressed(
                    Button.Pressed(app.query_one("#distro-refresh", Button)))
                await pilot.pause()
        asyncio.run(scenario())

    def test_attach_serial_read_options_error(self):
        async def scenario():
            async with _running() as (app, pilot):
                serial = _serial_tab(app)
                app.query_one("#serial-table", DataTable).move_cursor(row=0)
                with patch.object(serial, "read_options",
                                  side_effect=ValueError("bad opt")):
                    serial.attach()
                    await pilot.pause()
                self.assertTrue(any("bad opt" in getattr(n, "notice_message", "")
                                    for n in app._notices))
        asyncio.run(scenario())

    def test_detach_serial_no_selection(self):
        async def scenario():
            async with _running(ports=[]) as (app, pilot):
                _serial_tab(app).detach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_gamepad_no_selection_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                gamepad = _gamepad_tab(app)
                table = app.query_one("#gamepad-table", DataTable)
                # Push the cursor past the real slots so the selection resolves
                # to None (clearing the table does not reset cursor_row).
                table.add_row("x", "-", "-")
                table.move_cursor(row=len(PAD_SLOTS))
                self.assertIsNone(gamepad.selected_slot())
                gamepad.attach()
                await pilot.pause()
                gamepad.detach()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertGreaterEqual(len(app._notices), 1)
        asyncio.run(scenario())

    def test_gamepad_double_attach_warns(self):
        async def scenario():
            async with _running() as (app, pilot):
                app.query_one("#gamepad-table", DataTable).move_cursor(row=0)
                _gamepad_tab(app).attach()
                await pilot.pause()
                _gamepad_tab(app).attach()  # slot 0 already attached
                await pilot.pause()
                self.assertTrue(any("already attached"
                                    in getattr(n, "notice_message", "")
                                    for n in app._notices))
        asyncio.run(scenario())

    def test_periodic_refresh_swallows_errors(self):
        async def scenario():
            async with _running() as (app, pilot):
                # (1) A blocking-enumeration error inside the worker thread is
                # swallowed there, so the next tick can retry.
                with patch("com2tty.windows.dashboard._tabs.collect_ports",
                           side_effect=OSError("enum failed")):
                    app._periodic_refresh()
                    await app.workers.wait_for_complete()
                # (2) An error from the synchronous gamepad refresh is caught by
                # the periodic handler's own guard, never killing the timer.
                with patch.object(_gamepad_tab(app), "refresh_table",
                                  side_effect=RuntimeError("boom")):
                    app._periodic_refresh()  # must not raise
                await pilot.pause()
        asyncio.run(scenario())

    def test_restore_cursor_when_selection_vanishes(self):
        async def scenario():
            ports = list(PORTS)
            with patch("com2tty.windows.dashboard._tabs.collect_ports",
                       side_effect=lambda: list(ports)), \
                 patch("com2tty.windows.dashboard.app.list_wsl_distros",
                       return_value=[]):
                app = _build_app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.query_one("#serial-table", DataTable).move_cursor(row=1)
                    ports.pop()  # remove the selected COM5
                    _serial_tab(app).refresh_table()
                    await pilot.pause()
                    self.assertEqual(
                        app.query_one("#serial-table", DataTable).row_count, 1)
        asyncio.run(scenario())

    def test_invalid_theme_is_ignored(self):
        async def scenario():
            with patch("com2tty.windows.dashboard.app.THEME", "no-such-theme"), \
                 patch("com2tty.windows.dashboard._tabs.collect_ports",
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
        with patch("com2tty.windows.dashboard._constants._local_readme",
                   return_value=_BadPath()):
            text = _readme_text()
        self.assertTrue(text)  # fell back to package metadata

    def test_local_readme_none_without_project_root(self):
        from pathlib import Path
        with patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(_local_readme())


if __name__ == "__main__":
    unittest.main()
