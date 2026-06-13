"""Shared, dependency-free definitions used by both sides of the bridge.

Everything under ``com2tty.core`` is imported by the Windows host *and* by
the helper processes running inside WSL, so it must stay pure standard
library (Python 3.8+): no pyserial, no ctypes.windll, no termios.
"""
