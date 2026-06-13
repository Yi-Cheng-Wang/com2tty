"""Console colour handling for the startup banner."""
import os
import sys


def enable_vt_mode():
    """Enable ANSI escape processing on the Windows console (legacy conhost
    does not interpret VT sequences unless this flag is set)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetStdHandle returns a HANDLE (pointer-sized); without an explicit
        # restype ctypes truncates it to a 32-bit int before it is handed to
        # Get/SetConsoleMode.
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetConsoleMode.restype = ctypes.c_int  # BOOL
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetConsoleMode.restype = ctypes.c_int  # BOOL
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return False
        return True
    except Exception:
        return False


def get_banner_colors():
    """Return (yellow, cyan, green, reset) ANSI codes, or empty strings when
    they would render as garbage (redirected output, NO_COLOR, legacy conhost
    without VT support)."""
    if os.environ.get("NO_COLOR"):
        colored = False
    elif not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        colored = False
    elif os.name == "nt":
        colored = enable_vt_mode()
    else:
        colored = True
    if colored:
        return "\033[93m", "\033[96m", "\033[92m", "\033[0m"
    return "", "", "", ""
