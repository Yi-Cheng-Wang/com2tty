# Changelog

All notable changes to com2tty are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-06-16

### Added

- Interactive dashboard (`--dashboard`): a Textual terminal user interface that
  manages serial and gamepad forwarding and the environment doctor from a single
  screen. It lists detected COM ports and XInput controller slots in tables that
  refresh on a timer, attaches and detaches devices, allocates a distinct WSL
  endpoint and RFC 2217 port to each attached serial device automatically,
  switches the active WSL distribution, renders the doctor results, tails a
  unified activity log, surfaces action-required messages as dismissable notices
  in the lower-right corner, and renders the README inside the terminal with F1.
  The layout reflows to the terminal size.
- `textual`, version 1.0.0 or later, is now a host runtime dependency used only
  by the dashboard. The command-line modes do not require it, and the dashboard
  prints an installation hint and exits with a non-zero status when it cannot be
  imported. (The minimum was raised from 0.40.0 for the in-terminal text
  selection, read-only `TextArea`, and clipboard-copy APIs the dashboard now
  uses.)
- The dashboard proactively surfaces copy-pasteable setup commands when it
  detects a one-time privileged step the operator is missing. The gamepad pane
  pops a dialog with the `/dev/uinput` setup commands (and a button that copies
  them to the clipboard) when the uinput tier would fall back for lack of
  permission, and the Doctor pane offers the corresponding commands for any
  check that warned or failed (for example installing `psmisc` or `python3`).
- Argument profiles accept several whitespace-separated ports in one `port`
  key (`port = COM3 COM5`), so a saved profile can drive multi-port mode.

### Changed

- Running `com2tty` with no positional COM port and no other mode flag now opens
  the dashboard instead of reporting a missing-argument error. The command-line
  modes are unchanged and remain available for scripting and one-shot bridges.
- The dashboard's F1 README modal renders Markdown with the dashboard's own
  renderer (`windows/dashboard/_markdown.py`, no Markdown library imported) into
  a single selectable widget: headings, emphasis, inline code, fenced code
  blocks (rendered as a block), lists, blockquotes, rules, tables and links are
  styled, while the document stays one widget so selecting a passage and pressing
  Ctrl+C reliably copies it (markup stripped, code verbatim) -- a command from a
  code block pastes exactly. Links stay clickable: a table-of-contents entry
  jumps to its heading, a web link opens externally, and a relative path is
  reported but not followed. The modal's chrome is a `✕` button in the top-right
  corner and a centred footer hint. All dashboard copies (the README selection
  and the command dialog's Copy
  button) go through the in-process Win32 clipboard API rather than Textual's
  OSC 52 escape sequence, which conhost and some Windows Terminal configurations
  silently drop -- so a copy now actually lands on the clipboard. The command
  dialog confirms a copy with an in-dialog status line instead of a bottom
  toast, so pressing Copy no longer triggers a relayout that flickered the
  dialog border.
- The dashboard's Serial Ports tab auto-detects a `/dev` alias for each attached
  device. The device is served at `/tmp/ttyUSB{n}`; the user may, with no
  configuration, run `sudo ln -sf /tmp/ttyUSB0 /dev/<anyname>` by hand and the
  Endpoint column switches to that `/dev` path automatically (reverting if it is
  removed). The advanced settings keep an optional "Show /dev link command"
  checkbox that pops the suggested command on attach, and a "WSL path" field
  that renames the `/tmp` endpoint.
- After the gamepad uinput permission setup, the dashboard now reminds the user
  to detach and re-attach the controller for the change to take effect.
- A gamepad attached through the uinput tier now shows its real device class
  (`/dev/input/event*`) in the dashboard's Endpoint column, falling back to the
  `/tmp` stream path only when the permission probe shows the bridge will fall
  back -- a successful uinput device is no longer mislabelled as the `/tmp` path.
- The dashboard enumerates serial ports in a worker thread on its periodic
  refresh, so the blocking Windows SetupAPI call no longer hitches the interface.
