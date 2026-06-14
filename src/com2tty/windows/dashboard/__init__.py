"""Interactive dashboard (TUI) mode for com2tty.

The dashboard is the zero-argument default: running ``com2tty`` with no
positional COM port and no other mode flag drops the user into a single
full-screen view that can attach/detach serial and gamepad bridges, run the
environment doctor, and tail a unified log -- without having to remember the
command-line flags.

This package keeps the management core (:mod:`.manager`, the ``BridgeManager``)
free of any TUI dependency so it can be unit-tested without Textual installed.
Only :func:`run_dashboard` reaches for the Textual app, and it degrades to an
actionable message if the optional dependency is missing.
"""
from com2tty.core.constants import DEFAULT_RFC2217_PORT


def run_dashboard(distro=None, rfc2217_port=DEFAULT_RFC2217_PORT, debug=False):
    """Launch the dashboard TUI.

    Returns an exit status (0 on a clean quit, 1 when Textual is not
    available) so the CLI can propagate it to the shell.
    """
    try:
        from com2tty.windows.dashboard.app import DashboardApp
    except ImportError:
        print(
            "The dashboard mode needs the 'textual' package, which could not "
            "be imported.\n"
            "Install it with:  pip install textual\n"
            "Meanwhile the classic command-line modes still work, e.g. "
            "'com2tty COM3', 'com2tty --gamepad', 'com2tty --list', "
            "'com2tty --doctor'."
        )
        return 1

    app = DashboardApp(distro=distro, rfc2217_port=rfc2217_port, debug=debug)
    app.run()
    return 0
