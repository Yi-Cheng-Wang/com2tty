"""Modal screens used by the dashboard.

Extracted from ``app.py`` so each screen class lives in its own module,
following the Textual convention of one screen per file (or at least
separate from the main ``App``).
"""
import copy
from math import ceil

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.style import Style
from textual.widgets import Button, Static, TextArea

from com2tty.windows.dashboard._markdown import render_markdown


class _MarkdownStatic(Static):
    """The rendered README, rendered one visible row at a time.

    Link spans carry an ``@click=link(href)`` action, which Textual routes to
    this widget's ``action_link`` -- so a contents entry jumps to its heading
    and other links are handled in-app, never by shelling out to a browser.

    Why a custom ``render_line``: the README is a single ~800-row ``Content``
    so that a drag-select + Ctrl+C copies one clean, contiguous string (see
    ``ReadmeScreen``/``_markdown``). But Textual's default ``Static`` re-renders
    the *whole* widget on every refresh, and dragging a selection refreshes the
    widget on every mouse move -- so each move re-wrapped and re-styled all 800
    rows (~10 ms each), which made highlighting crawl. Here the document is
    wrapped to visual rows just once per width (the expensive step), the
    un-selected strip of each row is cached, and ``render_line`` styles only the
    rows the selection actually touches. The compositor only asks for visible
    rows, so a drag now costs O(visible rows) instead of O(whole document),
    while producing byte-identical output to the full render (selection and
    link styling included)."""

    def __init__(self, content, **kwargs):
        super().__init__(content, **kwargs)
        # The source ``Content``; kept so we can wrap it ourselves. ``_rows`` is
        # the wrapped visual rows, ``_row_strips`` their cached un-selected
        # strips, and ``_wrap_key`` the ``(width, theme)`` those caches hold for
        # -- both are rebuilt only when the width or theme changes, never on the
        # per-move selection refresh.
        self._content_doc = content
        self._rows = None
        self._row_strips = None
        self._wrap_key = None

    def action_link(self, href) -> None:
        self.screen.follow_link(href)

    def _ensure_wrapped(self, width) -> None:
        """Wrap the document to visual rows for ``width`` (cached per width/theme).

        This is the costly pass (string wrapping + theme-variable resolution),
        so it must not run on the selection-refresh path -- only when the cache
        key changes (a resize, or a theme switch that changes resolved colours).
        """
        key = (width, self.app.theme)
        if self._wrap_key == key and self._rows is not None:
            return
        # Static defaults: left-aligned, fold overflow, soft-wrap on, no pad.
        self._rows = self._content_doc._wrap_and_format(
            width, align="left", overflow="fold", no_wrap=False,
            line_pad=0, get_style=self._get_style,
        )
        self._row_strips = [None] * len(self._rows)
        self._wrap_key = key

    def render_line(self, y) -> Strip:
        width = self.content_size.width
        if not width:
            return Strip.blank(0)
        self._ensure_wrapped(width)
        if y >= len(self._rows):
            return Strip.blank(self.size.width, self.visual_style.rich_style)

        row = self._rows[y]
        selection = self.text_selection
        span = None if selection is None else selection.get_span(row.y)
        if span is None:
            # No selection on this row: serve (and memoise) its base strip.
            strip = self._row_strips[y]
            if strip is None:
                strip = Strip(*row.to_strip(self.visual_style))
                strip = strip._apply_link_style(self.link_style)
                self._row_strips[y] = strip
            return strip

        # Map the selection's content-line span onto this wrapped row's slice
        # ``[row.x, row.x + len)`` (``end == -1`` means "to end of line").
        start_x, end_x = span
        row_len = len(row.plain)
        lo = max(0, start_x - row.x)
        hi = row_len if end_x == -1 else min(row_len, end_x - row.x)
        content = row.content
        if hi > lo:
            content = content.stylize(self._selection_style(), lo, hi)
        styled = copy.copy(row)
        styled.content = content
        strip = Strip(*styled.to_strip(self.visual_style))
        return strip._apply_link_style(self.link_style)

    def _selection_style(self) -> Style:
        """The theme's text-selection style (the ``screen--selection`` class).

        This must match how the installed Textual's ``Visual.to_strips``
        derives it, or our lazy ``render_line`` drifts from a full render. That
        derivation changed across versions, so we mirror it: newer Textual
        overlays the component's *partial* style (a semi-transparent background,
        transparent foreground) and blends it onto each row at render time;
        older Textual flattened the component to an opaque, pre-blended style.
        We detect which by the foreground the partial style carries -- a
        transparent foreground means the modern overlay model -- and on the
        modern path keep only the background so the text colour shows through.
        """
        partial = Style.from_styles(
            self.screen.get_component_styles("screen--selection"))
        foreground = partial.foreground
        if foreground is not None and foreground.a:
            # Older Textual: the selection style is flattened and pre-blended,
            # carrying an opaque foreground -- use it verbatim.
            return Style.from_rich_style(
                self.screen.get_component_rich_style("screen--selection"))
        return Style(background=partial.background)


