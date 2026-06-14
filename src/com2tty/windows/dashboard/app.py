"""The dashboard Textual application (the View).

Layout, top to bottom:

* a ``Header`` with the project title and a clock;
* a global settings bar to switch the active WSL distribution;
* a ``TabbedContent`` with three panes -- Serial Ports, Gamepads, and a
  System Doctor -- each of which drives the :class:`~.manager.BridgeManager`
  to attach/detach devices or to run the environment self-check;
* a shared, titled ``RichLog`` that tails every ``com2tty`` log record;
* a ``Footer`` exposing the key bindings.

Two things make the TUI safe to host the existing bridge code, which was
written for a plain terminal:

* **No raw output bleed.** ``run_bridge`` / ``run_gamepad_bridge`` print
  colour banners to stdout and the CLI logs to stderr; both would punch holes
  through the Textual screen. On mount we silence the banners
  (:func:`~com2tty.windows.os_hacks.console.set_banners_enabled`) and detach
  the root logger's stream handlers, routing every record into the on-screen
  ``RichLog`` instead.
* **Important messages stand out.** The action-required parts of those
  banners (e.g. "run ``source ~/.bashrc`` in WSL", the gamepad endpoint path)
  are re-surfaced as Textual notifications on attach, any WARNING/ERROR record
  also raises a toast, and an insufficient-permission ``/dev/uinput`` is
  checked up front and warned about -- so the user is not expected to spot
  them in the scrolling log.

The serial/gamepad tables auto-refresh on a timer so unplugged devices and
dropped bridges update without a manual refresh, and the
:class:`~.manager.BridgeManager` auto-allocates a distinct WSL endpoint and
RFC 2217 port per attached serial device (shown in the Endpoint column).
"""
import io
import logging
import threading
from contextlib import redirect_stdout
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    MarkdownViewer,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from com2tty.core.boards import BOARD_CHOICES
from com2tty.windows.dashboard.manager import BridgeManager
from com2tty.windows.discovery import collect_ports, list_wsl_distros
from com2tty.windows.os_hacks.console import set_banners_enabled

PAD_SLOTS = (0, 1, 2, 3)
REFRESH_INTERVAL = 2.0  # seconds between automatic table refreshes
DEFAULT_DISTRO_LABEL = "(default)"
THEME = "tokyo-night"  # cohesive built-in palette; swap for any registered theme

