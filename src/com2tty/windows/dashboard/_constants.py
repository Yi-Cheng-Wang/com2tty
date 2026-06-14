"""Module-level constants and helper functions for the dashboard.

Extracted from the monolithic ``app.py`` to keep each module focused.
These are shared across the dashboard's view (``app``), screens, actions,
and logging handler.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Controller / gamepad constants
# ---------------------------------------------------------------------------
PAD_SLOTS = (0, 1, 2, 3)
REFRESH_INTERVAL = 2.0  # seconds between automatic table refreshes
DEFAULT_DISTRO_LABEL = "(default)"
THEME = "tokyo-night"  # cohesive built-in palette; swap for any registered theme

# ---------------------------------------------------------------------------
# Responsive breakpoints (terminal cells).  A class is toggled on the Screen
# at each threshold and the CSS reflows accordingly -- the TUI equivalent of
# CSS media queries.
# ---------------------------------------------------------------------------
WIDE_WIDTH = 120     # >= : two-column body (tabs beside the log)
NARROW_WIDTH = 76    # <  : drop hints/summary, shrink controls
SHORT_HEIGHT = 22    # <  : drop hints, shrink the log panel

# ---------------------------------------------------------------------------
# Dismissable notice strip.  Notices auto-clear after these seconds (0 = stay
# until the user closes them with the X); the strip is capped so it can never
# flood the screen.
# ---------------------------------------------------------------------------
MAX_NOTICES = 5
NOTICE_TIMEOUT = {"information": 10.0, "warning": 25.0, "error": 0.0}

# Status -> Rich style for the Doctor table cells.
_STATUS_STYLES = {
    "OK": "bold green",
    "WARN": "bold yellow",
    "FAIL": "bold red",
    "SKIP": "dim",
}

# ---------------------------------------------------------------------------
# README helpers
# ---------------------------------------------------------------------------

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