- The WSL helper now detects mark and space parity (CMSPAR) on the pseudo
  terminal, so a client setting either is propagated to the COM port rather than
  reported as plain odd/even.
- The PyPI publish workflow runs with least-privilege `permissions: contents:
  read` (read-only GitHub token) by default.

### Security

- The UF2 relay now authenticates the picotool wrapper with a per-session token
  held only in the owner-readable wrapper script, so on a shared or multi-user
  WSL host another local user can no longer push firmware to the relay during an
  upload.
- The fixed-name `/tmp` artifacts the WSL helper writes (the picotool wrapper,
  its owner file, and the per-port heartbeats) are now written through a
  symlink-safe path that unlinks any pre-existing entry and creates the file with
  `O_EXCL | O_NOFOLLOW`, so a symlink planted by another local user at one of
  those paths is rejected rather than followed. The picotool wrapper is created
  owner-only so its embedded token stays secret.
- Session-liveness detection now requires `/proc/<pid>/cmdline` to name both
  `com2tty` and the helper script, so leftover-listener cleanup and orphan
  reclamation no longer treat an unrelated process that merely mentions one of
  those strings as a live bridge.

### Fixed

- A WSL distribution passed with `--distro` is now honoured by the dashboard
  rather than reset to the default distribution when the interface starts.
- The post-upload serial-port reopen is now serialised against the
  dynamic-settings handler. The RFC 2217 and UF2 upload controllers now share
  that handler's lock instead of each falling back to a private one, extending
  the 0.3.0 reopen-lock fix to cover the upload-path reopens.
- The WSL helper no longer crashes when the user-writable `/tmp` fallback for
  the tty symlink is blocked by the sticky bit. If `/tmp/ttyUSB0` is owned by
  another user and cannot be unlinked, the helper retreats to a user-scoped
  path (`/tmp/ttyUSB0_<user>`, then a PID-scoped one) instead of raising a
  `PermissionError`.
- The XInput functions (`XInputGetState`, the undocumented `XInputGetStateEx`,
  and `XInputSetState`) now declare their `ctypes` argument and return types, so
  the unsigned 32-bit status is not truncated and the pointer arguments are
  sized correctly on 64-bit Python.
- A malformed dynamic line-settings token from WSL (one without `=`) is now
  skipped with a warning instead of aborting the whole settings update, so the
  remaining valid tokens still apply.
- The serial-mode environment-variable injection now appends to the shell
  startup files atomically. The 0.3.1 fix made the cleanup rewrite atomic, but
  the append path still used a plain append that a crash could leave truncated.
- A failed UF2 flash no longer leaks the background Explorer-window-closer
  thread. The closer is now stopped in a `finally` block, so an error during the
  flash cannot leave it polling indefinitely.
- The serial-mode environment-variable injection now preserves a symlinked
  `~/.bashrc`/`~/.zshrc` (common with dotfile managers): the link's target is
  rewritten and the link is kept, rather than the link being replaced with a
  regular file that detaches the user's tracked dotfiles.
- The dashboard no longer accumulates records for bridges that ended on their
  own (for example after the WSL helper exited): such terminal records are
  reaped from the device listings, which also clears their now-stale endpoint
  from the table.
