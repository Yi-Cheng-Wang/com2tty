"""Entry shim for the WSL serial-bridge helper.

The Windows host launches this file directly inside WSL with
``wsl --exec python3 -u .../com2tty/bridge.py``, so it cannot rely on the
package being importable: when run as a standalone script it puts the
package's parent directory on ``sys.path`` first. Keeping this file at the
package root (rather than moving it into ``com2tty/wsl``) preserves the
exact path the host resolves and verifies (see ``--doctor``).

The implementation lives in ``com2tty.wsl`` (``serial_app`` and friends).
"""
import os
import sys

if __package__ in (None, ""):  # executed as a script inside WSL
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com2tty.wsl.serial_app import main  # noqa: E402

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
