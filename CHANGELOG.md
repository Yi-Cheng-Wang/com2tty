# Changelog

All notable changes to com2tty are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-06-13

### Added

- `--auto-respawn`: when the WSL helper dies (`wsl --shutdown`, a WSL
  servicing update, or a crash), com2tty now waits until WSL answers again
  and rebuilds the bridge with the same endpoint paths instead of exiting.
  Works in serial mode (where it implies `--wait`), multi-port mode, and
  gamepad mode.
- Multi-gamepad forwarding: `--pad-index` accepts several slots
  (`--pad-index 0 1`); each controller gets its own WSL helper and endpoint
  (`/tmp/com2pad0`, `/tmp/com2pad1`, ...), and its own `/dev/input` device
  in the uinput tier.
- Force feedback for the `/tmp` gamepad tier: a second FIFO at `<path>.ff`
  accepts 6-byte rumble frames from the consumer and drives the physical
  controller's motors, matching the uinput tier's kernel-driven rumble.
- Event-driven hot-plug wake-ups: the host registers for Windows
  `WM_DEVICECHANGE` notifications, so the reconnect loops and `--wait`
  resume the moment a device is enumerated instead of on the next polling
  tick; plain polling remains as the fallback when registration fails.
- `com2tty --doctor`: an environment self-check that probes `wsl.exe`,
  `wsl --exec` support, `python3` in the selected distribution, bridge-script
  readability, `fuser`, the RFC 2217 and UF2 relay TCP ports, leftovers from
  crashed sessions, `/dev/uinput` access, the AutoPlay recovery marker, and
  the XInput DLL, printing one actionable line per check.
- `--wait`: in serial mode, wait for the COM port to appear instead of
  failing when the device is not plugged in yet.
- `--list --json`: machine-readable JSON output for the port list.

### Changed

- Automatic baud-rate detection now reads the configured rate directly from
  the Win32 `GetCommState` API, which is locale-independent; parsing
  `mode.com` output (whose field labels are translated on non-English
  Windows) is kept only as a fallback.
- Profile INI files are now read as UTF-8 (with BOM tolerance), independent
  of the Windows ANSI codepage; files in a legacy single-byte encoding fall
  back to latin-1.
- Passing a COM port together with `--gamepad` is now an argument error
  instead of being silently ignored.

### Fixed

- Two concurrent com2tty sessions no longer destroy each other. A second
  invocation that reuses the default `--rfc2217-port` previously SIGKILLed
  the first session's WSL helper while "reclaiming" the port; listeners are
  now only reclaimed when the owning session's heartbeat file is stale.
  Likewise, the PlatformIO environment blocks in `~/.bashrc`/`~/.zshrc` and
  the fish snippet are tagged with the owning session's PID, so a session
  only removes its own block (and blocks of dead sessions), and the picotool
  interception records its owner so startup recovery no longer un-intercepts
  a live session.
- Hot-plug reconnection now survives traffic in the WSL-to-COM direction: a
  write to the dead COM handle no longer tears the whole bridge down; the
  data is dropped until the device reappears.
- Serial forwarding latency: the COM reader no longer waits for a full
  1024-byte buffer (up to 200 ms per chunk); it reads whatever has arrived.
- Dynamic line-setting changes from WSL now support Space/Mark parity and
  1.5 stop bits, matching the CLI, and the termios `B0` hangup pseudo-rate
  is ignored instead of being applied as a 0 baud rate.
- The RFC 2217 forwarder and the UF2 relay no longer read the shared stdin
  pipe at the same time; sessions are serialised against each other.
- Concurrent open/close/setting changes on the Windows serial handle (the
  hot-plug reconnect racing the dynamic-settings handler) are now serialised
  by a lock.

## [0.2.0] - 2026

### Added

- `com2tty --list` (`-l`): enumerate every serial port Windows can see,
  showing the device name, VID:PID, USB bus id (e.g. `1-6`, read from the
  device descriptor without usbipd), USB serial number, detected board
  family, and description.
- `--version` flag.
- Hot-plug auto-reconnect: when a bridged device is unplugged or
  re-enumerates, the bridge now follows it by USB serial number and resumes
  automatically instead of spinning on the dead handle.
