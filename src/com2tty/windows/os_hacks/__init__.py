"""Facade over the unavoidable Windows OS-level hacks.

The bridge has to reach below the documented API surface in a few places:
hiding/closing File Explorer windows that AutoPlay opens for a bootloader
drive, suppressing AutoPlay itself through the registry, watching
``WM_DEVICECHANGE`` through a hidden message window, and switching the
console to VT mode. Each hack lives in its own module here so the rest of
the codebase can stay free of raw ``ctypes.windll`` calls.

Every module in this package degrades gracefully off-Windows or when the
underlying call fails: the bridge keeps working, only the nicety is lost.
"""
