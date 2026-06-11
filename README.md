# com2tty

`com2tty` is a Python package that runs on a Windows host and forwards a device
attached to Windows into a Windows Subsystem for Linux (WSL) instance, where it
appears as a native Linux device. It supports two kinds of forwarding. The first
forwards a Windows COM port into WSL as a virtual serial device such as
`/tmp/ttyUSB0`. The second forwards a Windows XInput game controller into WSL as
a Linux evdev gamepad. Both kinds use the same transport: a low-latency,
firewall-resilient bridge built on standard input and output redirection between
the Windows host process and a helper process running inside WSL. No network
configuration, port forwarding, or firewall change is required.

The intended users are developers who work inside WSL but whose hardware is bound
to the Windows host: embedded developers who flash and monitor microcontrollers
over USB-to-serial adapters, and developers who need a game controller available
to Linux tools running in WSL.

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Listing available ports](#listing-available-ports)
  - [Bridging a serial port](#bridging-a-serial-port)
  - [Hot-plug auto-reconnect](#hot-plug-auto-reconnect)
  - [Bridging multiple ports](#bridging-multiple-ports)
  - [Argument profiles](#argument-profiles)
  - [Automatic baud-rate detection](#automatic-baud-rate-detection)
  - [Configuring /dev/ttyUSB0 in WSL](#configuring-devttyusb0-in-wsl)
  - [Firmware upload through the bridge](#firmware-upload-through-the-bridge)
  - [Gamepad mode](#gamepad-mode)
- [Architecture overview](#architecture-overview)
- [Development setup](#development-setup)
- [Contributing](#contributing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Requirements

The Windows host requires Python 3.8 or later. The `pyserial` package, version
3.5 or later, is the only runtime dependency and is installed automatically with
the package. A working WSL installation is required, and the WSL distribution
must provide `python3` on its `PATH`. By default the WSL default distribution
is used; a specific one can be selected with `--distro`. The WSL helper uses
only the Python standard library and therefore needs no additional packages
inside WSL. These prerequisites are verified at startup and a specific,
actionable error is reported when one is missing.

Serial forwarding requires a COM port that Windows can open. Gamepad forwarding
requires a controller that the Windows XInput driver recognises, which is the
standard case for Xbox and XInput-compatible controllers. The opt-in gamepad tier
that creates a real Linux input device additionally requires a one-time
privileged setup inside WSL, described in [Gamepad mode](#gamepad-mode).

## Installation

Install the released package from PyPI on the Windows host.

```cmd
pip install com2tty
```

Alternatively, install from a checkout of the source by running the following
in the project root.

```cmd
pip install .
```

To work on the package itself, install it in editable mode.

```cmd
pip install -e .
```

Installation registers a console entry point named `com2tty`. If the entry point
is not on your `PATH`, the package can also be invoked as a module with
`python -m com2tty`.

## Configuration

`com2tty` is configured through command-line arguments, optionally saved as
named profiles in an INI file (see
[Argument profiles](#argument-profiles)). Note that in serial mode the WSL
helper writes environment variables into the WSL user's shell configuration;
this behaviour is described in
[Firmware upload through the bridge](#firmware-upload-through-the-bridge).

The positional arguments are the COM ports. At least one is required in
serial mode; several may be given to bridge them concurrently. The port is
omitted in gamepad mode, which is selected with `--gamepad`, and in the
`--list` and `--version` modes.

### Options common to both modes

The following options apply to both modes.

```text
--version              Print the com2tty version and exit.
-l, --list             List the serial ports Windows can see (device name,
                       VID:PID, USB bus id, serial number, detected board,
                       description) and exit.
-d, --debug            Enable verbose debug logging on standard error.
--distro NAME          WSL distribution to use (default: the WSL default
                       distribution). Useful when the default distribution
                       lacks python3, for example docker-desktop.
```

### Serial-mode options

```text
port [port ...]        Windows COM port(s) to bridge, for example COM3, or
                       COM3 COM5 to bridge two ports at once. Required
                       unless --gamepad or --list is given.
-b, --baud BAUD        Baud rate, or the literal value "auto" to detect the
                       rate Windows has configured for the port
                       (default: auto; falls back to 9600 if detection fails).
-w, --wsl-tty PATH     Target symlink path created inside WSL
                       (default: /tmp/ttyUSB0). With multiple ports, each
                       additional port increments the trailing number.
--rfc2217-port PORT    TCP port for the in-WSL RFC 2217 forwarder
                       (default: 4000). The UF2 relay uses PORT + 1; with
                       multiple ports, each additional port uses PORT + 2i.
--bytesize {5,6,7,8}   Serial byte size (default: 8).
--parity {N,E,O,S,M}   Parity: none, even, odd, space, or mark (default: N).
--stopbits {1,1.5,2}   Stop bits (default: 1).
--xonxoff              Enable software flow control (XON/XOFF).
--rtscts               Enable hardware flow control (RTS/CTS).
--dsrdtr               Enable hardware flow control (DSR/DTR).
--board {auto,esp32,pico,nrf52,samd,stm32,none}
                       Override USB VID board detection for reset and upload
                       handling (default: auto). Use this when a board uses a
                       USB-UART chip that com2tty does not recognise; "none"
                       disables board-specific reset sequences entirely.
```

### Gamepad-mode options

```text
--gamepad              Select gamepad mode. No COM port is required.
--pad-index {0,1,2,3}  XInput controller slot to forward (default: 0).
--pad-name NAME        Device name advertised inside WSL
                       (default: "Microsoft X-Box 360 pad").
--uinput               Create a real /dev/input device through /dev/uinput
                       instead of the default /tmp event stream.
--wsl-pad PATH         FIFO path for the default /tmp event stream
                       (default: /tmp/com2pad0).
--poll-hz HZ           XInput polling rate in hertz (default: 250). Frames are
                       sent only when the controller state changes.
```

## Usage

Run `com2tty` from any Windows terminal, either PowerShell or Command Prompt. The
process runs in the foreground and is stopped with Ctrl+C.

### Listing available ports

`com2tty --list` (or `-l`) enumerates every serial port Windows can see,
without requiring usbipd or administrator rights.

```text
> com2tty --list
Device  VID:PID    Bus ID  Serial number     Board    Description
------  ---------  ------  ----------------  -------  -----------------------
COM3    -          -       -                 unknown  Bluetooth serial (COM3)
COM17   2E8A:F00F  1-6     98C4FFA253A63FB7  pico     USB serial device (COM17)
```

The `Bus ID` column is the USB bus location (for example `1-6` or `2-6`), read
directly from the device descriptor. The `Board` column shows the family that
VID-based detection would assign, which is the same detection the bridge uses
for reset and upload handling.

### Bridging a serial port

Bridge `COM3` to the default WSL path `/tmp/ttyUSB0` at 115200 baud.

```cmd
com2tty COM3 --baud 115200
```

Bridge `COM5` to a custom WSL device path at 9600 baud.

```cmd
com2tty COM5 --baud 9600 -w /tmp/my_device
```

While the bridge is active, a Linux program inside WSL opens the symlinked path
and reads from and writes to it as if it were a local serial device. Data is
relayed in both directions between the Windows COM port and the WSL pseudo
terminal. Dynamic changes that a WSL program makes to the line settings, such as
the baud rate, are detected and applied to the underlying Windows COM port.

### Hot-plug auto-reconnect

When the bridged device is unplugged, resets, or re-enumerates, the bridge
does not need to be restarted. After a short grace period for transient
errors (such as a board rebooting into its bootloader), com2tty closes the
stale Windows handle and waits for the device to come back, first under its
original COM name and then by scanning for its USB serial number, because
Windows may assign a different COM number after a replug. Once the device
reappears the bridge resumes automatically; the WSL endpoint stays in place
the whole time.

### Bridging multiple ports

Passing several ports bridges them all from one invocation.

```cmd
com2tty COM3 COM5
```

Each port gets its own WSL helper and endpoint: the symlink path is derived
from `--wsl-tty` by incrementing its trailing number (`/tmp/ttyUSB0`,
`/tmp/ttyUSB1`, ...), and the RFC 2217 port is the base value plus two per
additional port (each bridge also reserves its port plus one for the UF2
relay). Only the first port writes the PlatformIO environment variables and
intercepts `picotool`, so concurrent bridges do not overwrite each other's
shell configuration; tools targeting a secondary port can use its RFC 2217
port directly.

### Argument profiles

Frequently used argument sets can be saved as named profiles in an INI file,
either `com2tty.ini` in the current directory or `.com2tty.ini` in the user
profile directory. Keys are the long option names (dashes and underscores are
both accepted), `port` supplies the positional argument, and boolean keys
take `true` or `false`.

```ini
[myboard]
port = COM5
baud = 115200
wsl-tty = /tmp/my_device
board = pico

[pad]
gamepad = true
uinput = true
```

A profile is invoked with an `@` prefix, and arguments given after the token
override the profile's values.

```cmd
com2tty @myboard
com2tty @myboard --baud 9600
com2tty @pad
```

### Automatic baud-rate detection

When the baud rate is left at its default value of `auto`, com2tty queries the
rate that Windows has configured for the port and uses it. If detection fails,
the bridge falls back to 9600 baud. To set the rate explicitly, pass a numeric
value to `--baud`.

```cmd
com2tty COM3 --baud auto
```

### Configuring /dev/ttyUSB0 in WSL

In Linux the `/dev` directory is owned by `root`. Running com2tty as an ordinary
Windows user means the WSL helper cannot create a symlink directly under `/dev`.
For this reason the default target is `/tmp/ttyUSB0`, which is user-writable, and
com2tty never requires elevated privileges at run time. If a path under `/dev` is
requested and permission is denied, the helper automatically falls back to the
equivalent path under `/tmp` and prints instructions.

To expose the device at a stable `/dev` path without granting com2tty privileges,
create a one-time symlink inside WSL that points from `/dev` to the stable `/tmp`
path.

```bash
sudo ln -sf /tmp/ttyUSB0 /dev/ttyUSB0
```

Each time com2tty starts, it repoints `/tmp/ttyUSB0` at the active pseudo
terminal, so `/dev/ttyUSB0` continues to resolve correctly. After this one-time
step, WSL programs such as `minicom`, `screen`, the ESP-IDF tools, or Python
scripts can use `/dev/ttyUSB0` directly.

### Firmware upload through the bridge

In serial mode com2tty additionally supports flashing microcontroller firmware
from build tools running inside WSL, so that a PlatformIO project in WSL can
upload to a board attached to Windows. This support is enabled by default and
involves three mechanisms.

First, the WSL helper starts an RFC 2217 forwarder that listens on
`127.0.0.1:<rfc2217-port>` inside WSL, where the port defaults to 4000. To make
PlatformIO use it, the helper appends environment variables to the WSL user's
shell startup file: `PLATFORMIO_UPLOAD_PORT` is set to
`rfc2217://127.0.0.1:<rfc2217-port>` and `PLATFORMIO_MONITOR_PORT` is set to the
serial symlink path. The variables are written to `~/.bashrc`, to `~/.zshrc`
when zsh is in use or that file exists, and to
`~/.config/fish/conf.d/com2tty.fish` when fish is in use or its configuration
directory exists. Open a new WSL shell or run `source ~/.bashrc` (or
`source ~/.zshrc`; fish picks the snippet up automatically) after starting
com2tty for them to take effect. The variables are removed when com2tty exits;
if a session is killed before it can clean up, the next run removes the stale
block on startup.

Second, com2tty detects the connected board type from its USB vendor identifier
and performs the appropriate hardware reset on the Windows side. For ESP32-class
boards it performs the DTR and RTS auto-reset sequence to enter the download
mode. For RP2040 and RP2350 boards, and for Adafruit nRF52 boards with the UF2
bootloader, it performs the 1200-baud touch that triggers the mass-storage
bootloader mode. For Arduino Leonardo- and SAMD-class boards it performs the
same 1200-baud touch; because their bootloader re-enumerates as a separate
serial port (often with a different USB identity than the application port),
com2tty snapshots the port list before the touch, waits for the new bootloader
port to appear, opens it, and runs the upload against it over RFC 2217. When the
upload finishes and the board reboots into the application, com2tty restores and
reopens the original application port. For STM32-class boards it pulses
DTR and RTS in the conventional BOOT0/NRST wiring so the board resets around
an upload; boards flashed through ST-LINK or DFU are unaffected by the pulse.

Third, for UF2-bootloader boards (RP2040, RP2350, and nRF52), com2tty
intercepts the `picotool` invocation
inside WSL. When PlatformIO calls `picotool` to flash a `.uf2` image, a wrapper
transfers the image back to the Windows host over a relay that listens on
`127.0.0.1:<rfc2217-port + 1>`. The host then triggers BOOTSEL mode, locates the
board's mass-storage drive, verifies the transferred image against an MD5
checksum, and writes the image to the drive. The original `picotool` is restored
when com2tty exits; if a session is killed before it can restore it, the next run
detects and reverses the leftover interception on startup, so PlatformIO uploads
are not left broken.

These mechanisms operate without any additional flags. The startup banner reports
the detected board type, the RFC 2217 port, the UF2 relay port, and the board's
USB serial number. If the detected board type is wrong (for example a board whose
USB-UART chip is not recognised), override it with `--board`.

The RFC 2217 forwarder and the UF2 relay listen on the loopback interface
(`127.0.0.1`) inside the WSL distribution and perform no authentication. On a
single-user machine this is not exposed to the network, but on a shared or
multi-user WSL host any local user in the same distribution could connect to
these ports during an upload. Run com2tty only on hosts you trust, and choose a
non-default `--rfc2217-port` if another local service needs the default port. To
reclaim a port left open by a previous com2tty session, the helper only
terminates processes whose command line identifies them as a com2tty bridge; an
unrelated service occupying the port is never killed.

### Gamepad mode

Gamepad mode forwards a Windows XInput controller into WSL. It exists because
forwarding a controller with `usbipd` does not work in a default WSL2 setup: the
stock WSL2 kernel is built without the `xpad` driver, so an attached controller
is enumerated but never produces a usable input device. Gamepad mode keeps the
controller on Windows, where the native XInput driver handles it, reads its state
on the Windows side, and streams that state through the same bridge used for
serial forwarding. Inside WSL a helper, which uses only the Python standard
library, turns the state into a Linux evdev `input_event` stream describing a
Microsoft X-Box 360 pad, identified by USB vendor 0x045e and product 0x028e.

The controller must be visible to Windows XInput. If the controller has been
bound or attached with `usbipd`, Windows no longer owns it and XInput reports no
controller; unbind it from `usbipd` so that Windows holds the controller before
using gamepad mode.

Gamepad mode provides two tiers. Both emit the identical evdev byte stream, so a
single reader works against either, and com2tty itself never requires elevated
privileges at run time.

#### Default tier: the /tmp event stream

The default tier writes the evdev event stream to a FIFO under `/tmp`, by default
`/tmp/com2pad0`, and requires no privileged setup.

```cmd
com2tty --gamepad
```

A consumer inside WSL reads 24-byte Linux `input_event` records from the FIFO and
interprets them using the device profile below. This tier is suited to programs
that read the stream directly. Standard applications and game engines that
enumerate `/dev/input` devices do not read a FIFO and require the uinput tier.

#### Opt-in tier: a real device through /dev/uinput

The opt-in tier creates a real system-wide device under `/dev/input` so that SDL2
applications, emulators, and tools such as `evtest` recognise a normally attached
controller.

```cmd
com2tty --gamepad --uinput
```

If `/dev/uinput` is not accessible, com2tty prints the one-time setup instructions
and automatically falls back to the `/tmp` event stream so that forwarding
continues to work.

#### One-time setup for the uinput tier

Creating a real input device requires access to `/dev/uinput`, which Linux
restricts to `root`, and reading the resulting `/dev/input/event*` node requires
membership of the `input` group. Both are granted once, inside WSL, and com2tty
still runs without privileges thereafter. Either use `sudo`, or run the commands
as root from Windows with `wsl -u root`, which requires no password.

```bash
sudo modprobe uinput
sudo chmod 0666 /dev/uinput
sudo usermod -aG input "$USER"
```

The permission granted by `chmod` does not survive `wsl --shutdown`. To make it
persist, add a boot command to `/etc/wsl.conf`, which runs as root on every WSL
start.

```ini
[boot]
command = modprobe uinput && chmod 0666 /dev/uinput
```

After editing `/etc/wsl.conf`, run `wsl --shutdown` once from Windows. This also
refreshes the group membership granted by `usermod`.

The stock WSL2 kernel sets `CONFIG_INPUT_UINPUT` as a module, which works, but
does not set `CONFIG_INPUT_JOYDEV`. As a result the legacy `/dev/input/js*` node
is absent. This is not a problem for modern applications and SDL2, which read
`/dev/input/event*` directly.

#### Verifying the uinput tier

With com2tty running in `--uinput` mode, confirm the device inside WSL with
`evtest`.

```bash
sudo apt install evtest
evtest
```

Select the Microsoft X-Box 360 pad device, then move the sticks and press buttons
on Windows and observe the events appear in WSL.

#### Device profile

Both tiers emit the same evdev codes. Buttons are reported as `BTN_A`, `BTN_B`,
`BTN_X`, `BTN_Y`, `BTN_TL`, `BTN_TR`, `BTN_SELECT`, `BTN_START`, `BTN_THUMBL`,
`BTN_THUMBR`, and `BTN_MODE` for the Guide (Xbox logo) button. The sticks are
reported as `ABS_X` and `ABS_Y` for the left
stick and `ABS_RX` and `ABS_RY` for the right stick, each spanning the signed
16-bit range. The triggers are reported as `ABS_Z` for the left trigger and
`ABS_RZ` for the right trigger, each spanning 0 to 255. The directional pad is
reported as `ABS_HAT0X` and `ABS_HAT0Y` with values of -1, 0, or 1. The stick Y
axes are inverted to follow the Linux convention in which pushing up produces a
negative value.

In the uinput tier the virtual pad also advertises `FF_RUMBLE` force
feedback. When a game or emulator inside WSL plays a rumble effect, the
effect's magnitudes travel back through the bridge to the Windows host, which
drives the physical controller's motors through `XInputSetState`. The strong
(left, low-frequency) and weak (right, high-frequency) motors map directly to
their XInput counterparts. The `/tmp` stream tier has no reverse channel and
therefore no force feedback.

The forwarded signal matches a real controller at the level of these event codes,
ranges, and resolutions, but it is not bit-for-bit identical to a controller
driven by the kernel `xpad` driver. The timing and latency differ because the
path is polled and piped rather than delivered by a fixed USB interrupt interval.
The Guide button is read through the undocumented `XInputGetStateEx` call and is
reported as zero on systems where only the legacy `xinput9_1_0` DLL is
available. These differences are inherent to the approach.

## Architecture overview

The package is organised around a host process on Windows and a helper process
inside WSL connected by the standard input and output streams of the helper.

`cli.py` parses the command line (after `profiles.py` expands any `@profile`
tokens) and dispatches to an entry function in `host.py`: `run_bridge` in
serial mode, `run_multi_bridge` when several ports are given, and
`run_gamepad_bridge` in gamepad mode. `discovery.py` implements `--list`.
`__main__.py` and the console entry point both call `cli.main`, and
`__init__.py` holds the package version.

`host.py` is the Windows side. In serial mode `run_bridge` opens the COM port with
`pyserial`, spawns the WSL helper with `wsl python3 -u bridge.py`, and runs three
threads: one relays bytes from the COM port to the helper's standard input, one
relays bytes from the helper's standard output to the COM port, and one reads the
helper's standard error. The standard error stream carries a line-oriented control
protocol whose messages are prefixed with `[CONTROL]`; these messages drive
dynamic serial-setting changes, the RFC 2217 session lifecycle, and the UF2 upload
sequence. `host.py` also contains the hot-plug reconnect logic and the routine
that writes a transferred UF2 image to the correct Windows drive. The board
detection and reset sequences live in `boards.py`, the UF2 drive lookup and
AutoPlay suppression in `uf2.py`, and the console colour handling in
`banner.py`; `host.py` re-exports these names for backwards compatibility.

`bridge.py` is the WSL side for serial forwarding. It creates a pseudo terminal
with `openpty`, symlinks the requested path to the pseudo-terminal slave, falling
back to `/tmp` if the requested path is not writable, and runs a `select` loop
that relays data between the helper's standard input and output and the
pseudo-terminal master. It also starts the RFC 2217 forwarder thread and the UF2
relay thread, writes the PlatformIO environment variables into `~/.bashrc`, and
installs the `picotool` interceptor. `rfc2217_server.py` provides the redirector
that implements the RFC 2217 protocol for the forwarder.

The gamepad path reuses the same spawn-and-pipe transport. `xinput.py` is the
Windows side: it polls an XInput controller slot through `ctypes` (preferring
the `XInputGetStateEx` export so the Guide button is visible) and packs each
state snapshot into a fixed 16-byte frame, sending a frame only when the state
changes. `pad_bridge.py` is the WSL side: it parses the frames, translates them
into evdev events, and writes them to one of two sinks. The default sink writes to
a `/tmp` FIFO, and the opt-in sink creates a real device through `/dev/uinput`
using raw `ioctl` calls. Both sinks share the same event-encoding code, so the
byte stream they produce is identical. In the uinput sink the helper also
services the kernel's force-feedback upload handshake and streams played
rumble effects back over its stdout, where the host applies them to the
physical controller with `XInputSetState`.

For a detailed account of the control protocol, the board reset sequences, the
reconnection model, the binary frame formats, and the known hardware-unverified
behaviour, see the [architecture document](ARCHITECTURE.md).

## Development setup

Install the package in editable mode together with the test tools.

```bash
pip install -e .
pip install pytest pytest-cov ruff
```

Run the test suite with coverage, and the linter.

```bash
pytest --cov=src/com2tty --cov-report=term-missing tests/
ruff check src tests scripts
```

A passing run reports all tests passing and full line coverage for the package.
The test suite is cross-platform. The `tests/conftest.py` file substitutes a mock
`termios` module on Windows so that the WSL-side modules import for testing, and
the platform-specific system calls used by the gamepad sinks are mocked so that
the suite runs on both Windows and Linux.

Continuous integration is defined in `.github/workflows/ci.yml`. It runs a ruff
lint job and the test suite on `windows-latest` and `ubuntu-latest` against
Python 3.8 through 3.13, and it enforces 100 percent line coverage by running
pytest with `--cov-fail-under=100`. A separate job publishes the package to PyPI
on pushes to the `main` branch.

Because CI has no WSL or serial hardware, a manual end-to-end smoke test lives
at `scripts/e2e_smoke.py`. Run it on a real Windows host with a device attached
before a release: it checks `--list`, starts a bridge, verifies the symlink and
the RFC 2217 forwarder inside WSL, and optionally verifies an echo round-trip
when the device has TX wired to RX (`--loopback`).

```cmd
python scripts/e2e_smoke.py --port COM17
```

## Contributing

Base feature branches on the `develop` branch and open pull requests against it.
Commit messages follow the Conventional Commits format, for example
`feat(gamepad): ...` or `test(host): ...`, as established in the project history.
Every change must keep the test suite passing with 100 percent line coverage on
both Windows and Ubuntu across the supported Python versions, and must pass the
ruff linter, because continuous integration enforces both. Add or update tests
for any behavioural change.

The full contribution process, including how to report bugs, the commit
convention, the linting requirements, and the manual end-to-end verification
step, is documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

At startup com2tty verifies the WSL environment and reports a specific error if
a prerequisite is missing. The checks and their remedies are:

- `wsl.exe` is not on `PATH`: install WSL with `wsl --install` from an elevated
  prompt and reboot if requested.
- `python3` is not available in the selected distribution: the WSL default
  distribution may not be a regular Linux distribution (for example
  `docker-desktop`). List distributions with `wsl -l -v` and either select a
  suitable one with `--distro`, or install Python inside WSL with
  `sudo apt install python3`.
- The bridge script is not readable from WSL: Windows drive automounting is
  disabled. Ensure `/etc/wsl.conf` does not disable the `[automount]` section,
  then restart WSL with `wsl --shutdown`.

If the helper reports that the RFC 2217 or UF2 relay port could not be bound,
another process inside WSL is holding the TCP port. com2tty attempts to clean up
leftover listeners automatically using `fuser`, which ships in the `psmisc`
package; on minimal distributions install it with `sudo apt install psmisc`, or
select a different port with `--rfc2217-port` (the UF2 relay always uses that
port plus one).

The serial-mode environment variables are written to `~/.bashrc`, to `~/.zshrc`
when zsh is detected or a `~/.zshrc` file exists, and to
`~/.config/fish/conf.d/com2tty.fish` when fish is detected. Users of other
shells must export `PLATFORMIO_UPLOAD_PORT` and `PLATFORMIO_MONITOR_PORT`
manually.

The startup banner uses ANSI colours only when standard output is an
interactive terminal that supports them; set the `NO_COLOR` environment
variable to suppress colours entirely.

If a board is not detected (the banner shows `Unknown`), its USB-UART chip is
not in the VID whitelist. Check what detection sees with `com2tty --list`, then
force the board family with `--board` (`esp32`, `pico`, `nrf52`, `samd`, or
`stm32`) so that reset and upload handling still work.

If the WSL helper reports a permission error while creating the serial symlink,
the requested path under `/dev` is not writable; the helper falls back to `/tmp`
and prints the one-time command to link the `/dev` path to it.

If the serial port reports that it is busy or access is denied, ensure no other
Windows application, such as a serial monitor or a second com2tty instance, is
holding the COM port open.

If gamepad mode reports all values as zero, Windows XInput is not receiving the
controller. Confirm the controller is not bound or attached through `usbipd`, so
that Windows owns it, and confirm it is on the expected XInput slot, which can be
changed with `--pad-index`.

If the `--uinput` tier cannot open `/dev/uinput`, complete the one-time setup
described in [Gamepad mode](#gamepad-mode). Until then, com2tty falls back to the
`/tmp` event stream.

For detailed logs and transfer statistics, run com2tty with `-d` or `--debug`.

```cmd
com2tty COM3 --debug
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file
for the full text.
