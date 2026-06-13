# Architecture

This document describes the internal design of com2tty. It is intended for
contributors and for users who need to understand or debug the bridge beyond
what the [README](README.md) covers. Every behaviour described here is
verifiable from the source under `src/com2tty/`.

## Problem domain

A developer working inside Windows Subsystem for Linux (WSL) cannot directly
reach hardware that is bound to the Windows host. USB-to-serial adapters and
XInput game controllers are owned by Windows drivers, and the usual remedy,
`usbipd`, either takes the device away from Windows entirely or, in the case of
XInput controllers, produces a device that the stock WSL2 kernel cannot drive
because it is built without the `xpad` driver. com2tty solves this by leaving
the device under its native Windows driver, reading or writing it on the Windows
side, and forwarding the data into WSL over a transport that needs no network
configuration, port forwarding, or firewall change.

## The transport

Every mode uses the same transport: the standard input, output, and error
streams of a helper process that the Windows host launches inside WSL with
`wsl --exec python3 -u <helper>`. The host writes host-to-WSL data on the
helper's standard input, reads WSL-to-host data on the helper's standard output,
and reads a line-oriented control protocol on the helper's standard error. This
choice is deliberate: pipes between the Windows host and the WSL guest are
always available, require no listening socket reachable from outside the
machine, and are not affected by firewall policy.

The `--exec` form matters. Without it, `wsl.exe` rejoins its arguments and hands
them to the distribution's login shell, which re-splits on whitespace and would
corrupt any path containing a space. The helper is therefore always launched
through `wsl_command` in `windows/wsl_process.py`, which inserts `--exec` and
preserves arguments verbatim. The host process is created with the
`CREATE_NO_WINDOW` flag so that `wsl.exe` does not alter the Windows console
mode, which would otherwise disable Ctrl+C handling for the foreground com2tty
process.

The lifetime of the helper is bound to the host. The spawned `wsl.exe` is placed
in a Windows Job Object flagged to terminate its members when the job handle is
closed, so that closing the console window, which kills the host process without
running its Ctrl+C teardown, reaps the helper instead of orphaning a `bridge.py`
that would keep holding the relay ports and the injected shell-rc block.
Teardown is made graceful from both directions: the serial helper installs
SIGTERM and SIGHUP handlers that route a terminating signal through the same
shutdown path as Ctrl+C, and on an orderly stop the host first closes the
helper's standard input, which the helper's `select` loop observes as
end-of-file, and then waits for the helper to finish its own cleanup and exit
before escalating to terminate and finally kill. This ensures the shell-rc block, the picotool interception, the tty
symlink, and the heartbeat files are removed on exit rather than leaked.

The helper entry scripts, `bridge.py` and `pad_bridge.py`, stay at the package
root as thin shims: the host resolves and verifies exactly those paths, and a
script launched by path cannot assume the package is importable, so each shim
puts the package's parent directory on `sys.path` before delegating to the
implementation in `com2tty.wsl`.

## The control protocol

The helper's standard error doubles as a control channel. Lines that begin with
`[CONTROL]` are interpreted by the host; all other lines are surfaced as log
output. The message catalogue is defined once in `core/protocol.py`, which both
sides import: the WSL helpers emit the literals, and the host routes each line
through a `ControlDispatcher` whose registered handlers live in
`windows/control_handler.py`. A line matches a handler only when the registered
name is followed by the end of the line or a `:` payload separator, so one
message name cannot be shadowed by another that shares its prefix. The serial
path uses the following messages, all emitted by the serial helper unless noted.

The line `[CONTROL] SETTINGS: baud=<n> bytesize=<n> parity=<X> stopbits=<n>`
reports that a WSL program changed the pseudo-terminal line discipline; the host
applies the change to the underlying COM port. The pair
`[CONTROL] RFC2217_READY:<port>`, `[CONTROL] RFC2217_CONNECT`, and
`[CONTROL] RFC2217_DISCONNECT` drive the RFC 2217 session lifecycle, and
`[CONTROL] RFC2217_ERROR: <detail>` reports a forwarder bind failure. The UF2
upload path uses `[CONTROL] UF2_READY:<port>`,
`[CONTROL] UF2_UPLOAD_START:<size>:<md5>`, `[CONTROL] UF2_UPLOAD_END`, and
`[CONTROL] UF2_ERROR: <detail>`. The host answers an upload start by writing
`[CONTROL] UF2_ACK` back on the helper's standard input, which is the only
control message that travels from host to helper. The gamepad helper
emits `[CONTROL] PAD_READY`, `[CONTROL] PAD_UINPUT_UNAVAILABLE`,
`[CONTROL] PAD_PERMISSION_ERROR`, and `[CONTROL] PAD_ERROR`.

