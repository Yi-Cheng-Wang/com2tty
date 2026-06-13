"""Windows-host-side implementation of com2tty.

Everything in this package runs on the Windows Python interpreter: it may
use pyserial, ``ctypes.windll``, ``winreg`` and Win32-specific process
flags. Nothing here is imported by the helpers running inside WSL.
"""
