"""UF2 flash support utilities on the Windows side.

Drive discovery, USB-serial-to-drive mapping, and the AutoPlay suppression
used while a UF2 mass-storage bootloader is mounted.
"""
import json
import logging
import os
import subprocess

try:
    import winreg
except ImportError:
    winreg = None


def md5_hexdigest(data):
    import hashlib
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    except TypeError:  # Python < 3.9 has no usedforsecurity flag
        return hashlib.md5(data).hexdigest()


def list_removable_drives():
    """Candidate roots for the BOOTSEL mass-storage drive.

    Checking the drive type first keeps the scan off disconnected network
    drives, where os.path.exists can block for tens of seconds.
    """
    import string
    try:
        import ctypes
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        DRIVE_REMOVABLE = 2
        return [f"{d}:\\" for d in string.ascii_uppercase
                if get_drive_type(f"{d}:\\") == DRIVE_REMOVABLE]
    except Exception:
        return [f"{d}:\\" for d in string.ascii_uppercase
                if os.path.exists(f"{d}:\\")]


def get_drive_by_serial(serial_num):
    """Use PowerShell/CIM to map a USB Serial Number to a logical Windows Drive Letter.

    The serial number originates from an external USB device descriptor and is
    therefore untrusted input. It is handed to PowerShell through an environment
    variable -- never interpolated into the script text -- and matched as a
    regex-escaped literal, so a hostile serial (containing quotes, ``$(...)``,
    backticks, or regex metacharacters) cannot inject PowerShell or corrupt the
    match.
    """
    ps_cmd = r'''
$serial = $env:COM2TTY_TARGET_SERIAL
$escaped = [regex]::Escape($serial)
$drives = Get-CimInstance Win32_DiskDrive
$partitions = Get-Partition
$result = @()
foreach ($d in $drives) {
    if ($d.PNPDeviceID -match $escaped) {
        foreach ($p in $partitions) {
            if ($p.DiskNumber -eq $d.Index -and $p.DriveLetter) {
                $result += [PSCustomObject]@{DriveLetter=($p.DriveLetter + ":\"); PNPDeviceID=$d.PNPDeviceID}
            }
        }
    }
}
$result | ConvertTo-Json -Compress
    '''
    try:
        env = dict(os.environ)
        env["COM2TTY_TARGET_SERIAL"] = serial_num
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                             capture_output=True, text=True,
                             creationflags=0x08000000, env=env)
        output = res.stdout.strip()
        if not output:
            return None

        data = json.loads(output)
        if isinstance(data, dict):
            return data.get('DriveLetter')
        elif isinstance(data, list) and len(data) > 0:
            return data[0].get('DriveLetter')
    except Exception as e:
        logging.debug(f"Failed to map USB serial to drive: {e}")
    return None


_AUTOPLAY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers"
_AUTOPLAY_VALUE_NAME = "DisableAutoplay"


def _autoplay_marker_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "com2tty_autoplay_state.json")


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
