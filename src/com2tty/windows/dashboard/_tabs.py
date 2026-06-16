"""Self-contained tab widgets for the dashboard.

Each pane the dashboard shows is its own :class:`~textual.widget.Widget`
subclass that owns its layout (``compose``), its table (columns + rebuild),
its form fields, and the attach/detach/run business logic that used to live in
the ``_DashboardActionsMixin``. The app (:mod:`.app`) is reduced to chrome:
header, status bar, the dismissable notice strip, the shared log, and
sequencing -- it never reaches into a tab's internals.

Tabs reach back to the app only for genuinely shared state: the active
``BridgeManager`` (``self.app.manager``), the selected WSL distro
(``self.app.distro``), the notice strip (``self.app.emit_notice``), and the
status-bar tally (``self.app.update_summary``). Worker methods marshal UI
mutations through ``self.app.call_from_thread`` exactly as before.
"""
import io
from contextlib import redirect_stdout

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Input,
    Label,
    Select,
    Static,
)

from com2tty.core.boards import BOARD_CHOICES
from com2tty.windows.dashboard._constants import PAD_SLOTS, _STATUS_STYLES
from com2tty.windows.dashboard._remediation import UINPUT_SETUP
from com2tty.windows.discovery import collect_ports


class _DeviceTab(Vertical):
    """Shared helpers for the device (serial/gamepad) tabs.

    A vertical container (not a bare ``Widget``) so it fills its ``TabPane``
    and lays its children out top-to-bottom -- which is what the stylesheet
    assumes (``.tab-table`` takes ``1fr`` of the height, ``.actionbar`` docks
    to the bottom). A plain ``Widget`` collapses to zero height and the table
    and controls vanish.
    """

    @staticmethod
    def _restore_cursor(table, keys, wanted):
        """Move the row cursor back onto ``wanted`` after a rebuild."""
        if wanted is None:
            return
        try:
            index = keys.index(wanted)
        except ValueError:
            return
        table.move_cursor(row=index)

    @staticmethod
    def _status_cell(attached):
        if attached:
            return Text("● ATTACHED", style="bold green")
        return Text("○ idle", style="dim")


