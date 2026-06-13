"""Temporary AutoPlay suppression while a UF2 bootloader drive is mounted.

Windows AutoPlay would pop up an Explorer window (or a toast) the moment the
BOOTSEL mass-storage drive enumerates, inviting the user to interact with a
drive the bridge is about to write and unmount. The suppressor flips the
``DisableAutoplay`` registry value for the duration of the flash and restores
the exact prior state afterwards -- including across crashes, via a marker
file persisted *before* the registry is touched.
"""
import json
import logging
import os

from ...core.constants import AUTOPLAY_MARKER_FILENAME

try:
    import winreg
except ImportError:
    winreg = None

_AUTOPLAY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers"
_AUTOPLAY_VALUE_NAME = "DisableAutoplay"


def _autoplay_marker_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), AUTOPLAY_MARKER_FILENAME)


def _restore_autoplay_state(existed, original_value):
    """Write the AutoPlay registry value back to a known prior state."""
    if not winreg:
        return
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOPLAY_KEY_PATH, 0, winreg.KEY_WRITE)
    try:
        if existed:
            winreg.SetValueEx(key, _AUTOPLAY_VALUE_NAME, 0, winreg.REG_DWORD, original_value)
        else:
            try:
                winreg.DeleteValue(key, _AUTOPLAY_VALUE_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def restore_orphaned_autoplay():
    """Recover AutoPlay if a previous run was killed while it was suppressed.

    AutoplaySuppressor persists the pre-modification state to a marker file
    before touching the registry. If the process dies before __exit__ runs, the
    DisableAutoplay value stays forced on; this reads that marker on the next
    startup, restores the saved state, and removes the marker.
    """
    marker = _autoplay_marker_path()
    if not os.path.exists(marker):
        return
    try:
        with open(marker, "r") as f:
            state = json.load(f)
        _restore_autoplay_state(state.get("existed", False), state.get("original_value", 0))
        logging.info("Recovered AutoPlay setting left disabled by a previous session.")
    except Exception as e:
        logging.debug(f"Failed to recover orphaned AutoPlay state: {e}")
    finally:
        try:
            os.remove(marker)
        except Exception:
            pass


class AutoplaySuppressor:
    """Temporarily disables Windows AutoPlay to prevent Explorer windows from popping up during device reboot."""
    def __init__(self):
        self.key_path = _AUTOPLAY_KEY_PATH
        self.value_name = _AUTOPLAY_VALUE_NAME
        self.original_value = None
        self.existed = False
        self.modified = False

    def __enter__(self):
        if not winreg:
            return self
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            try:
                self.original_value, val_type = winreg.QueryValueEx(key, self.value_name)
                self.existed = True
            except FileNotFoundError:
                self.existed = False
                self.original_value = 0

            # Persist the prior state BEFORE modifying, so a kill mid-flash can
            # be self-healed on the next startup (see restore_orphaned_autoplay).
            try:
                with open(_autoplay_marker_path(), "w") as f:
                    json.dump({"existed": self.existed,
                               "original_value": self.original_value}, f)
            except Exception as e:
                logging.debug(f"Failed to write AutoPlay recovery marker: {e}")

            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
            self.modified = True
        except Exception as e:
            logging.debug(f"Failed to temporarily disable AutoPlay: {e}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not winreg or not self.modified:
            return
        try:
            _restore_autoplay_state(self.existed, self.original_value)
        except Exception as e:
            logging.debug(f"Failed to restore AutoPlay settings: {e}")
        finally:
            try:
                os.remove(_autoplay_marker_path())
            except Exception:
                pass
