# Changelog

All notable changes to com2tty are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- The host module has been split: board detection and reset sequences live
  in `boards.py`, UF2/AutoPlay support in `uf2.py`, console colours in
  `banner.py`, port enumeration in `discovery.py`, and profile handling in
  `profiles.py`. `host.py` re-exports the moved names.
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