class SerialTab(_DeviceTab):
    """Serial-port pane: detected ports, attach options, attach/detach."""

    def __init__(self):
        super().__init__()
        self._rows = []  # row index -> device name
        # Last rendered table signature; the rebuild is skipped when it is
        # unchanged so the periodic refresh never flickers in place.
        self._signature = None

    def compose(self) -> ComposeResult:
        yield Static("Select a port, set options if needed, then Attach. Each "
                     "device is auto-assigned a WSL endpoint (Endpoint column).",
                     classes="hint")
        yield DataTable(id="serial-table", cursor_type="row",
                        zebra_stripes=True, classes="tab-table")
        with Vertical(classes="actionbar"):
            with Horizontal(classes="controls"):
                yield Label("Baud")
                yield Input(value="auto", compact=True, id="serial-baud")
                yield Label("Board")
                yield Select([(choice, choice) for choice in BOARD_CHOICES],
                             value="auto", allow_blank=False, compact=True,
                             id="serial-board")
                yield Checkbox("Auto-respawn", value=False, compact=True,
                               id="serial-respawn")
            with Collapsible(title="Advanced serial settings", collapsed=True):
                # Endpoint placement: kept here (a vertically-stacking section)
                # rather than the main control row so it never overlaps the
                # other controls on a narrow terminal.
                with Horizontal(classes="controls"):
                    # Informational: shows the one-time `sudo ln -sf` command on
                    # attach. The device is always served at /tmp; the dashboard
                    # auto-detects whatever /dev alias the user creates, so this
                    # is optional guidance, not configuration.
                    yield Checkbox("Show /dev link command", value=False,
                                   compact=True, id="serial-dev")
                with Horizontal(classes="controls"):
                    yield Label("WSL path")
                    yield Input(placeholder="(auto /tmp/ttyUSBn — rename the "
                                "WSL endpoint)", compact=True,
                                id="serial-wslpath")
                with Horizontal(classes="controls"):
                    yield Label("Byte size")
                    yield Select([("8", 8), ("7", 7), ("6", 6), ("5", 5)],
                                 value=8, allow_blank=False, compact=True,
                                 id="serial-bytesize")
                    yield Label("Parity")
                    yield Select([("None (N)", "N"), ("Even (E)", "E"),
                                  ("Odd (O)", "O"), ("Space (S)", "S"),
                                  ("Mark (M)", "M")],
                                 value="N", allow_blank=False, compact=True,
                                 id="serial-parity")
                with Horizontal(classes="controls"):
                    yield Label("Stop bits")
                    yield Select([("1", 1.0), ("1.5", 1.5), ("2", 2.0)],
                                 value=1.0, allow_blank=False, compact=True,
                                 id="serial-stopbits")
                with Horizontal(classes="controls"):
                    yield Checkbox("XON/XOFF", value=False, compact=True,
                                   id="serial-xonxoff")
                    yield Checkbox("RTS/CTS", value=False, compact=True,
                                   id="serial-rtscts")
                    yield Checkbox("DSR/DTR", value=False, compact=True,
                                   id="serial-dsrdtr")
            with Horizontal(classes="controls"):
                yield Button("Attach", id="serial-attach", variant="success")
                yield Button("Detach", id="serial-detach", variant="error")
                yield Button("Refresh", id="serial-refresh")

    def initialize(self) -> None:
        """Add columns then do the first refresh.

        Called from the app's ``on_mount`` (not the tab's) so the whole DOM --
        including the shared status bar the refresh updates -- already exists,
        which keeps column-setup strictly before the first ``add_row``.
        """
        table = self.query_one("#serial-table", DataTable)
        table.add_columns("Device", "Board", "VID:PID", "Endpoint", "Status")
        table.border_title = "Detected serial ports"
        self.refresh_table()

    # -- table rendering (UI thread only) -----------------------------------

    def _endpoints(self):
        """device -> displayed WSL endpoint for live serial bridges.

        Shows the auto-detected ``/dev`` alias when the user has created one for
        the bridge's ``/tmp`` endpoint, otherwise the ``/tmp`` endpoint itself.
        """
        return {b["key"]: (b["dev_alias"] or b["endpoint"])
                for b in self.app.manager.list_bridges()
                if b["kind"] == "serial" and b["endpoint"]}

    @work(thread=True, exclusive=True, group="serial-ports")
    def refresh_table_worker(self) -> None:
        """Enumerate ports off the UI thread, then rebuild the table on it.

        ``serial.tools.list_ports.comports()`` queries the Windows SetupAPI and
        can block for hundreds of milliseconds; running it on the periodic timer
        (UI thread) made the dashboard hitch/freeze. The worker does only the
        blocking enumeration and marshals the rows back for rendering. A
        transient enumeration error is swallowed -- the next tick retries.
        """
        try:
            ports = collect_ports()
        except Exception:
            return
        self.app.call_from_thread(self.refresh_table, ports)

    def refresh_table(self, ports=None) -> None:
        manager = self.app.manager
        if ports is None:
            ports = collect_ports()
        endpoints = self._endpoints()
        rows = [(row["device"], row["board"], row["vid_pid"] or "-",
                 endpoints.get(row["device"], "-"),
                 manager.is_attached("serial", row["device"]))
                for row in ports]
        signature = tuple(rows)
        if signature == self._signature:
            return
        self._signature = signature

        table = self.query_one("#serial-table", DataTable)
        previously_selected = self.selected_device()
        table.clear()
        self._rows = []
        for device, board, vid_pid, endpoint, attached in rows:
            table.add_row(device, board, vid_pid, endpoint,
                          self._status_cell(attached))
            self._rows.append(device)
        if not self._rows:
            table.add_row("(no serial ports found)", "-", "-", "-",
                          Text("idle", style="dim"))
        self._restore_cursor(table, self._rows, previously_selected)
        self.app.update_summary()

    def selected_device(self):
        table = self.query_one("#serial-table", DataTable)
        index = table.cursor_row
        if index is None or index < 0 or index >= len(self._rows):
            return None
        return self._rows[index]

    # -- attach / detach ----------------------------------------------------

    def attach(self) -> None:
        device = self.selected_device()
        if device is None:
            self.app.emit_notice("No serial port selected.", "com2tty", "warning")
            return
        try:
            options = self.read_options()
        except ValueError as exc:
            self.app.emit_notice(str(exc), "com2tty", "warning")
            return
        try:
            bridge_id = self.app.manager.start_serial_bridge(
                device, distro=self.app.distro, **options)
        except ValueError as exc:
            self.app.emit_notice(str(exc), "com2tty", "warning")
            return
        self.refresh_table()
        # The action-required half of the suppressed startup banner.
        self.app.emit_notice(
            "In WSL, open a new terminal or run `source ~/.bashrc` "
            "(or ~/.zshrc) to load the PlatformIO environment.",
            f"{device} attached", "information", 12.0,
        )
        # Optional guidance: if the user asked, show the one-time command that
        # aliases the device under /dev. The device is served at the /tmp
        # endpoint; the /dev name is the user's to choose (the dashboard
        # auto-detects whatever they create -- see poll_dev_aliases), so this
        # just pre-fills the /tmp basename as a sensible default.
        if self.query_one("#serial-dev", Checkbox).value:
            from com2tty.windows.dashboard._remediation import serial_dev_link
            endpoint = (self.app.manager.get(bridge_id) or {}).get("endpoint")
            if endpoint:
                name = endpoint.rsplit("/", 1)[-1]
                self.app.show_command_help(
                    *serial_dev_link(endpoint, "/dev/" + name))

    def read_options(self):
        """Collect every serial form field into start_serial_bridge kwargs.

        The "Show /dev link command" checkbox is read separately in ``attach``
        (it drives a UI hint, not a bridge option), so it is not included here.
        """
        baud = self.query_one("#serial-baud", Input).value.strip() or "auto"
        return dict(
            baud=baud,
            board=self.query_one("#serial-board", Select).value,
            bytesize=self.query_one("#serial-bytesize", Select).value,
            parity=self.query_one("#serial-parity", Select).value,
            stopbits=self.query_one("#serial-stopbits", Select).value,
            xonxoff=self.query_one("#serial-xonxoff", Checkbox).value,
            rtscts=self.query_one("#serial-rtscts", Checkbox).value,
            dsrdtr=self.query_one("#serial-dsrdtr", Checkbox).value,
            auto_respawn=self.query_one("#serial-respawn", Checkbox).value,
            wsl_path=self.query_one("#serial-wslpath", Input).value.strip() or None,
        )

    @work(thread=True, group="serial-dev-poll", exclusive=True)
    def poll_dev_aliases(self) -> None:
        """Auto-detect a ``/dev`` alias for each serial bridge's ``/tmp`` endpoint.

        Run from the periodic refresh: scan ``/dev`` inside WSL for a symlink the
        user created pointing at each bridge's ``/tmp`` endpoint, and record it
        so the Endpoint column shows the ``/dev`` path (reverting to ``/tmp`` if
        the alias is removed). This needs no configuration -- the user can run
        ``sudo ln -sf /tmp/ttyUSB0 /dev/<anyname>`` and the dashboard finds it.
        """
        from com2tty.windows.doctor import find_dev_aliases

        manager = self.app.manager
        serial = [b for b in manager.list_bridges() if b["kind"] == "serial"]
        if not serial:
            return
        aliases = find_dev_aliases(self.app.distro,
                                   [b["endpoint"] for b in serial])
        changed = False
        for bridge in serial:
            dev = aliases.get(bridge["endpoint"]) or None
            if manager.set_dev_alias(bridge["bridge_id"], dev):
                changed = True
        if changed:
            self.app.call_from_thread(self.refresh_table)

    @work(thread=True, group="detach")
    def detach(self) -> None:
        app = self.app
        device = app.call_from_thread(self.selected_device)
        if device is None:
            app.call_from_thread(app.emit_notice, "No serial port selected.",
                                 "com2tty", "warning")
            return
        if not app.manager.is_attached("serial", device):
            app.call_from_thread(app.emit_notice, f"{device} is not attached.",
                                 "com2tty", "information")
            return
        app.manager.stop_bridge(f"serial:{device}")
        app.call_from_thread(self.refresh_table)
        app.call_from_thread(app.emit_notice, f"{device} detached.",
                             "com2tty", "information")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "serial-attach":
            self.attach()
            event.stop()
        elif button_id == "serial-detach":
            self.detach()
            event.stop()
        elif button_id == "serial-refresh":
            self.refresh_table()
            event.stop()