## The serial data path

In serial mode, `run_bridge` in `windows/bridge_app.py` opens the COM port with
`pyserial`, launches the serial helper in WSL, and runs three daemon threads. One thread relays
bytes from the COM port to the helper's standard input, one relays bytes from
the helper's standard output to the COM port, and one reads the helper's standard
error and acts on the control protocol. A `threading.Event` named
`shutdown_event` coordinates orderly teardown, and two further events,
`rfc2217_active_event` and `uf2_active_event`, suspend ordinary pseudo-terminal
relaying while an RFC 2217 session or a UF2 upload owns the pipe.

Inside WSL, `wsl/serial_app.py` creates a pseudo terminal with `os.openpty`
(through `wsl/pty_manager.py`), symlinks the requested path (default
`/tmp/ttyUSB0`) to the pseudo-terminal slave, and runs a
`select` loop that copies data between the helper's standard input and output and
the pseudo-terminal master. It keeps the slave descriptor open for the lifetime
of the process so that a WSL client opening and closing the port does not raise
an input/output error on the master side. When the requested symlink path is not
writable, typically because it lies under `/dev`, the helper falls back to the
equivalent path under `/tmp` and prints the one-time command to link the two.

## RFC 2217 forwarding and firmware upload

To let build tools inside WSL flash a board attached to Windows, the serial
helper starts two TCP servers bound to the loopback interface inside the WSL
distribution; both derive from `LoopbackTcpServer` in `wsl/servers/base.py`,
which owns the shared lifecycle (liveness-aware reclamation of a leftover
listener, bind-failure reporting, the READY announcement, and the accept loop).
The RFC 2217 forwarder (`wsl/servers/rfc2217_forwarder.py`) listens on the
configured port (default 4000) and relays an esptool or PlatformIO serial
connection through the standard input and output pipe;
`windows/rfc2217_redirector.py` supplies the `Redirector` that implements the
RFC 2217 protocol against the Windows COM port. The UF2 relay
(`wsl/servers/uf2_relay.py`) listens on the configured port plus one.

When an RFC 2217 client connects, the host suspends pseudo-terminal relaying,
performs the board-specific reset described below, and runs the redirector for
the duration of the session. Because the host performs all DTR and RTS resets
itself, the COM port handed to the redirector is wrapped in `ResetProofSerial`,
which silently absorbs DTR and RTS changes requested by the RFC 2217 client so
that they cannot interfere with the controlled reset sequence while forwarding
all other attribute access to the real port.

For boards whose firmware is delivered as a UF2 image on a mass-storage
bootloader, `wsl/integrations/picotool.py` intercepts the `picotool` binary
inside WSL by renaming it to `picotool.real` and replacing it with a wrapper
(rendered from the packaged template `wsl/assets/picotool_wrapper.py.in`). When
PlatformIO invokes
`picotool`, the wrapper sends the UF2 image to the UF2 relay, which forwards it
to the host over standard output framed by the `UF2_UPLOAD_START` and
`UF2_UPLOAD_END` control messages and an MD5 checksum. The host accumulates the
image, verifies the checksum, triggers the bootloader, locates the target
mass-storage drive, and writes the image. To make the upload deterministic, the
host suppresses Windows AutoPlay and closes any Explorer window that opens for
the bootloader drive while the write is in progress.

To prevent a crashed session from leaving the environment broken, the helper
self-heals on startup: it restores any `picotool` left intercepted by a previous
run and removes any stale environment-variable block from the shell startup
files, and the host restores any AutoPlay setting left disabled by a previous
run.

## Board detection and reset sequences

