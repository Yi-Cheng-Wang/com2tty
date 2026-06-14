"""Modal screens used by the dashboard.

Extracted from ``app.py`` so each screen class lives in its own module,
following the Textual convention of one screen per file (or at least
separate from the main ``App``).
"""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import MarkdownViewer, Static


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