class GamepadTab(_DeviceTab):
    """Gamepad pane: controller slots, attach options, attach/detach."""

    def __init__(self):
        super().__init__()
        self._signature = None

    def compose(self) -> ComposeResult:
        yield Static("Select a controller slot, tweak options, then Attach. "
                     "Watch the log / toasts for any one-time WSL setup steps.",
                     classes="hint")
        yield DataTable(id="gamepad-table", cursor_type="row",
                        zebra_stripes=True, classes="tab-table")
        with Vertical(classes="actionbar"):
            with Horizontal(classes="controls"):
                yield Label("Poll Hz")
                yield Input(value="250", compact=True, id="gamepad-poll")
                yield Label("Name")
                yield Input(value="Microsoft X-Box 360 pad", compact=True,
                            id="gamepad-name")
            with Horizontal(classes="controls"):
                yield Checkbox("uinput", value=False, compact=True,
                               id="gamepad-uinput")
                yield Checkbox("Auto-respawn", value=False, compact=True,
                               id="gamepad-respawn")
            with Collapsible(title="Advanced gamepad settings", collapsed=True):
                with Horizontal(classes="controls"):
                    yield Label("WSL FIFO")
                    yield Input(placeholder="(auto: /tmp/com2padN)",
                                compact=True, id="gamepad-wslpad")
            with Horizontal(classes="controls"):
                yield Button("Attach", id="gamepad-attach", variant="success")
                yield Button("Detach", id="gamepad-detach", variant="error")

    def initialize(self) -> None:
        """Add columns then do the first refresh (see ``SerialTab.initialize``)."""
        table = self.query_one("#gamepad-table", DataTable)
        table.add_columns("Slot", "Endpoint", "Status")
        table.border_title = "Controller slots"
        self.refresh_table()

    # -- table rendering (UI thread only) -----------------------------------

    def refresh_table(self) -> None:
        manager = self.app.manager
        endpoints = {b["key"]: b["endpoint"] for b in manager.list_bridges()
                     if b["kind"] == "gamepad" and b["endpoint"]}
        rows = [(slot, endpoints.get(str(slot), "-"),
                 manager.is_attached("gamepad", slot))
                for slot in PAD_SLOTS]
        signature = tuple(rows)
        if signature == self._signature:
            return
        self._signature = signature

        table = self.query_one("#gamepad-table", DataTable)
        previously_selected = self.selected_slot()
        table.clear()
        for slot, endpoint, attached in rows:
            table.add_row(str(slot), endpoint, self._status_cell(attached))
        self._restore_cursor(table, [str(s) for s in PAD_SLOTS],
                             None if previously_selected is None
                             else str(previously_selected))
        self.app.update_summary()

    def selected_slot(self):
        table = self.query_one("#gamepad-table", DataTable)
        index = table.cursor_row
        if index is None or index < 0 or index >= len(PAD_SLOTS):
            return None
        return PAD_SLOTS[index]

    # -- attach / detach ----------------------------------------------------

    def attach(self) -> None:
        slot = self.selected_slot()
        if slot is None:
            self.app.emit_notice("No gamepad slot selected.", "com2tty", "warning")
            return
        try:
            poll_hz = int(self.query_one("#gamepad-poll", Input).value.strip())
        except ValueError:
            self.app.emit_notice("Poll Hz must be an integer.", "com2tty",
                                 "warning")
            return
        name = self.query_one("#gamepad-name", Input).value.strip() \
            or "Microsoft X-Box 360 pad"
        use_uinput = self.query_one("#gamepad-uinput", Checkbox).value
        respawn = self.query_one("#gamepad-respawn", Checkbox).value
        tmp_path = self.query_one("#gamepad-wslpad", Input).value.strip() or None
        try:
            self.app.manager.start_gamepad_bridge(
                slot, poll_hz=poll_hz, name=name, use_uinput=use_uinput,
                tmp_path=tmp_path, distro=self.app.distro, auto_respawn=respawn,
            )
        except ValueError as exc:
            self.app.emit_notice(str(exc), "com2tty", "warning")
            return
        self.refresh_table()
        fallback = tmp_path or f"/tmp/com2pad{slot}"
        if use_uinput:
            message = ("uinput mode: creating a real /dev/input device. If "
                       f"/dev/uinput is not writable it falls back to {fallback}.")
            # Probe the permission so an insufficient one becomes a clear
            # warning plus a copy-pasteable command modal, and the endpoint
            # column reflects the real (fallback) sink -- not buried in the log.
            self._check_uinput_worker(slot, fallback)
        else:
            message = (f"WSL endpoint: {fallback} (root-free evdev stream). "
                       "Point your WSL app at this FIFO.")
        self.app.emit_notice(message, f"Gamepad slot {slot} attached",
                             "information", 12.0)

    @work(thread=True, group="uinput", exclusive=True)
    def _check_uinput_worker(self, slot, fallback) -> None:
        from com2tty.windows.doctor import WARN, check_uinput

        status, _, detail = check_uinput(self.app.distro)
        if status == WARN:
            # The bridge will fall back to the /tmp stream: correct the endpoint
            # column away from the optimistic uinput label, surface the one-time
            # root setup as copy-pasteable commands, and explain in a toast.
            self.app.manager.set_endpoint(f"gamepad:{slot}", fallback)
            self.app.call_from_thread(self.refresh_table)
            self.app.call_from_thread(self.app.show_command_help, *UINPUT_SETUP)
            self.app.call_from_thread(
                self.app.emit_notice,
                f"Gamepad slot {slot}: insufficient /dev/uinput permission -- "
                f"{detail} Using the /tmp stream for now. After the one-time "
                "root setup (commands shown), DETACH AND RE-ATTACH this gamepad "
                "for uinput to take effect.",
                "com2tty", "warning", 16.0,
            )

    @work(thread=True, group="detach")
    def detach(self) -> None:
        app = self.app
        slot = app.call_from_thread(self.selected_slot)
        if slot is None:
            app.call_from_thread(app.emit_notice, "No gamepad slot selected.",
                                 "com2tty", "warning")
            return
        if not app.manager.is_attached("gamepad", slot):
            app.call_from_thread(app.emit_notice,
                                 f"Gamepad slot {slot} is not attached.",
                                 "com2tty", "information")
            return
        app.manager.stop_bridge(f"gamepad:{slot}")
        app.call_from_thread(self.refresh_table)
        app.call_from_thread(app.emit_notice, f"Gamepad slot {slot} detached.",
                             "com2tty", "information")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "gamepad-attach":
            self.attach()
            event.stop()
        elif button_id == "gamepad-detach":
            self.detach()
            event.stop()


