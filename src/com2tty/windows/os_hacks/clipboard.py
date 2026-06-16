"""Set the Windows clipboard directly from the host process.

Textual's ``copy_to_clipboard`` emits an OSC 52 escape sequence, which conhost
and several Windows Terminal configurations silently ignore -- so an in-app
copy appears to do nothing. The dashboard runs on the Windows host, so it can
set the clipboard itself.

It does so with the Win32 clipboard API through ``ctypes`` rather than spawning
``clip.exe``. Spawning a console subprocess from the full-screen Textual app is
not safe here: a child console process can change the Windows console mode --
which is how Ctrl+C is delivered to the foreground process (see the CREATE_NO_
WINDOW note in ARCHITECTURE.md) -- and so intermittently froze or crashed the
dashboard when the user pressed Ctrl+C to copy. An in-process API call touches
no console and starts no child process, so it cannot disturb the terminal.
"""
import ctypes
import os

# Clipboard format for a NUL-terminated UTF-16 (wide) string, and the moveable
# global-memory flag SetClipboardData requires for the handle it takes ownership
# of. See the Win32 clipboard documentation.
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def set_windows_clipboard(text):
    """Put ``text`` on the Windows clipboard via the Win32 API; True on success.

    Returns False (never raises) on non-Windows or any failure, so the caller
    can fall back to the OSC 52 path. Unicode is preserved: the text is stored
    as ``CF_UNICODETEXT`` (UTF-16), so non-ASCII content copies correctly.
    """
    if os.name != "nt":
        return False
    try:
        return _set_clipboard_unicode(text)
    except Exception:
        return False


def _set_clipboard_unicode(text):
    """Copy ``text`` to the clipboard with the Win32 API. True on success.

    The handle passed to ``SetClipboardData`` is owned by the system on success,
    so it is freed here only on the failure paths (before ownership transfers).
    """
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    # Declare pointer-sized return/argument types so 64-bit handles and
    # pointers are not silently truncated to 32 bits.
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    # CF_UNICODETEXT is a UTF-16-LE string terminated by a wide NUL.
    data = text.encode("utf-16-le") + b"\x00\x00"

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            # Ownership did not transfer; release the memory ourselves.
            kernel32.GlobalFree(handle)
            return False
        # Success: the system now owns ``handle``; do not free it.
        return True
    finally:
        user32.CloseClipboard()
