"""Hiding and closing the Explorer windows Windows opens for a BOOTSEL drive.

Even with AutoPlay suppressed, Windows (or the user) can end up with a File
Explorer window showing the UF2 bootloader drive while the bridge is writing
firmware to it. These helpers find such windows by their ``CabinetWClass``
window class and a title match (the BOOTSEL volume labels, or the drive
letter in the parenthesised "(X:)" form Explorer actually renders), hide
them instantly, and post ``WM_CLOSE``.

ctypes is imported lazily inside each function: the callers guard on
``os.name`` and the tests substitute a mock ctypes via ``sys.modules``.
"""
import logging
import time

from ...core.constants import EXPLORER_WINDOW_CLASS, UF2_VOLUME_LABELS

WM_CLOSE = 0x0010
SW_HIDE = 0

#: How often the background closer re-scans the desktop window list.
CLOSER_SCAN_INTERVAL = 0.01


def close_explorer_for_drive(drive_letter):
    """Close any File Explorer windows showing the BOOTSEL drive to prevent user interference."""
    try:
        import ctypes
        EnumWindows = ctypes.windll.user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        GetClassNameW = ctypes.windll.user32.GetClassNameW
        GetWindowTextW = ctypes.windll.user32.GetWindowTextW
        ShowWindow = ctypes.windll.user32.ShowWindow
        PostMessageW = ctypes.windll.user32.PostMessageW

        dl = drive_letter[0].upper()
        # Use the parenthesised "(X:)" form Explorer renders in titles; a
        # bare "X:" would also match unrelated windows showing that path.
        targets = list(UF2_VOLUME_LABELS) + [f"({dl}:)"]

        def foreach_window(hwnd, lParam):
            class_name = ctypes.create_unicode_buffer(256)
            GetClassNameW(hwnd, class_name, 256)
            if class_name.value == EXPLORER_WINDOW_CLASS:
                title = ctypes.create_unicode_buffer(512)
                GetWindowTextW(hwnd, title, 512)
                t_val = title.value.upper()
                for target in targets:
                    if target in t_val:
                        ShowWindow(hwnd, SW_HIDE)
                        PostMessageW(hwnd, WM_CLOSE, 0, 0)
                        break
            return True

        EnumWindows(EnumWindowsProc(foreach_window), 0)
    except Exception as e:
        logging.debug(f"Failed to close explorer window: {e}")


class BootselWindowCloser:
    """Background thread closing BOOTSEL Explorer windows as they appear.

    Scans every ``CLOSER_SCAN_INTERVAL`` seconds while a UF2 flash is in
    progress, so a window AutoPlay manages to open is hidden within ~10 ms.
    ``target_letters`` is a *live* list: the flash routine appends the
    discovered drive letter once known, and subsequent scans match it too.
    """

    def __init__(self, target_letters):
        self._target_letters = target_letters
        self._stop_event = None
        self._thread = None

    def _matches(self, t_val):
        """Whether an uppercased window title identifies a BOOTSEL window."""
        for label in UF2_VOLUME_LABELS:
            if label in t_val:
                return True
        for dl in self._target_letters:
            if f"({dl}:)" in t_val:
                return True
        return False

    def _run(self):
        import ctypes
        try:
            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            GetClassNameW = ctypes.windll.user32.GetClassNameW
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            ShowWindow = ctypes.windll.user32.ShowWindow
            PostMessageW = ctypes.windll.user32.PostMessageW

            def foreach_window(hwnd, lParam):
                class_name = ctypes.create_unicode_buffer(256)
                GetClassNameW(hwnd, class_name, 256)
                if class_name.value == EXPLORER_WINDOW_CLASS:
                    title = ctypes.create_unicode_buffer(512)
                    GetWindowTextW(hwnd, title, 512)
                    if self._matches(title.value.upper()):
                        ShowWindow(hwnd, SW_HIDE)
                        PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True

            # Keep the callback referenced for the lifetime of the loop so
            # ctypes does not garbage-collect the thunk mid-enumeration.
            cb = EnumWindowsProc(foreach_window)
            while not self._stop_event.is_set():
                EnumWindows(cb, 0)
                time.sleep(CLOSER_SCAN_INTERVAL)
        except Exception as e:
            logging.debug(f"Error in window closer thread: {e}")

    def start(self):
        import threading
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=1.0):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
