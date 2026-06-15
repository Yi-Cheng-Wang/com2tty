"""The dashboard Textual application (the View).

Layout, top to bottom:

* a ``Header`` with the project title and a clock;
* a global settings bar to switch the active WSL distribution;
* a ``TabbedContent`` with three self-contained tab widgets -- a
  :class:`~._tabs.SerialTab`, a :class:`~._tabs.GamepadTab`, and a
  :class:`~._tabs.DoctorTab` -- each of which owns its own table, form, and
  attach/detach/run logic and drives the :class:`~.manager.BridgeManager`;
* a shared, titled ``RichLog`` that tails every ``com2tty`` log record;
* a ``Footer`` exposing the key bindings.

The app itself is deliberately thin: it owns the chrome the tabs share -- the
status-bar device tally, the dismissable notice strip, the WSL-distro switcher,
and the log diversion -- plus lifecycle and responsive layout. It never reaches
into a tab's internals; the tabs reach back to it only for that shared state.

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
import logging
import threading

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Header,
    Footer,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from com2tty.windows.dashboard._constants import (
    DEFAULT_DISTRO_LABEL,
    MAX_NOTICES,
    NOTICE_TIMEOUT,
    REFRESH_INTERVAL,
    THEME,
    WIDE_WIDTH,
    NARROW_WIDTH,
    SHORT_HEIGHT,
    _readme_text,
)
from com2tty.windows.dashboard._log_handler import _RichLogHandler
from com2tty.windows.dashboard._screens import CommandHelpScreen, ReadmeScreen
from com2tty.windows.dashboard._styles import DASHBOARD_CSS
from com2tty.windows.dashboard._tabs import DoctorTab, GamepadTab, SerialTab
from com2tty.windows.dashboard.manager import BridgeManager
from com2tty.windows.discovery import list_wsl_distros
from com2tty.windows.os_hacks.console import set_banners_enabled


class DashboardApp(App):
    """Single-screen manager for every com2tty bridge."""

    TITLE = "com2tty"
    SUB_TITLE = "Smart Management Dashboard"

    CSS = DASHBOARD_CSS

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
        self._log_handler = None
        self._saved_handlers = []
        self._notices = []  # live dismissable notice widgets

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
                    yield SerialTab()
                with TabPane("Gamepads", id="gamepad-tab"):
                    yield GamepadTab()
                with TabPane("Doctor", id="doctor-tab"):
                    yield DoctorTab()
            with Vertical(id="log-panel"):
                yield RichLog(id="log", highlight=True, markup=False, wrap=True)
        yield Footer()

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

        # Build each tab's table and do its first refresh here -- after the
        # whole DOM (including the shared status bar) exists -- so the tabs
        # never race ahead of the chrome they update.
        self.query_one(SerialTab).initialize()
        self.query_one(GamepadTab).initialize()
        self.query_one(DoctorTab).initialize()

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

    # -- periodic refresh / summary (UI thread only) ------------------------

    def _periodic_refresh(self) -> None:
        # NB: must NOT be named ``_auto_refresh`` -- that collides with
        # Textual's internal ``Widget._auto_refresh`` timer slot (None by
        # default), which would shadow this method and register the interval
        # with a null callback (so the tables would never refresh).
        # A transient enumeration error must not kill the recurring timer.
        # Serial port enumeration blocks (Windows SetupAPI), so it runs off the
        # UI thread; the gamepad table is pure in-memory state and stays sync.
        try:
            serial = self.query_one(SerialTab)
            serial.refresh_table_worker()
            # Auto-detect any /dev alias the user created for a bridge's /tmp
            # endpoint so the Endpoint column tracks it appearing/disappearing
            # without a re-attach (and without any pre-configuration).
            serial.poll_dev_aliases()
            self.query_one(GamepadTab).refresh_table()
        except Exception:  # noqa: BLE001 - best-effort periodic refresh
            logging.debug("Periodic refresh failed", exc_info=True)

    def update_summary(self) -> None:
        """Render the live attached-device tally in the status bar.

        Public because it is a cross-component API: the tab widgets call it
        (like ``emit_notice``) after they change what is attached.
        """
        target = self.query_one("#attach-summary", Static)
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
        target.update(summary)

    # -- notice system ------------------------------------------------------

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

    # -- actions / bindings -------------------------------------------------

    def action_refresh(self) -> None:
        # Switch to the Serial Ports tab so the refreshed table is actually
        # visible, then refresh it (the binding is useless if the result is on
        # a tab the user is not looking at).
        self.query_one(TabbedContent).active = "serial-tab"
        self.query_one(SerialTab).refresh_table()

    def action_run_doctor(self) -> None:
        # Switch to the Doctor tab so the run's results are on screen.
        self.query_one(TabbedContent).active = "doctor-tab"
        self.query_one(DoctorTab).run()

    def action_open_readme(self) -> None:
        """Render the README inside the terminal (no external viewer)."""
        text = _readme_text()
        if not text:
            self.emit_notice("README is not available in this installation.",
                             "Docs", "warning")
            return
        self.push_screen(ReadmeScreen(text))

    def set_clipboard_text(self, text) -> None:
        """Copy text to the clipboard, robust against terminals ignoring OSC 52.

        Textual's ``copy_to_clipboard`` only emits an OSC 52 escape sequence,
        which many Windows terminals drop. The dashboard is a Windows process,
        so it sets the clipboard directly via the in-process Win32 API
        (``windows/os_hacks/clipboard.py``) and falls back to the OSC 52 path
        only off Windows or if that call fails. That call starts no child
        process, so -- unlike the previous ``clip.exe`` subprocess -- it cannot
        disturb the Windows console mode and freeze/crash the dashboard when
        the user copies (e.g. Ctrl+C in the README).

        The write runs on a worker thread, not the UI thread. Setting the
        clipboard broadcasts a change notification to every clipboard listener
        (third-party clipboard managers, and Windows' own Clipboard History /
        Cloud Clipboard sync), and ``SetClipboardData``/``CloseClipboard``
        block until those listeners respond -- which can take hundreds of
        milliseconds. Doing that on the UI thread is what made copying feel
        laggy: the whole dashboard stalled until the broadcast returned.
        Off-loading it keeps the UI responsive while still using our own
        clipboard implementation.
        """
        self._set_clipboard_text_worker(text)

    @work(thread=True, group="clipboard", exclusive=True)
    def _set_clipboard_text_worker(self, text) -> None:
        """Perform the (potentially blocking) clipboard write off the UI thread.

        ``exclusive`` means a fresh copy supersedes one still draining a slow
        listener chain -- the last copy wins, which is what the user expects.
        """
        from com2tty.windows.os_hacks.clipboard import set_windows_clipboard
        if not set_windows_clipboard(text):
            # OSC 52 writes to the terminal through the app, so it must run on
            # the UI thread; marshal the fallback back there.
            self.call_from_thread(self.copy_to_clipboard, text)

    def show_command_help(self, title, explanation, commands) -> None:
        """Pop a modal of copy-pasteable remediation commands.

        Public so the tabs (e.g. the gamepad uinput-permission path) can
        proactively surface the exact one-time setup commands when they detect
        a deficiency, instead of leaving them in the scrolling log.
        """
        self.push_screen(CommandHelpScreen(title, explanation, commands))

    # -- event handlers -----------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "distro":
            # "" is the "(default)" sentinel -> use the WSL default distro.
            self.distro = event.value or None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # The tabs handle and stop their own buttons; only the app-level
        # chrome buttons (notice close, distro refresh) bubble up to here.
        if event.button.has_class("notice-close"):
            self._dismiss_notice(event.button.parent)
            return
        if event.button.id == "distro-refresh":
            self._load_distros_worker()


# ---------------------------------------------------------------------------
# Re-exports: keep the old import paths working for tests and any external
# consumers. ``_RichLogHandler``, ``ReadmeScreen``, ``_readme_text`` and the
# tab classes are already module attributes (imported and used above); only
# these two are pulled in solely to be re-exported.
# ---------------------------------------------------------------------------
from com2tty.windows.dashboard._constants import (  # noqa: E402,F401
    PAD_SLOTS as PAD_SLOTS,
    _local_readme as _local_readme,
)
