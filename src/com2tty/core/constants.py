"""Magic strings, default paths, ports, and timing shared across the bridge.

These values are part of com2tty's observable behaviour (file system paths
created inside WSL, marker lines written into shell rc files, window titles
matched on the Windows desktop). Changing any of them is a compatibility
break for running sessions and external tooling, which is why they live in
one auditable place instead of being scattered as inline literals.
"""

# --------------------------------------------------------------------------
# Default endpoints and CLI defaults
# --------------------------------------------------------------------------

#: Default symlink path of the virtual serial device inside WSL.
DEFAULT_WSL_TTY = "/tmp/ttyUSB0"

#: Default FIFO path of the gamepad evdev stream inside WSL.
DEFAULT_PAD_FIFO = "/tmp/com2pad0"

#: Virtual device name advertised to Linux for the forwarded controller.
DEFAULT_PAD_NAME = "Microsoft X-Box 360 pad"

#: Default TCP port of the RFC 2217 forwarder inside WSL. The UF2 relay
#: always listens on this port + 1.
DEFAULT_RFC2217_PORT = 4000

#: Default XInput polling rate (Hz) in gamepad mode.
DEFAULT_POLL_HZ = 250

# --------------------------------------------------------------------------
# Process creation (Windows host)
# --------------------------------------------------------------------------

#: Win32 creation flag for the spawned wsl.exe. Prevents wsl.exe from
#: altering the console mode, which would disable Ctrl+C for the CLI.
CREATE_NO_WINDOW = 0x08000000

# --------------------------------------------------------------------------
# UF2 mass-storage bootloader (Windows host)
# --------------------------------------------------------------------------

#: Volume labels of the RP2040/RP2350 BOOTSEL drive, as they appear in
#: Explorer window titles (matched uppercased).
UF2_VOLUME_LABELS = ("RPI-RP2", "RP2350")

#: Marker file identifying a mounted UF2 bootloader drive.
UF2_INFO_FILENAME = "INFO_UF2.TXT"

#: Filename the firmware image is written to on the bootloader drive.
UF2_FLASH_FILENAME = "flash.uf2"

#: Window class of File Explorer windows (closed while a BOOTSEL drive is
#: mounted so the user cannot interfere with the flash).
EXPLORER_WINDOW_CLASS = "CabinetWClass"

# --------------------------------------------------------------------------
# Session liveness markers (WSL side, under /tmp)
# --------------------------------------------------------------------------

#: A heartbeat file older than this many seconds marks its session as dead.
ALIVE_TTL = 10.0

#: How often the WSL main loop refreshes its heartbeat files.
ALIVE_TOUCH_INTERVAL = 2.0

#: Per-port heartbeat file path (``% port``).
ALIVE_FILE_TEMPLATE = "/tmp/com2tty_alive_%d"

#: PID of the session that currently owns the picotool interception.
PICOTOOL_OWNER_FILE = "/tmp/com2tty_picotool.owner"

#: Where the generated picotool wrapper script is written inside WSL.
PICOTOOL_WRAPPER_PATH = "/tmp/com2tty_picotool.py"

# --------------------------------------------------------------------------
# Shell rc-file injection markers (WSL side)
# --------------------------------------------------------------------------
# Blocks are tagged with the owning session's PID (" [pid=N] ===") so two
# concurrent sessions do not remove each other's block; these prefixes are
# what identifies a block regardless of its tag.

RC_MARKER_START_PREFIX = "# === COM2TTY INJECTION START"
RC_MARKER_END_PREFIX = "# === COM2TTY INJECTION END"

# --------------------------------------------------------------------------
# AutoPlay crash-recovery marker (Windows host)
# --------------------------------------------------------------------------

#: Basename (under the system temp dir) of the JSON file persisting the
#: pre-suppression AutoPlay state, so a killed session can be healed.
AUTOPLAY_MARKER_FILENAME = "com2tty_autoplay_state.json"