- The dashboard's F1 README was rebuilt with the dashboard's own Markdown
  renderer feeding a single selectable widget, instead of Textual's `Markdown`
  widget. Selecting a passage and pressing Ctrl+C no longer crashes (the
  Markdown widget's deep-tree, screen-level selection was the cause); the
  rendered document is one widget whose selection copies as clean plain text
  (markup stripped, code verbatim). Links remain clickable -- a table-of-contents
  entry jumps to its heading, a web link opens externally, and a relative path is
  reported but not followed, so a documentation link can no longer open a browser
  or trip the OS folder-access protection. The copy routes through the in-process
  Win32 clipboard (see below). There is no longer a "copy the whole document"
  button.
- The dashboard's `r` (Refresh ports) and `d` (Run doctor) key bindings now
  switch to the relevant tab as they run, so the refreshed port table and the
  doctor results are actually brought on screen instead of updating a tab the
  user is not looking at.
- Copying from the dashboard no longer freezes or crashes the interface when the
  user presses Ctrl+C. The clipboard is now set with the in-process Win32
  clipboard API (via `ctypes`) instead of spawning `clip.exe`; a console
  subprocess could change the Windows console mode, which is how Ctrl+C is
  delivered to the foreground process, and so intermittently hung or crashed the
  dashboard exactly when copying.
- Copying from the dashboard no longer briefly freezes the interface. The Win32
  clipboard write now runs on a worker thread instead of the UI thread, because
  setting the clipboard broadcasts a change notification to every listener
  (third-party clipboard managers and Windows' own Clipboard History and Cloud
  Clipboard sync) and blocks until they respond, which could stall the dashboard
  for the duration of each copy.
- Highlighting text in the F1 README no longer lags. The README widget now
  renders only the visible rows as the selection is dragged, wrapping the
  document once per width, instead of re-rendering the entire several-hundred-row
  document on every mouse move; the rendered output is unchanged.
- The dashboard now auto-detects a serial bridge's `/dev` alias with no
  configuration. The bridge serves the device at `/tmp/ttyUSB{n}`; if the user
  runs `sudo ln -sf /tmp/ttyUSB0 /dev/<anyname>` by hand, the dashboard scans
  `/dev` (by realpath, so any name is found) and shows that `/dev` path in the
  Endpoint column, reverting to `/tmp` if the alias is removed -- no re-attach
  and no pre-configured name. The advanced "Show /dev link command" checkbox is
  now optional guidance that just pops the suggested `sudo ln -sf` command.

## [0.3.1] - 2026-06-13

### Added

- Startup same-port conflict guard: before any shared-state side effects, a
  serial session probes its RFC 2217 and UF2 relay ports and refuses to start
  if either is already bound, rather than starting with a broken forwarder and
  cross-wiring its uploads to another session's board.
- tty symlink anti-hijack: the WSL helper refuses to replace a tty path that is
  a symlink to another live session's pseudo-terminal slave, so the shared
  default `/tmp/ttyUSB0` cannot be silently stolen; stale or dangling links are
  still replaced.

### Changed

- Intercepted `picotool` now forwards non-flashing subcommands (`info`,
  `reboot`, `help`, and any invocation that carries no `.uf2`/`.elf` image) to
  the real binary instead of exiting silently, so diagnostic uses keep working
  while the interception is active.
- Passing a COM port together with `--list` is now an argument error instead of
  being silently ignored.
- Argument profiles accept a `@@` literal escape: `@@value` is passed through as
  the literal `@value` and is never treated as a profile reference.

### Fixed

- Closing the console window no longer orphans the WSL helper. The `wsl.exe`
  child is placed in a kill-on-close Windows Job Object, so it is reaped with
  the host instead of being left running and holding the relay ports and the
  injected shell-rc block.
- Injections (the shell-rc block, the picotool interception, the tty symlink,
  and the heartbeat files) are now reliably cleaned on exit, including on Ctrl+C
  and on window close. The helper handles SIGTERM and SIGHUP through its normal
  shutdown path, and the host closes the helper's stdin and waits for a graceful
  exit before escalating to terminate and kill.
- Session-liveness detection is now PID-verified: a port is reported as held
  only when its heartbeat is fresh and owned by a live com2tty process other
  than the caller, which removes a spurious "port held by another live session"
  warning a single session could raise against its own marker, and prevents a
  recycled PID from being mistaken for a live session.

### Security

- The XInput DLL is loaded by its absolute path under `System32` rather than by
  bare name, closing a DLL-hijacking path through the working directory.
- The gamepad event FIFO and its force-feedback companion are created with
  owner-only permissions instead of world-writable, preventing other local
  users from reading the input stream or injecting events.
- The shell startup files are rewritten atomically (temporary file plus rename),
  so an interrupted cleanup cannot leave `~/.bashrc` or `~/.zshrc` truncated.
- The AutoPlay recovery marker is stored under the user's LocalAppData instead
  of the shared system temporary directory, removing a temporary-file hijacking
  surface.

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