- Multi-port bridging: `com2tty COM3 COM5` bridges several ports at once.
  Each port gets its own WSL endpoint and RFC 2217 port; only the first
  port owns the PlatformIO environment variables and picotool interception
  (secondary helpers run with the new `--no-env-setup` flag).
- New board families for reset and upload handling: `nrf52` (Adafruit UF2
  bootloader, reuses the UF2 relay), `samd` (Arduino Leonardo/SAMD-class
  1200-baud touch, with automatic acquisition of the re-enumerated bootloader
  port and restoration of the application port afterwards), and `stm32`
  (DTR/RTS reset pulse). `--board` accepts the new values.
- Gamepad force feedback: in `--uinput` mode the virtual pad advertises
  `FF_RUMBLE`; effects played by games inside WSL are forwarded back to the
  Windows host and drive the physical controller's motors via
  `XInputSetState`.
- Guide (Xbox logo) button is now reported as `BTN_MODE`, polled through
  the `XInputGetStateEx` export when available.
- Argument profiles: `com2tty @myboard` expands the `[myboard]` section of
  `./com2tty.ini` or `~/.com2tty.ini` into command-line arguments.
- fish shell support: the PlatformIO environment variables are also written
  to `~/.config/fish/conf.d/com2tty.fish` when fish is detected.
- Manual end-to-end smoke test script under `scripts/`.

### Changed

- The flat module layout has been reorganised into four domain packages
  with no change to external behaviour, the command-line interface, or the
  `[CONTROL]` wire protocol. `core/` holds the dependency-free definitions
  shared by both interpreters (the `[CONTROL]` message catalogue and its
  dispatcher, the gamepad and rumble frame codecs, board data, and
  constants); `windows/` holds the host-side logic previously in `host.py`
  (the serial and gamepad session orchestration, the COM-port and hot-plug
  handling, the board reset sequences, the control-protocol handlers, and an
  `os_hacks/` facade over the AutoPlay, Explorer, device-notification, and
  console interventions); `wsl/` holds the guest-side helpers previously in
  `bridge.py` and `pad_bridge.py` (the pseudo-terminal manager, the loopback
  TCP servers, the shell-environment and picotool integrations, and the
  evdev sinks); and `cli/` holds the argument parser and profile handling.
  `bridge.py` and `pad_bridge.py` remain at the package root as thin entry
  shims so the launch paths the Windows host resolves are unchanged. Internal
  import paths changed accordingly and no backwards-compatibility re-exports
  were kept.
- CI now also runs on Python 3.13 and includes a ruff lint job.

## [0.1.3] - 2026

### Added

- Startup environment checks with actionable errors (`wsl.exe` presence,
  `python3` in the distribution, bridge script readability).
- `--distro` to select a WSL distribution and `--board` to override USB VID
  board detection.
- Crash self-healing: orphaned picotool interceptions, stale rc-file blocks,
  and a disabled AutoPlay setting left by a killed session are repaired on
  the next start.

### Fixed

- PowerShell injection hardening in the USB-serial-to-drive lookup.
- Cross-environment compatibility fixes (localized `mode.com` output,
  non-ASCII install paths, legacy conhost colour handling).

## [0.1.2] - 2026

### Added

- Gamepad mode: forward a Windows XInput controller into WSL as an evdev
  stream (`/tmp` FIFO by default, real `/dev/input` device via `--uinput`).
- Smart UF2 routing: picotool interception in WSL, UF2 relay back to the
  host, target-drive lookup by USB serial number, MD5 verification, and
  AutoPlay/Explorer window suppression during flashing.

## [0.1.1] - 2026

### Added

- ESP32 firmware upload from WSL through an in-WSL RFC 2217 forwarder with
  automatic DTR/RTS download-mode reset.
- Automatic baud-rate detection (`--baud auto`).

## [0.1.0] - 2026

### Added

- Initial release: bridge a Windows COM port into WSL as a pseudo-terminal
  symlink (default `/tmp/ttyUSB0`) over a stdin/stdout pipe, with dynamic
  line-setting propagation.
