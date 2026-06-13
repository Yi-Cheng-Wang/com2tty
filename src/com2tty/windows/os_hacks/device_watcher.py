"""Event-driven device-change wake-ups for the Windows host.

The hot-plug reconnect loops and the ``--wait`` startup poll the Windows
port list on a fixed interval. When this watcher is running they wake
immediately on a ``WM_DEVICECHANGE`` notification instead of sleeping out
the full interval, so a replugged board resumes (and ``--wait`` proceeds)
the moment Windows enumerates it.

The watcher is a hidden window of the predefined ``STATIC`` class,
subclassed so its window procedure can observe ``WM_DEVICECHANGE``, and
registered for all device-interface arrival/removal notifications. A
daemon thread pumps its message queue and sets a ``threading.Event`` on
every arrival or removal.

Everything here is an optimization with a mandatory fallback: on
non-Windows hosts, or when window creation or notification registration
fails, the watcher reports inactive and callers fall back to plain
``time.sleep`` polling.
"""
import logging
import os
import threading
import time

WM_DEVICECHANGE = 0x0219
WM_CLOSE = 0x0010
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_DEVICEINTERFACE = 5
DEVICE_NOTIFY_ALL_INTERFACE_CLASSES = 4
GWLP_WNDPROC = -4
HWND_MESSAGE = -3

# Give the driver a beat after the notification: Windows can deliver the
# device change slightly before the COM port is actually openable.
_POST_EVENT_GRACE = 0.05


class DeviceChangeWatcher:
    """Signals a threading.Event whenever a device arrives or is removed."""

    def __init__(self):
        self._signal = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._hwnd = None
        self._wndproc = None  # keep the callback referenced for ctypes
        self.active = False

    def start(self, _os_name=os.name):
        """Start the message pump; True when notifications are live."""
        if _os_name != "nt":
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="com2tty-devnotify")
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.active

    def _handle_message(self, msg, wparam):
        """Window-procedure logic, separated from ctypes for testability.

        Returns True when the message asks the pump to quit (WM_CLOSE).
        """
        if msg == WM_DEVICECHANGE and wparam in (DBT_DEVICEARRIVAL,
                                                 DBT_DEVICEREMOVECOMPLETE):
            self._signal.set()
        return msg == WM_CLOSE

    def _pump(self):
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.CreateWindowExW.restype = ctypes.c_void_p
            user32.RegisterDeviceNotificationW.restype = ctypes.c_void_p

            hwnd = user32.CreateWindowExW(
                0, "STATIC", "com2tty-devnotify", 0, 0, 0, 0, 0,
                ctypes.c_void_p(HWND_MESSAGE), None, None, None)
            if not hwnd:
                raise OSError("CreateWindowExW failed")

            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_void_p)

            def _wndproc(h, msg, wparam, lparam):
                if self._handle_message(msg, wparam):
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(
                    ctypes.c_void_p(h), msg,
                    ctypes.c_void_p(wparam), ctypes.c_void_p(lparam))

            self._wndproc = wndproc_type(_wndproc)
            set_long = getattr(user32, "SetWindowLongPtrW",
                               user32.SetWindowLongW)
            set_long.restype = ctypes.c_void_p
            set_long(ctypes.c_void_p(hwnd), GWLP_WNDPROC, self._wndproc)

            class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
                _fields_ = [
                    ("dbcc_size", wintypes.DWORD),
                    ("dbcc_devicetype", wintypes.DWORD),
                    ("dbcc_reserved", wintypes.DWORD),
                    ("dbcc_classguid", ctypes.c_byte * 16),
                    ("dbcc_name", ctypes.c_wchar * 1),
                ]

            flt = DEV_BROADCAST_DEVICEINTERFACE()
            flt.dbcc_size = ctypes.sizeof(flt)
            flt.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
            hnotify = user32.RegisterDeviceNotificationW(
                ctypes.c_void_p(hwnd), ctypes.byref(flt),
                DEVICE_NOTIFY_ALL_INTERFACE_CLASSES)
            if not hnotify:
                user32.DestroyWindow(ctypes.c_void_p(hwnd))
                raise OSError("RegisterDeviceNotificationW failed")

            self._hwnd = hwnd
            self.active = True
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            user32.UnregisterDeviceNotification(ctypes.c_void_p(hnotify))
            user32.DestroyWindow(ctypes.c_void_p(hwnd))
        except Exception as e:
            logging.debug(f"Device-change watcher unavailable: {e}")
        finally:
            # The pump has ended (or never came up): callers must fall back
            # to plain sleeps, and start() must stop waiting either way.
            self.active = False
            self._ready.set()

    def wait(self, timeout):
        """Block up to ``timeout`` seconds; True when a device change fired.

        Falls back to a plain sleep when the watcher is not active, so
        callers can use it unconditionally in place of ``time.sleep``.
        """
        if not self.active:
            time.sleep(timeout)
            return False
        fired = self._signal.wait(timeout)
        if fired:
            self._signal.clear()
            time.sleep(_POST_EVENT_GRACE)
        return fired

    def stop(self):
        """Ask the pump to exit and wait briefly for the thread."""
        if self._hwnd:
            try:
                import ctypes
                ctypes.windll.user32.PostMessageW(
                    ctypes.c_void_p(self._hwnd), WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.active = False


_watcher = None
_watcher_lock = threading.Lock()


def start_device_watcher():
    """Start the process-wide watcher once; never raises.

    Returns the watcher when notifications are live, or None when the
    fallback (plain sleeping) is in effect.
    """
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            watcher = DeviceChangeWatcher()
            try:
                watcher.start()
            except Exception as e:
                logging.debug(f"Device-change watcher failed to start: {e}")
            _watcher = watcher
    return get_watcher()


def get_watcher():
    """The live process-wide watcher, or None when inactive/not started."""
    watcher = _watcher
    if watcher is not None and watcher.active:
        return watcher
    return None
