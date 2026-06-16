"""CSS stylesheet for the ``DashboardApp``.

Extracted verbatim from the inline ``CSS`` class attribute so the view module
(``app.py``) stays focused on composition and lifecycle.  The string is
assigned to ``DashboardApp.CSS`` in ``app.py``.
"""

DASHBOARD_CSS = """\
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
