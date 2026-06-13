"""WSL-guest-side implementation of com2tty.

Everything in this package runs on the *Linux* Python interpreter inside
WSL, spawned by the Windows host as ``wsl --exec python3 -u <entry shim>``
(the shims are ``bridge.py`` and ``pad_bridge.py`` at the package root).

Hard constraints:
* Standard library only (Python 3.8+) -- the WSL guest must need no extra
  packages, so pyserial and any Windows-only modules are off limits.
* Unix-only modules (termios, fcntl) are fine here but must be importable
  on Windows for the test suite; the tests install lightweight fakes.
* May import ``com2tty.core`` (shared, dependency-free) but never
  ``com2tty.windows``.
"""
