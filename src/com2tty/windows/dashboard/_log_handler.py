"""Logging handler that funnels records into the dashboard's ``RichLog``.

Extracted from ``app.py`` so the handler's threading/marshalling logic lives
in its own module, keeping the main view file focused on composition.
"""
import logging
import threading

from rich.text import Text


class _RichLogHandler(logging.Handler):
    """Funnel ``logging`` records into the dashboard's ``RichLog``.

    Bridge worker threads emit records from outside the Textual event loop, so
    writes are marshalled with ``call_from_thread`` unless we are already on
    the UI thread. WARNING and above are also raised as toast notifications so
    the user does not have to catch them in the scrolling log. Shutdown races
    (loop gone, widget unmounted) are swallowed -- a closing dashboard must
    never crash on a late log line.
    """

    def __init__(self, app, rich_log):
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