`core/boards.py` maps a USB vendor identifier to a board family through
`BOARD_VID_MAP` and holds the reset timing parameters; the sequences themselves
live in `windows/board_reset.py`. The Raspberry Pi vendor maps to `pico`; Silicon Labs, QinHeng,
FTDI, Prolific, and Espressif map to `esp32`; the Adafruit nRF52 vendor maps to
`nrf52`; Arduino, Seeed, and SparkFun map to `samd`; and STMicroelectronics maps
to `stm32`. Generic USB-UART vendors are classified as `esp32` because the ESP32
auto-reset is the useful default for the boards that typically use them; the
`--board` option overrides the guess, and `--board none` disables board-specific
resets entirely. The families whose firmware arrives as a UF2 image,
`UF2_FAMILIES`, are `pico` and `nrf52`.

Each family has a reset sequence. The ESP32 sequence toggles DTR and RTS to enter
the download mode, and a post-upload pulse of RTS boots the new firmware. The
RP2040, RP2350, and nRF52 sequence is the 1200-baud touch that triggers the
mass-storage bootloader; it closes the port afterwards and restores the baud rate
while the port is closed so that a later reopen does not re-trigger the touch.
The SAMD and Leonardo sequence is the same 1200-baud touch, but the bootloader
re-enumerates as a separate serial port, so the host handles it specially as
described in the next section. The STM32 sequence pulses DTR and RTS in the
conventional BOOT0 and NRST wiring; boards flashed through ST-LINK or DFU are
unaffected by the pulse.

## Hot-plug reconnection

A USB device that is unplugged, reset, or re-enumerated invalidates the open
Windows handle, and Windows may assign it a different COM number when it returns.
`windows/serial_host.py` handles this with two helpers. `reopen_serial_port` closes the stale
handle and then repeatedly tries the original port name, falling back to scanning
for a port whose USB serial number matches the original device. The COM-to-WSL
relay thread tolerates a short run of transient errors, which cover a board
rebooting into its bootloader, before treating the device as gone and invoking
the reopen logic. The reopen yields to an active RFC 2217 or UF2 session so that
it never competes with the upload path, which manages its own reopen.

SAMD and Leonardo bootloaders need a different strategy because the bootloader is
a genuinely new device with its own USB identity, so matching by serial number is
unreliable. For these, `acquire_new_port` snapshots the set of COM ports before
the 1200-baud touch, then opens whichever port newly appears, which is the
approach the Arduino tooling uses. The host runs the upload against that
bootloader port and restores the application port when the upload completes and
the board reboots.

## The gamepad data path

The gamepad path reuses the spawn-and-pipe transport. On the Windows side,
`windows/gamepad_host.py` polls one XInput controller slot through `ctypes`, preferring the
undocumented `XInputGetStateEx` export (ordinal 100) so that the Guide button is
visible, and packs each state snapshot into a fixed sixteen-byte frame. A frame is
sent only when the controller's packet number or connection status changes, with
a periodic heartbeat to keep the pipe warm. Inside WSL, `wsl/gamepad_app.py` parses the
frames with a resynchronising reader that tolerates partial reads and stray bytes,
feeds them to the sinks in `wsl/evdev_sink.py`,
translates each state into a list of Linux evdev events, and writes them to one of
two sinks. The default sink writes the event stream to a FIFO under `/tmp` and
needs no privileges. The opt-in sink creates a real device through `/dev/uinput`
using raw `ioctl` calls so that SDL2 applications and emulators see a normally
attached controller; if `/dev/uinput` is not accessible it prints the one-time
setup instructions and falls back to the FIFO. Both sinks share the same
event-encoding code, so the byte stream they produce is identical.

The uinput sink also advertises `FF_RUMBLE` force feedback. It services the
kernel's force-feedback upload handshake on the uinput descriptor, records the
strong and weak magnitudes of each uploaded rumble effect, and when an effect is
played sends a six-byte rumble frame back to the host over standard output. The
host parses those frames with the shared `RumbleReader` from `core/frames.py`
and drives the physical controller's motors through `XInputSetState`. The FIFO sink has no
reverse channel and therefore no force feedback.

## Binary frame formats

Both gamepad frame formats are defined once in `core/frames.py` and imported by
both sides of the pipe. The gamepad frame is sixteen bytes packed little-endian
as `<BBBBHBBhhhh>`: two
magic bytes (`0xAB`, `0xCD`), the pad index, a flags byte whose low bit marks the
controller as connected, the XInput button bitmask as an unsigned sixteen-bit
value, the two trigger bytes, and the four signed sixteen-bit thumbstick axes.
The rumble frame is six bytes packed as `<BBHH>`: two magic bytes (`0xFB`,
`0xFE`) and the strong and weak motor magnitudes as unsigned sixteen-bit values.
The evdev records the helper writes are standard twenty-four-byte
`struct input_event` values packed as `=qqHHi`, which is exactly what a real
`/dev/input/eventN` node emits, so a single reader is portable across both sinks.