class ReadmeScreen(ModalScreen):
    """The README rendered in-terminal by the dashboard's own Markdown renderer.

    The document is one styled :class:`~textual.content.Content` (from
    :func:`com2tty.windows.dashboard._markdown.render_markdown`) shown in a
    single selectable :class:`_MarkdownStatic`. Keeping it one widget is what
    makes Ctrl+C copy reliable -- selecting a passage and pressing Ctrl+C copies
    it as clean plain text (markup stripped, code verbatim) through the
    in-process Win32 clipboard. Links stay clickable because each link span
    carries an ``@click`` action: a ``#anchor`` (the table of contents) scrolls
    to that heading, a web link opens externally, and a relative path is
    reported but not followed. Ctrl+C is bound on the screen because Textual
    binds it app-wide to a non-copy handler except on ``Input``/``TextArea``.

    Layout: a ``✕`` button in the top-right corner closes the dialog, the
    document scrolls in the middle, and a centred hint sits at the bottom.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("ctrl+c", "copy_selection", "Copy selection"),
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
    #readme-topbar {
        dock: top;
        height: 1;
        align-horizontal: right;
        background: $surface;
    }
    #readme-close {
        width: 5;
        min-width: 5;
        height: 1;
        padding: 0 1;
        border: none;
        background: transparent;
        color: $text-muted;
    }
    #readme-close:hover {
        color: $text;
        background: $boost;
    }
    #readme-scroll {
        height: 1fr;
        background: $surface;
        padding: 0 1;
    }
    #readme-md {
        width: 1fr;
        height: auto;
        background: $surface;
    }
    #readme-hint {
        dock: bottom;
        height: 1;
        width: 1fr;
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self, markdown_text):
        super().__init__()
        self._markdown = markdown_text
        self._anchors = {}

    def compose(self) -> ComposeResult:
        content, self._anchors = render_markdown(self._markdown)
        with Vertical(id="readme-dialog"):
            with Horizontal(id="readme-topbar"):
                yield Button("✕", id="readme-close")
            with VerticalScroll(id="readme-scroll"):
                yield _MarkdownStatic(content, id="readme-md")
            yield Static("Click a contents link to jump · Select text + Ctrl+C "
                         "to copy · Esc / q to close", id="readme-hint")

    def on_mount(self) -> None:
        self.query_one("#readme-dialog", Vertical).border_title = "README"
        # Focus the scroller so the arrow/page keys scroll the document; Ctrl+C
        # still reaches the screen binding regardless of focus.
        self.query_one("#readme-scroll", VerticalScroll).focus()

    def follow_link(self, href) -> None:
        """Resolve a clicked link: jump to an anchor, open a web link, or report.

        A ``#anchor`` scrolls to its heading; an ``http``/``https``/``mailto``
        link opens externally; anything else (a relative path such as
        ``ARCHITECTURE.md``) is reported but not followed, so it can neither
        launch a browser nor trip the OS folder-access protection.
        """
        hint = self.query_one("#readme-hint", Static)
        if href.startswith("#"):
            self._scroll_to_anchor(href[1:], hint)
        elif href.startswith(("http://", "https://", "mailto:")):
            self.app.open_url(href)
        else:
            hint.update("Link to %s -- open it outside the dashboard  ·  "
                        "Esc / q to close" % href)

    def _scroll_to_anchor(self, slug, hint) -> None:
        """Scroll the document so the heading with ``slug`` is at the top.

        The heading's line index (from the renderer) is converted to a visual
        row by counting how many rows each preceding line wraps to at the
        current width, so the jump lands correctly even when lines wrap.
        """
        index = self._anchors.get(slug)
        if index is None:
            hint.update("Section '%s' not found in this document  ·  "
                        "Esc / q to close" % slug)
            return
        scroll = self.query_one("#readme-scroll", VerticalScroll)
        md = self.query_one("#readme-md", _MarkdownStatic)
        width = max(1, md.content_size.width)
        lines = md.render().plain.split("\n")
        rows = sum(max(1, ceil(len(lines[j]) / width))
                   for j in range(min(index, len(lines))))
        scroll.scroll_to(y=rows, animate=False)
        hint.update("Jumped to '%s'  ·  Select text + Ctrl+C to copy  ·  "
                    "Esc / q to close" % slug)

    def action_copy_selection(self) -> None:
        """Copy the current text selection through the Win32 clipboard.

        Feedback goes to the centred footer hint (a fixed-height line), so it
        cannot relayout/flicker the dialog.
        """
        hint = self.query_one("#readme-hint", Static)
        text = self.get_selected_text()
        if not text:
            hint.update("Drag to select a passage first, then Ctrl+C  ·  "
                        "Esc / q to close")
            return
        self.app.set_clipboard_text(text)
        hint.update("✓ Copied %d characters to clipboard  ·  Esc / q to close"
                    % len(text))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "readme-close":
            self.action_close()
            event.stop()

    def action_close(self) -> None:
        self.dismiss()


class CommandHelpScreen(ModalScreen):
    """A small modal of copy-pasteable setup/remediation commands.

    Popped whenever the dashboard detects a permission/setup deficiency (e.g.
    /dev/uinput not writable), or as optional guidance for exposing a serial
    device under /dev. The commands appear each on their own line in a
    read-only, selectable area, and a single button copies them all to the
    clipboard (through ``App.set_clipboard_text`` -- the Win32 clipboard).

    The copy confirmation is an in-dialog status line of fixed height -- not a
    bottom toast -- so pressing Copy does not trigger a global relayout that
    would make the dialog border flicker.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
    ]

    DEFAULT_CSS = """
    CommandHelpScreen {
        align: center middle;
        background: $background 70%;
    }
    #cmd-dialog {
        width: 80%;
        max-width: 92;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        border-title-color: $accent;
        border-title-align: center;
        background: $surface;
    }
    #cmd-explanation {
        width: 1fr;
        height: auto;
        padding: 0 0 1 0;
        color: $text-muted;
    }
    CommandHelpScreen TextArea {
        height: auto;
        max-height: 12;
        background: $boost;
        border: none;
    }
    /* Fixed-height reserved status line: updating its text on copy must not
       change the dialog's height (which would jitter the border). */
    #cmd-status {
        width: 1fr;
        height: 1;
        color: $success;
    }
    #cmd-buttons {
        height: auto;
        align-horizontal: right;
    }
    #cmd-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, title, explanation, commands):
        super().__init__()
        self._title = title
        self._explanation = explanation
        self._commands = list(commands)

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-dialog"):
            yield Static(self._explanation, id="cmd-explanation")
            yield TextArea("\n".join(self._commands), read_only=True,
                           soft_wrap=False, id="cmd-text")
            yield Static("", id="cmd-status")
            with Horizontal(id="cmd-buttons"):
                yield Button("Copy commands", variant="primary", id="cmd-copy")
                yield Button("Close", id="cmd-close")

    def on_mount(self) -> None:
        self.query_one("#cmd-dialog", Vertical).border_title = self._title

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cmd-copy":
            self.app.set_clipboard_text("\n".join(self._commands))
            # In-dialog confirmation only; no toast, so no relayout/flicker.
            self.query_one("#cmd-status", Static).update("✓ Copied to clipboard")
            event.stop()
        elif event.button.id == "cmd-close":
            self.action_close()
            event.stop()

    def action_close(self) -> None:
        self.dismiss()
