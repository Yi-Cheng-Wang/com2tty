"""UF2 flash support utilities on the Windows side.

Drive discovery and USB-serial-to-drive mapping for the mass-storage
bootloader (BOOTSEL) flashing path.
"""
import json
import logging
import os
import subprocess

from ..core.constants import CREATE_NO_WINDOW
# Re-exported so existing importers (control_handler) keep the same path while
# the implementation lives once in core.
from ..core.util import md5_hexdigest  # noqa: F401


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
                             creationflags=CREATE_NO_WINDOW, env=env)
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