class DoctorTab(Vertical):
    """System-doctor pane: run the environment self-check and show results.

    A vertical container for the same reason as ``_DeviceTab`` -- so it fills
    its ``TabPane`` and the result table is given height.
    """

    def __init__(self):
        super().__init__()
        # Remediation topics from the last run that have a copy-pasteable fix.
        self._remediations = []

    def compose(self) -> ComposeResult:
        yield Static("Checks the WSL/Python/uinput environment com2tty needs.",
                     classes="hint")
        with Horizontal(classes="controls"):
            yield Button("Run environment checks", id="doctor-run",
                         variant="primary")
            yield Button("Setup commands", id="doctor-fix")
        yield Static("", id="doctor-summary")
        yield DataTable(id="doctor-table", cursor_type="row",
                        zebra_stripes=True, classes="tab-table")

    def initialize(self) -> None:
        """Add the result-table columns (no initial run)."""
        table = self.query_one("#doctor-table", DataTable)
        table.add_columns("Check", "Status", "Detail")
        table.border_title = "Environment checks"

    @work(thread=True, group="doctor", exclusive=True)
    def run(self) -> None:
        from com2tty.windows.doctor import collect_doctor_results

        app = self.app
        app.call_from_thread(
            self.query_one("#doctor-summary", Static).update,
            Text("Running checks (WSL probes can take a few seconds)...",
                 style="italic"),
        )
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                results = collect_doctor_results(distro=app.distro,
                                                 rfc2217_port=app.rfc2217_port)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the UI
            app.call_from_thread(
                self.query_one("#doctor-summary", Static).update,
                Text(f"Doctor failed: {exc}", style="bold red"),
            )
            return
        app.call_from_thread(self.render_results, results)

    def render_results(self, results) -> None:
        from com2tty.windows.doctor import FAIL, WARN

        from com2tty.windows.dashboard._remediation import remediation_for_results

        table = self.query_one("#doctor-table", DataTable)
        table.clear()
        for status, label, detail in results:
            table.add_row(
                label,
                Text(status, style=_STATUS_STYLES.get(status, "")),
                detail or "-",
            )
        failed = sum(1 for status, _, _ in results if status == FAIL)
        warned = sum(1 for status, _, _ in results if status == WARN)
        if failed:
            summary = Text(f"{failed} failed, {warned} warning(s) — com2tty "
                           "will not work until the failures are fixed.",
                           style="bold red")
        elif warned:
            summary = Text(f"All required checks passed ({warned} warning(s)).",
                           style="yellow")
        else:
            summary = Text("All checks passed.", style="bold green")
        self.query_one("#doctor-summary", Static).update(summary)

        # Surface copy-pasteable fixes for any check that warned/failed and has
        # a known remedy, so the user is pointed at the 'Setup commands' button.
        self._remediations = remediation_for_results(results)
        if self._remediations:
            self.app.emit_notice(
                f"{len(self._remediations)} check(s) have copy-paste setup "
                "commands — press 'Setup commands' to view them.",
                "com2tty", "information", 12.0)

    def show_fixes(self) -> None:
        """Open a copy-pasteable command modal for the last run's remedies."""
        topics = self._remediations
        if not topics:
            self.app.emit_notice("No setup commands needed; run the checks "
                                 "first.", "com2tty", "information")
            return
        if len(topics) == 1:
            self.app.show_command_help(*topics[0])
            return
        # Several remedies: aggregate into one modal, each under a comment.
        commands = []
        for title, _explanation, cmds in topics:
            commands.append(f"# {title}")
            commands.extend(cmds)
            commands.append("")
        self.app.show_command_help(
            "Setup commands",
            "Copy-pasteable fixes for the checks that need attention:",
            commands)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "doctor-run":
            self.run()
            event.stop()
        elif event.button.id == "doctor-fix":
            self.show_fixes()
            event.stop()