## Package layout and module responsibilities

The code under `src/com2tty/` is organised by where it runs. `__init__.py`
holds the package version, `__main__.py` lets the package run as
`python -m com2tty`, and `bridge.py`/`pad_bridge.py` are the WSL entry shims
described under "The transport".

`cli/` is the user-interface layer: `cli/__init__.py` defines the argument
parser, expands `@profile` tokens, and dispatches to the mode facades;
`cli/profiles.py` loads named argument sets from an INI file.

`core/` contains the dependency-free definitions both interpreters share:
`constants.py` (paths, ports, marker strings, timing), `protocol.py` (the
`[CONTROL]` message catalogue and the host-side `ControlDispatcher`),
`frames.py` (the controller and rumble frame codecs with their
resynchronising readers), and `boards.py` (the USB VID classification and
reset timing data).

`windows/` runs on the Windows interpreter. `wsl_process.py` builds and
supervises the `wsl --exec` helper process; `serial_host.py` owns the COM
port (settings, baud detection via `GetCommState`, hot-plug reconnection,
`ResetProofSerial`); `board_reset.py` implements the per-family reset
sequences; `control_handler.py` holds the handlers behind the control
protocol (dynamic settings, the RFC 2217 session controller, the UF2 upload
controller and flash routine); `bridge_app.py` and `gamepad_app.py`
orchestrate the serial and gamepad sessions (`run_bridge`,
`run_multi_bridge`, `run_with_respawn`, `run_gamepad_bridge`,
`run_multi_gamepad_bridge`); `gamepad_host.py` polls XInput;
`rfc2217_redirector.py` adapts pyserial's RFC 2217 machinery to the pipe;
`uf2_flash.py` locates the bootloader drive; `discovery.py` and `doctor.py`
implement `--list` and `--doctor`. The `windows/os_hacks/` facade isolates
the raw OS-level interventions: `autoplay.py` (registry AutoPlay
suppression with crash recovery), `explorer.py` (closing Explorer windows
on the bootloader drive), `device_watcher.py` (`WM_DEVICECHANGE` wake-ups),
and `console.py` (VT-mode banner colours).

`wsl/` runs on the Linux interpreter inside WSL and uses only the standard
library. `serial_app.py` and `gamepad_app.py` are the helper entry points;
`pty_manager.py` owns the pseudo-terminal primitives; `evdev_sink.py`
implements the `/tmp` FIFO and uinput gamepad sinks; `liveness.py` tracks
session heartbeats and PID markers; `wsl/servers/` contains the
`LoopbackTcpServer` base with the RFC 2217 forwarder and UF2 relay; and
`wsl/integrations/` carries the shell-environment injection and the
picotool interception with its `assets/` wrapper template.

## Dependencies and integration points

The Windows host depends only on `pyserial`. The WSL helper uses only the Python
standard library, so the guest distribution needs nothing beyond `python3` on its
`PATH`. The bridge integrates with PlatformIO by exporting
`PLATFORMIO_UPLOAD_PORT` and `PLATFORMIO_MONITOR_PORT` into the WSL user's shell
startup files for bash, zsh, and fish, and with esptool, bossac, and `picotool`
through the RFC 2217 forwarder and the UF2 relay. On minimal distributions the
`fuser` utility from the `psmisc` package is used to reclaim a TCP port left open
by a previous session; only processes whose command line identifies them as a
com2tty bridge are terminated.

The host-side polling loops (hot-plug reconnect, bootloader-port
acquisition, and `--wait`) sleep through
`windows/os_hacks/device_watcher.py`, which runs a
hidden message-only window registered for `WM_DEVICECHANGE`
device-interface notifications on a daemon thread; a plug or unplug wakes
the loops immediately, and a plain timed sleep is the fallback whenever the
watcher cannot start. With `--auto-respawn`, `run_with_respawn` in
`windows/bridge_app.py` re-runs the bridge entry function after the WSL helper dies,
first polling `check_wsl_environment` until the distribution answers
again, so a `wsl --shutdown` no longer ends the session.

