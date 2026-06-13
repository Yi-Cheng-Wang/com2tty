"""Tests for the WSL entry shims (bridge.py, pad_bridge.py).

These two files sit at the package root and are launched by path inside WSL
(``wsl --exec python3 -u .../bridge.py``). Each must work both when imported
as part of the package (``__package__`` set) and when run as a standalone
script (``__package__`` empty), in which case it bootstraps ``sys.path`` so
that ``import com2tty...`` resolves. Both branches are exercised here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


class TestShimPackageImport(unittest.TestCase):
    """When imported as a package module, the shim re-exports ``main`` and
    does not touch sys.path (``__package__`` is set)."""

    def test_bridge_reexports_serial_app_main(self):
        import com2tty.bridge as shim
        from com2tty.wsl.serial_app import main as serial_main
        self.assertIs(shim.main, serial_main)

    def test_pad_bridge_reexports_gamepad_app_main(self):
        import com2tty.pad_bridge as shim
        from com2tty.wsl.gamepad_app import main as gamepad_main
        self.assertIs(shim.main, gamepad_main)


class TestShimStandaloneExecution(unittest.TestCase):
    """When run as a script, ``__package__`` is empty, so the shim inserts
    the package parent on sys.path before importing the implementation."""

    def _exec_as_script(self, rel_path):
        path = os.path.join(_SRC, "com2tty", rel_path)
        with open(path, encoding="utf-8") as f:
            code = compile(f.read(), path, "exec")
        # Mimic ``python3 .../bridge.py``: top-level script, no package.
        ns = {"__name__": "not_main", "__package__": None, "__file__": path}
        saved_path = list(sys.path)
        try:
            exec(code, ns)
        finally:
            sys.path[:] = saved_path
        return ns

    def test_bridge_bootstraps_and_binds_main(self):
        ns = self._exec_as_script("bridge.py")
        from com2tty.wsl.serial_app import main as serial_main
        self.assertIs(ns["main"], serial_main)

    def test_pad_bridge_bootstraps_and_binds_main(self):
        ns = self._exec_as_script("pad_bridge.py")
        from com2tty.wsl.gamepad_app import main as gamepad_main
        self.assertIs(ns["main"], gamepad_main)

    def test_standalone_inserts_package_parent_on_path(self):
        # The bootstrap must put the directory *containing* the com2tty
        # package (i.e. src/) at the front of sys.path so the subsequent
        # ``import com2tty...`` resolves when launched by bare path.
        path = os.path.join(_SRC, "com2tty", "bridge.py")
        with open(path, encoding="utf-8") as f:
            code = compile(f.read(), path, "exec")
        ns = {"__name__": "not_main", "__package__": "", "__file__": path}
        saved_path = list(sys.path)
        captured = {}
        try:
            exec(code, ns)
            captured["first"] = sys.path[0]
        finally:
            sys.path[:] = saved_path
        self.assertEqual(os.path.normcase(captured["first"]),
                         os.path.normcase(_SRC))


if __name__ == "__main__":
    unittest.main()