def _local_readme():
    """Path to the repo's README.md when running from a checkout, else None.

    Walks up to the directory that holds ``pyproject.toml`` (the project root)
    and returns its ``README.md``.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            readme = parent / "README.md"
            return readme if readme.is_file() else None
    return None


def _readme_text():
    """The project's README as Markdown text, matching the running version.

    Prefers the checkout's ``README.md`` (always current while developing);
    falls back to the copy embedded in the installed package metadata (the
    long description built from the same README), so an installed wheel shows
    its own README rather than a possibly-mismatched online version. Returns
    None only if neither source is available.
    """
    readme = _local_readme()
    if readme is not None:
        try:
            return readme.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        import importlib.metadata as importlib_metadata
        payload = importlib_metadata.metadata("com2tty").get_payload()
        if payload:
            return payload
    except Exception:  # noqa: BLE001 - metadata may be absent/odd; degrade
        pass
    return None

# Responsive breakpoints (terminal cells). A class is toggled on the Screen at
# each threshold and the CSS reflows accordingly -- the TUI equivalent of CSS
# media queries.
WIDE_WIDTH = 120     # >= : two-column body (tabs beside the log)
NARROW_WIDTH = 76    # <  : drop hints/summary, shrink controls
SHORT_HEIGHT = 22    # <  : drop hints, shrink the log panel

# Dismissable notice strip. Notices auto-clear after these seconds (0 = stay
# until the user closes them with the X); the strip is capped so it can never
# flood the screen.
MAX_NOTICES = 5
NOTICE_TIMEOUT = {"information": 10.0, "warning": 25.0, "error": 0.0}

# Status -> Rich style for the Doctor table cells.
_STATUS_STYLES = {
    "OK": "bold green",
    "WARN": "bold yellow",
    "FAIL": "bold red",
    "SKIP": "dim",
}


class _RichLogHandler(logging.Handler):
    """Funnel ``logging`` records into the dashboard's ``RichLog``.

    Bridge worker threads emit records from outside the Textual event loop, so
    writes are marshalled with ``call_from_thread`` unless we are already on
    the UI thread. WARNING and above are also raised as toast notifications so
    the user does not have to catch them in the scrolling log. Shutdown races
    (loop gone, widget unmounted) are swallowed -- a closing dashboard must
    never crash on a late log line.
    """

    def __init__(self, app: "DashboardApp", rich_log: RichLog):
        super().__init__()
        self._app = app
        self._rich_log = rich_log

    def _on_ui_thread(self):
        return threading.get_ident() == self._app.ui_thread_id

    def _dispatch(self, fn, *args):
        try:
            if self._on_ui_thread():
                fn(*args)
            else:
                self._app.call_from_thread(fn, *args)
        except Exception:  # noqa: BLE001 - app is shutting down / unmounted
            pass

    def emit(self, record):
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - formatting must never crash logging
            return
        style = {
            logging.WARNING: "yellow",
            logging.ERROR: "bold red",
            logging.CRITICAL: "bold red",
        }.get(record.levelno)
        self._dispatch(self._rich_log.write,
                       Text(message, style=style) if style else message)
        # Only errors auto-raise a notice; warnings stay coloured in the log so
        # the notice strip is reserved for things that need the user's eye.
        if record.levelno >= logging.ERROR:
            self._dispatch(self._app.emit_notice, record.getMessage(),
                          "com2tty", "error")


class ReadmeScreen(ModalScreen):
    """The README rendered in-terminal, so no external Markdown viewer is
    needed (and the shown docs always match the running version)."""

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    ReadmeScreen {
        align: center middle;
        background: $background 70%;
    }
    #readme-dialog {
        width: 90%;
        height: 90%;
        max-width: 120;
        border: round $accent;
        border-title-color: $accent;
        border-title-align: center;
        background: $surface;
    }
    ReadmeScreen MarkdownViewer {
        height: 1fr;
        background: $surface;
    }
    #readme-hint {
        dock: bottom;
        width: 1fr;
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self, markdown_text):
        super().__init__()
        self._markdown = markdown_text

    def compose(self) -> ComposeResult:
        with Vertical(id="readme-dialog"):
            # open_links=False: this is an offline reader; don't shell out to a
            # browser for hyperlinks the user may not be able to follow.
            yield MarkdownViewer(self._markdown, show_table_of_contents=True,
                                 open_links=False)
            yield Static("Esc / q to close", id="readme-hint")

    def on_mount(self) -> None:
        self.query_one("#readme-dialog", Vertical).border_title = "README"

    def action_close(self) -> None:
        self.dismiss()


class DashboardApp(App):
    """Single-screen manager for every com2tty bridge."""

    TITLE = "com2tty"
    SUB_TITLE = "Smart Management Dashboard"

    # The layout is responsive (RWD): every region sizes with `fr`/`auto`/`%`,
    # and breakpoint classes toggled on the Screen (-wide / -narrow / -short)
    # reflow it like CSS media queries. Device tables take the flexible space
    # (`1fr`) and scroll internally; each tab's controls dock to the bottom so
    # they are never pushed off-screen however the window is resized.
    CSS = """
    Screen {
        layout: vertical;
    }

    /* -- Status bar: WSL distro picker + live attached-device summary ------ */
    #statusbar {
        height: auto;
        align-vertical: middle;
        padding: 0 1;
        margin: 0 1;
        background: $panel;
    }
    #statusbar Label {
        width: auto;
        padding: 0 1 0 0;
        color: $text-muted;
    }
    /* Flat (compact=True) selector so it matches the in-tab fields. */
    #distro {
        width: 28;
        background: $boost;
    }
    #distro-refresh {
        width: 5;
        min-width: 5;
        margin: 0 0 0 1;
    }
    #attach-summary {
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }

    /* -- Dismissable notices: float bottom-right like toasts -------------- */
    /* Mirrors Textual's ToastRack: an invisible (click-through) rack docked
       bottom-right on the overlay layer; only the notice cards are visible and
       interactive, so the X closes them without blocking the UI underneath. */
    #notices {
        layer: _toastrack;
        dock: bottom;
        align: right bottom;
        visibility: hidden;
        width: 1fr;
        height: auto;
        margin: 0 1 1 0;
    }
    .notice {
        visibility: visible;
        width: 64;
        max-width: 100%;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        background: $panel;
        border-left: thick $accent;
    }
    .notice-warning {
        border-left: thick $warning;
    }
    .notice-error {
        border-left: thick $error;
    }
    .notice-msg {
        width: 1fr;
        height: auto;
        padding: 0 1 0 0;
    }
    .notice-close {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        background: $panel;
        color: $text-muted;
    }
    .notice-close:hover {
        background: $error;
        color: $text;
    }

    /* -- Body: single column by default, two columns when wide ------------- */
    #body {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    .-wide #body {
        layout: horizontal;
    }

    TabbedContent {
        height: 1fr;
    }
    .-wide TabbedContent {
        width: 2fr;
        height: 1fr;
    }

    /* Scroll as a last resort on very short terminals so the table area can
       slide under the docked controls instead of content being lost. */
    TabPane {
        padding: 0 1;
        overflow-y: auto;
    }
    /* The flexible centre of each tab: grows with the window, scrolls its own
       rows when there is not enough room, never collapses below 3 rows. */
    .tab-table {
        height: 1fr;
        min-height: 3;
        margin: 1 0 0 0;
        border: round $primary;
        border-title-color: $accent;
        border-title-align: left;
    }
    /* Pinned to the bottom of the tab so controls stay reachable on resize. */
    .actionbar {
        dock: bottom;
        height: auto;
        padding: 1 0 0 0;
        border-top: solid $primary;
    }
    .actionbar Collapsible {
        border: none;
        padding: 0;
        margin: 0;
    }
    .actionbar CollapsibleTitle {
        padding: 0 1;
        color: $accent;
    }

    /* -- Log panel: below the tabs (vertical) or beside them (wide) -------- */
    #log-panel {
        height: 32%;
        min-height: 4;
        max-height: 16;
        border: round $primary;
        border-title-color: $accent;
        border-title-align: left;
        padding: 0 1;
        margin: 0 1 1 1;
    }
    .-wide #log-panel {
        width: 1fr;
        height: 1fr;
        max-height: 100%;
        margin: 1 1 1 0;
    }
    #log {
        height: 1fr;
        background: $surface;
    }

    /* -- Controls: compact, single-row so the whole action bar fits 80x24 -- */
    .hint {
        width: 1fr;
        height: auto;
        color: $text-muted;
    }
    .controls {
        height: auto;
        align-vertical: middle;
    }
    /* Inline form labels must hug their text (Static defaults to width:1fr and
       would otherwise eat the whole row). */
    .controls Label {
        width: auto;
        height: 1;
        padding: 0 1 0 1;
        color: $text-muted;
    }
    /* Flat fields via the widgets' native compact mode (compact=True); only
       the width is pinned here so the option overlays open normally. */
    .controls Input, .controls Select {
        width: 18;
    }
    .controls Input {
        background: $boost;
    }
    .controls Checkbox {
        width: auto;
        background: transparent;
        margin: 0 2 0 0;
    }
    /* Action buttons keep their full button chrome (height + variant border)
       so they read as clearly-clickable, unlike the flat input fields. */
    .controls Button {
        min-width: 14;
        margin: 1 1 0 0;
    }
    #doctor-summary {
        width: 1fr;
        height: auto;
        padding: 1 0 0 0;
    }

    /* -- Responsive trims: reclaim rows/cols on small terminals ------------ */
    .-short .hint, .-narrow .hint {
        display: none;
    }
    .-short #log-panel {
        min-height: 3;
    }
    .-narrow #attach-summary {
        display: none;
    }
    .-narrow .controls Input, .-narrow .controls Select {
        width: 14;
    }
    """

    BINDINGS = [
        ("r", "refresh", "Refresh ports"),
        ("d", "run_doctor", "Run doctor"),
        ("f1", "open_readme", "Docs"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, distro=None, rfc2217_port=4000, debug=False):
        super().__init__()
        self.distro = distro
        self.rfc2217_port = rfc2217_port
        # NB: Textual's App already owns a read-only ``debug`` property (the
        # --dev flag), so the dashboard's own debug-logging flag is private.
        self._debug_logging = debug
        # The manager owns endpoint/port allocation; seed it with the base
        # RFC 2217 port the user passed so the slots count up from there.
        self.manager = BridgeManager(rfc2217_base=rfc2217_port)
        self.ui_thread_id = threading.get_ident()
        self._serial_rows = []  # row index -> device name
        self._log_handler = None
        self._saved_handlers = []
        self._notices = []  # live dismissable notice widgets
        # Last rendered table signatures; the periodic refresh only rebuilds a
        # table when its signature changes, so it never flickers in place.
        self._serial_signature = None
        self._gamepad_signature = None

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="statusbar"):
            yield Label("WSL distro")
            # Seed the value from the constructor distro so a CLI --distro is
            # honoured (and not reset to the default on mount).
            yield Select(self._distro_options(), value=self.distro or "",
                         allow_blank=False, compact=True, id="distro")
            yield Button("↻", compact=True, id="distro-refresh")
            yield Static("", id="attach-summary")
        yield Vertical(id="notices")
        with Vertical(id="body"):
            with TabbedContent(initial="serial-tab"):
                with TabPane("Serial Ports", id="serial-tab"):
                    yield from self._compose_serial_tab()
                with TabPane("Gamepads", id="gamepad-tab"):
                    yield from self._compose_gamepad_tab()
                with TabPane("Doctor", id="doctor-tab"):
                    yield from self._compose_doctor_tab()
            with Vertical(id="log-panel"):
                yield RichLog(id="log", highlight=True, markup=False, wrap=True)
        yield Footer()

    def _compose_serial_tab(self) -> ComposeResult:
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

    def _compose_gamepad_tab(self) -> ComposeResult:
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

    def _compose_doctor_tab(self) -> ComposeResult:
        yield Static("Checks the WSL/Python/uinput environment com2tty needs.",
                     classes="hint")
        with Horizontal(classes="controls"):
            yield Button("Run environment checks", id="doctor-run",
                         variant="primary")
        yield Static("", id="doctor-summary")
        yield DataTable(id="doctor-table", cursor_type="row",
                        zebra_stripes=True, classes="tab-table")

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.ui_thread_id = threading.get_ident()

        # Cohesive palette; ignore if the named theme is not registered so a
        # Textual version bump can never break startup.
        try:
            self.theme = THEME
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        self._apply_responsive(self.size)

        serial_table = self.query_one("#serial-table", DataTable)
        serial_table.add_columns("Device", "Board", "VID:PID", "Endpoint",
                                 "Status")
        serial_table.border_title = "Detected serial ports"

        gamepad_table = self.query_one("#gamepad-table", DataTable)
        gamepad_table.add_columns("Slot", "Endpoint", "Status")
        gamepad_table.border_title = "Controller slots"

        doctor_table = self.query_one("#doctor-table", DataTable)
        doctor_table.add_columns("Check", "Status", "Detail")
        doctor_table.border_title = "Environment checks"

        self.query_one("#log-panel", Vertical).border_title = "Activity Log"

        # The notice strip is empty (and hidden) until a message arrives.
        self.query_one("#notices", Vertical).display = False

        # Stop the bridges from punching raw text through the Textual screen:
        # silence the stdout banners and divert the root logger (the CLI's
        # stderr handler) entirely into the on-screen log.
        set_banners_enabled(False)
        rich_log = self.query_one("#log", RichLog)
        handler = _RichLogHandler(self, rich_log)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s",
                                               datefmt="%H:%M:%S"))
        handler.setLevel(logging.DEBUG if self._debug_logging else logging.INFO)
        root = logging.getLogger()
        self._saved_handlers = root.handlers[:]
        for existing in self._saved_handlers:
            root.removeHandler(existing)
        root.addHandler(handler)
        self._log_handler = handler

        self.refresh_serial_table()
        self.refresh_gamepad_table()
        self._load_distros_worker()
        # Keep the device tables in step with reality (unplugged ports, bridges
        # that dropped) without the user pressing Refresh.
        self.set_interval(REFRESH_INTERVAL, self._periodic_refresh)
        logging.info("Dashboard ready. Select a device and press Attach.")

    def on_unmount(self) -> None:
        root = logging.getLogger()
        if self._log_handler is not None:
            root.removeHandler(self._log_handler)
            self._log_handler = None
        for existing in self._saved_handlers:
            root.addHandler(existing)
        self._saved_handlers = []
        set_banners_enabled(True)
        # Tear every bridge down cleanly so no daemon thread is left holding a
        # serial handle or a WSL helper after the UI is gone.
        self.manager.stop_all(timeout=3.0)

    # -- table rendering (UI thread only) -----------------------------------

    def _periodic_refresh(self) -> None:
        # NB: must NOT be named ``_auto_refresh`` -- that collides with
        # Textual's internal ``Widget._auto_refresh`` timer slot (None by
        # default), which would shadow this method and register the interval
        # with a null callback (so the tables would never refresh).
        # A transient enumeration error must not kill the recurring timer.
        try:
            self.refresh_serial_table()
            self.refresh_gamepad_table()
        except Exception:  # noqa: BLE001 - best-effort periodic refresh
            logging.debug("Periodic refresh failed", exc_info=True)

    def _serial_endpoints(self):
        """device -> allocated WSL endpoint, for live serial bridges."""
        return {b["key"]: b["endpoint"] for b in self.manager.list_bridges()
                if b["kind"] == "serial" and b["endpoint"]}

    def refresh_serial_table(self) -> None:
        endpoints = self._serial_endpoints()
        rows = [(row["device"], row["board"], row["vid_pid"] or "-",
                 endpoints.get(row["device"], "-"),
                 self.manager.is_attached("serial", row["device"]))
                for row in collect_ports()]
        # Skip the rebuild when nothing changed so the periodic refresh does
        # not flicker the table or fight the user's cursor.
        signature = tuple(rows)
        if signature == self._serial_signature:
            return
        self._serial_signature = signature

        table = self.query_one("#serial-table", DataTable)
        previously_selected = self._selected_device()
        table.clear()
        self._serial_rows = []
        for device, board, vid_pid, endpoint, attached in rows:
            table.add_row(device, board, vid_pid, endpoint,
                          self._status_cell(attached))
            self._serial_rows.append(device)
        if not self._serial_rows:
            table.add_row("(no serial ports found)", "-", "-", "-",
                          Text("idle", style="dim"))
        self._restore_cursor(table, self._serial_rows, previously_selected)
        self._update_summary()

    def refresh_gamepad_table(self) -> None:
        endpoints = {b["key"]: b["endpoint"] for b in self.manager.list_bridges()
                     if b["kind"] == "gamepad" and b["endpoint"]}
        rows = [(slot, endpoints.get(str(slot), "-"),
                 self.manager.is_attached("gamepad", slot))
                for slot in PAD_SLOTS]
        signature = tuple(rows)
        if signature == self._gamepad_signature:
            return
        self._gamepad_signature = signature

        table = self.query_one("#gamepad-table", DataTable)
        previously_selected = self._selected_slot()
        table.clear()
        for slot, endpoint, attached in rows:
            table.add_row(str(slot), endpoint, self._status_cell(attached))
        self._restore_cursor(table, [str(s) for s in PAD_SLOTS],
                             None if previously_selected is None
                             else str(previously_selected))
        self._update_summary()

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

    def _update_summary(self) -> None:
        """Render the live attached-device tally in the status bar."""
        counts = self.manager.attached_counts()
        serial = counts.get("serial", 0)
        gamepad = counts.get("gamepad", 0)
        if not serial and not gamepad:
            summary = Text("no devices attached", style="dim")
        else:
            parts = []
            if serial:
                parts.append(Text(f"● {serial} serial", style="green"))
            if gamepad:
                parts.append(Text(f"● {gamepad} pad", style="green"))
            summary = Text("attached: ", style="dim") + Text("  ").join(parts)
        self.query_one("#attach-summary", Static).update(summary)

    # -- responsive layout (RWD) --------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive(event.size)

    def _apply_responsive(self, size) -> None:
        """Toggle breakpoint classes on the Screen so the CSS can reflow.

        ``set_class`` is idempotent, so calling this on every resize event is
        cheap and only triggers a relayout when a threshold is actually
        crossed.
        """
        width, height = size.width, size.height
        screen = self.screen
        screen.set_class(width >= WIDE_WIDTH, "-wide")
        screen.set_class(width < NARROW_WIDTH, "-narrow")
        screen.set_class(height < SHORT_HEIGHT, "-short")

    def emit_notice(self, message, title="com2tty", severity="information",
                    timeout=None):
        """Show a message in the dismissable notice strip.

        Positional-only forwarding keeps cross-thread calls simple (this is
        relayed through ``call_from_thread`` from worker threads, which only
        passes positional args). Notices stay until the user closes them with
        the X, or auto-clear after a severity-dependent delay.
        """
        # Dedupe: an identical message already on screen just stays.
        for existing in self._notices:
            if getattr(existing, "notice_message", None) == message:
                return

        text = Text()
        if title and title != "com2tty":
            text.append(f"{title}  ", style="bold")
        text.append(message)
        close = Button("✕", classes="notice-close")
        row = Horizontal(Static(text, classes="notice-msg"), close,
                         classes=f"notice notice-{severity}")
        row.notice_message = message

        container = self.query_one("#notices", Vertical)
        container.mount(row)
        self._notices.append(row)
        # Bound the strip so it can never flood the screen.
        while len(self._notices) > MAX_NOTICES:
            self._dismiss_notice(self._notices[0])
        container.display = True

        delay = NOTICE_TIMEOUT.get(severity, 10.0) if timeout is None else timeout
        if delay:
            self.set_timer(delay, lambda r=row: self._dismiss_notice(r))

    def _dismiss_notice(self, row) -> None:
        if row in self._notices:
            self._notices.remove(row)
            row.remove()
        if not self._notices:
            self.query_one("#notices", Vertical).display = False

    # -- actions / bindings -------------------------------------------------

    def action_refresh(self) -> None:
        self.refresh_serial_table()

    def action_run_doctor(self) -> None:
        self._run_doctor_worker()

    def action_open_readme(self) -> None:
        """Render the README inside the terminal (no external viewer)."""
        text = _readme_text()
        if not text:
            self.emit_notice("README is not available in this installation.",
                             "Docs", "warning")
            return
        self.push_screen(ReadmeScreen(text))

    # -- event handlers -----------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "distro":
            # "" is the "(default)" sentinel -> use the WSL default distro.
            self.distro = event.value or None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("notice-close"):
            self._dismiss_notice(event.button.parent)
            return
        button_id = event.button.id
        if button_id == "serial-attach":
            self._attach_serial()
        elif button_id == "serial-detach":
            self._detach_serial()
        elif button_id == "serial-refresh":
            self.refresh_serial_table()
        elif button_id == "gamepad-attach":
            self._attach_gamepad()
        elif button_id == "gamepad-detach":
            self._detach_gamepad()
        elif button_id == "doctor-run":
            self._run_doctor_worker()
        elif button_id == "distro-refresh":
            self._load_distros_worker()

    # -- WSL distro switching -----------------------------------------------

    @work(thread=True, group="distros", exclusive=True)
    def _load_distros_worker(self) -> None:
        distros = list_wsl_distros()
        self.call_from_thread(self._populate_distros, distros)

    def _distro_options(self, distros=()):
        """Build the distro Select options: (default) + any current/found ones.

        Keeps a constructor-supplied distro selectable even when enumeration
        missed it (e.g. WSL was slow to answer).
        """
        options = [(DEFAULT_DISTRO_LABEL, "")]
        if self.distro and self.distro not in distros:
            options.append((self.distro, self.distro))
        options.extend((name, name) for name in distros)
        return options

    def _populate_distros(self, distros) -> None:
        options = self._distro_options(distros)
        select = self.query_one("#distro", Select)
        current = self.distro or ""
        select.set_options(options)
        # Preserve the active selection across a re-enumeration.
        if any(value == current for _, value in options):
            select.value = current
        if distros:
            logging.info("WSL distributions: %s", ", ".join(distros))

    # -- serial actions -----------------------------------------------------

    def _selected_device(self):
        table = self.query_one("#serial-table", DataTable)
        index = table.cursor_row
        if index is None or index < 0 or index >= len(self._serial_rows):
            return None
        return self._serial_rows[index]

    def _attach_serial(self) -> None:
        device = self._selected_device()
        if device is None:
            self.emit_notice("No serial port selected.", "com2tty", "warning")
            return
        try:
            options = self._read_serial_options()
        except ValueError as exc:
            self.emit_notice(str(exc), "com2tty", "warning")
            return
        try:
            self.manager.start_serial_bridge(device, distro=self.distro,
                                             **options)
        except ValueError as exc:
            self.emit_notice(str(exc), "com2tty", "warning")
            return
        self.refresh_serial_table()
        # The action-required half of the suppressed startup banner.
        self.emit_notice(
            "In WSL, open a new terminal or run `source ~/.bashrc` "
            "(or ~/.zshrc) to load the PlatformIO environment.",
            f"{device} attached", "information", 12.0,
        )

    def _read_serial_options(self):
        """Collect every serial form field into start_serial_bridge kwargs."""
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
        )

    @work(thread=True, group="detach")
    def _detach_serial(self) -> None:
        device = self.call_from_thread(self._selected_device)
        if device is None:
            self.call_from_thread(self.emit_notice, "No serial port selected.",
                                  "com2tty", "warning")
            return
        if not self.manager.is_attached("serial", device):
            self.call_from_thread(self.emit_notice, f"{device} is not attached.",
                                  "com2tty", "information")
            return
        self.manager.stop_bridge(f"serial:{device}")
        self.call_from_thread(self.refresh_serial_table)
        self.call_from_thread(self.emit_notice, f"{device} detached.",
                              "com2tty", "information")

    # -- gamepad actions ----------------------------------------------------

    def _selected_slot(self):
        table = self.query_one("#gamepad-table", DataTable)
        index = table.cursor_row
        if index is None or index < 0 or index >= len(PAD_SLOTS):
            return None
        return PAD_SLOTS[index]

    def _attach_gamepad(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            self.emit_notice("No gamepad slot selected.", "com2tty", "warning")
            return
        try:
            poll_hz = int(self.query_one("#gamepad-poll", Input).value.strip())
        except ValueError:
            self.emit_notice("Poll Hz must be an integer.", "com2tty", "warning")
            return
        name = self.query_one("#gamepad-name", Input).value.strip() \
            or "Microsoft X-Box 360 pad"
        use_uinput = self.query_one("#gamepad-uinput", Checkbox).value
        respawn = self.query_one("#gamepad-respawn", Checkbox).value
        tmp_path = self.query_one("#gamepad-wslpad", Input).value.strip() or None
        try:
            self.manager.start_gamepad_bridge(
                slot, poll_hz=poll_hz, name=name, use_uinput=use_uinput,
                tmp_path=tmp_path, distro=self.distro, auto_respawn=respawn,
            )
        except ValueError as exc:
            self.emit_notice(str(exc), "com2tty", "warning")
            return
        self.refresh_gamepad_table()
        endpoint = tmp_path or f"/tmp/com2pad{slot}"
        if use_uinput:
            message = ("uinput mode: a real /dev/input device needs a writable "
                       f"/dev/uinput. If it is not, it falls back to {endpoint}.")
            # Probe the permission so an insufficient one is a clear warning,
            # not something buried in the log.
            self._check_uinput_worker(slot)
        else:
            message = (f"WSL endpoint: {endpoint} (root-free evdev stream). "
                       "Point your WSL app at this FIFO.")
        self.emit_notice(message, f"Gamepad slot {slot} attached",
                         "information", 12.0)

    @work(thread=True, group="uinput", exclusive=True)
    def _check_uinput_worker(self, slot) -> None:
        from com2tty.windows.doctor import WARN, check_uinput

        status, _, detail = check_uinput(self.distro)
        if status == WARN:
            self.call_from_thread(
                self.emit_notice,
                f"Gamepad slot {slot}: insufficient /dev/uinput permission -- "
                f"{detail} Falling back to the /tmp stream until the one-time "
                "root setup is done (see README).",
                "com2tty", "warning", 14.0,
            )

    @work(thread=True, group="detach")
    def _detach_gamepad(self) -> None:
        slot = self.call_from_thread(self._selected_slot)
        if slot is None:
            self.call_from_thread(self.emit_notice, "No gamepad slot selected.",
                                  "com2tty", "warning")
            return
        if not self.manager.is_attached("gamepad", slot):
            self.call_from_thread(self.emit_notice,
                                  f"Gamepad slot {slot} is not attached.",
                                  "com2tty", "information")
            return
        self.manager.stop_bridge(f"gamepad:{slot}")
        self.call_from_thread(self.refresh_gamepad_table)
        self.call_from_thread(self.emit_notice, f"Gamepad slot {slot} detached.",
                              "com2tty", "information")

    # -- doctor -------------------------------------------------------------

    @work(thread=True, group="doctor", exclusive=True)
    def _run_doctor_worker(self) -> None:
        from com2tty.windows.doctor import collect_doctor_results

        self.call_from_thread(
            self.query_one("#doctor-summary", Static).update,
            Text("Running checks (WSL probes can take a few seconds)...",
                 style="italic"),
        )
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                results = collect_doctor_results(distro=self.distro,
                                                 rfc2217_port=self.rfc2217_port)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the UI
            self.call_from_thread(
                self.query_one("#doctor-summary", Static).update,
                Text(f"Doctor failed: {exc}", style="bold red"),
            )
            return
        self.call_from_thread(self._render_doctor_results, results)

    def _render_doctor_results(self, results) -> None:
        from com2tty.windows.doctor import FAIL, WARN

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