Shared resources are guarded by session-liveness markers so that two
concurrently running sessions cannot reclaim each other's state. Each helper
refreshes a per-port heartbeat file (`/tmp/com2tty_alive_<port>`) from its main
loop, and only after the port has been reclaimed and bound rather than before, so
that a session cannot mistake its own freshly written marker for another live
session. `is_port_session_alive` treats a port as held only when its heartbeat is
both fresh and owned by a PID that is a live com2tty process other than the
caller, and `kill_leftover_listener` then refuses to kill that owner. The rc-file
environment blocks and the fish snippet are tagged with the owning helper's
PID (`[pid=N]` in the marker line), and cleanup removes only blocks that are
owned by the cleaning session, untagged (written by an older version), or owned
by a PID that is no longer a running com2tty process. The picotool interception
records its owner in `/tmp/com2tty_picotool.owner`; startup orphan recovery
leaves the interception in place while that owner is still running. Liveness of a
PID is established by reading `/proc/<pid>/cmdline` and confirming it names a
com2tty helper, so a recycled PID belonging to an unrelated process is not
mistaken for a live session.

Two further guards prevent a misconfigured second invocation from corrupting a
running session. Before performing any shell-rc injection, picotool interception,
or symlink creation, the serial helper probes its RFC 2217 and UF2 relay TCP
ports; if either is already bound it prints an actionable message and exits
without side effects, so a second invocation that reuses the same
`--rfc2217-port` cannot cross-wire its uploads to the first session's board or
overwrite its shell configuration. Separately, `create_symlink_with_fallback`
refuses to replace a tty path that is a symlink to another live session's
pseudo-terminal slave, so the shared default path `/tmp/ttyUSB0` cannot be
silently stolen; a stale or dangling link is replaced as before.

## Security considerations

The RFC 2217 forwarder and the UF2 relay listen on the loopback interface inside
the WSL distribution and perform no authentication. On a single-user machine they
are not reachable from the network, but on a shared or multi-user WSL host any
local user in the same distribution could connect to those ports during an
upload. com2tty should be run only on hosts the operator trusts. The USB serial
number used to locate the bootloader mass-storage drive originates from an
external device descriptor and is treated as untrusted input: it is passed to
PowerShell through an environment variable rather than interpolated into the
script text and is matched as a regular-expression-escaped literal, so a hostile
serial number cannot inject PowerShell or corrupt the match.

Several local-attack surfaces on a shared host are narrowed. The XInput DLL is
loaded by its absolute path under the Windows `System32` directory rather than by
bare name, so a malicious DLL planted in the working directory cannot be loaded
in its place. The gamepad event FIFO and its force-feedback companion are created
with owner-only permissions, so another local user in the WSL distribution cannot
read the input stream or inject events. The shell startup files are rewritten
atomically, by writing a sibling temporary file and renaming it over the
original, so an interrupted cleanup cannot leave a user's `~/.bashrc` or
`~/.zshrc` truncated.

## Known limitations and unverified behaviour

The following items are implemented and covered by unit tests but have not been
verified against the corresponding physical hardware or kernel at the time of
writing.

The SAMD and Leonardo upload path depends on timing parameters in
`acquire_new_port` and `reopen_serial_port`, namely how long to wait for the
bootloader port to appear and for the application port to return. These values
are set from documented behaviour of the Arduino bootloaders and have not been
measured on a physical board.

The uinput force-feedback handshake reads the strong and weak rumble magnitudes
at fixed offsets within `struct uinput_ff_upload` that assume a sixty-four-bit
(LP64) kernel ABI. The offsets are derived from the kernel structure layout but
have not been confirmed on a running WSL kernel with `fftest`. The uinput sink
already refuses to operate on a non-LP64 interpreter and falls back to the FIFO
stream.

The STM32 reset assumes the conventional wiring in which RTS drives NRST and DTR
drives BOOT0; boards wired differently reset but may not enter the ROM
bootloader, which is the documented reason `--board none` exists.

The Guide button is reported only when the loaded XInput DLL exports
`XInputGetStateEx`; on systems where only the legacy `xinput9_1_0` DLL is present
it reads as zero. Force feedback is unsupported in the FIFO stream tier because
that tier has no reverse channel. These two limitations are inherent to the
approach rather than defects.
