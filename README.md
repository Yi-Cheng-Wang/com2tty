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
  - [Bridging a serial port](#bridging-a-serial-port)
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
must provide `python3` on its `PATH`. The WSL helper uses only the Python
standard library and therefore needs no additional packages inside WSL.

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

`com2tty` is configured entirely through command-line arguments. There are no
configuration files and no environment variables that the tool itself reads.
Note that in serial mode the WSL helper writes environment variables into the
WSL user's `~/.bashrc`; this behaviour is described in
[Firmware upload through the bridge](#firmware-upload-through-the-bridge).

The first positional argument is the COM port. It is required in serial mode and
is omitted in gamepad mode, which is selected with `--gamepad`.

### Options common to both modes

The following option applies to both modes.

```text
-d, --debug            Enable verbose debug logging on standard error.
```

### Serial-mode options

```text
port                   Windows COM port to bridge, for example COM3. Required
                       unless --gamepad is given.
-b, --baud BAUD        Baud rate, or the literal value "auto" to detect the
                       rate Windows has configured for the port
                       (default: auto; falls back to 9600 if detection fails).
-w, --wsl-tty PATH     Target symlink path created inside WSL
                       (default: /tmp/ttyUSB0).
--rfc2217-port PORT    TCP port for the in-WSL RFC 2217 forwarder
                       (default: 4000). The UF2 relay uses PORT + 1.
--bytesize {5,6,7,8}   Serial byte size (default: 8).
--parity {N,E,O,S,M}   Parity: none, even, odd, space, or mark (default: N).
--stopbits {1,1.5,2}   Stop bits (default: 1).
--xonxoff              Enable software flow control (XON/XOFF).
--rtscts               Enable hardware flow control (RTS/CTS).
--dsrdtr               Enable hardware flow control (DSR/DTR).
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
`~/.bashrc`: `PLATFORMIO_UPLOAD_PORT` is set to
`rfc2217://127.0.0.1:<rfc2217-port>` and `PLATFORMIO_MONITOR_PORT` is set to the
serial symlink path. Because these variables are written to `~/.bashrc`, open a
new WSL shell or run `source ~/.bashrc` after starting com2tty for them to take
effect. The variables are removed when com2tty exits.

Second, com2tty detects the connected board type from its USB vendor identifier
and performs the appropriate hardware reset on the Windows side. For ESP32-class
boards it performs the DTR and RTS auto-reset sequence to enter the download
mode. For RP2040 and RP2350 boards it performs the 1200-baud touch that triggers
the BOOTSEL mass-storage mode.

Third, for RP2040 and RP2350 boards, com2tty intercepts the `picotool` invocation
inside WSL. When PlatformIO calls `picotool` to flash a `.uf2` image, a wrapper
transfers the image back to the Windows host over a relay that listens on
`127.0.0.1:<rfc2217-port + 1>`. The host then triggers BOOTSEL mode, locates the
board's mass-storage drive, verifies the transferred image against an MD5
checksum, and writes the image to the drive. The original `picotool` is restored
when com2tty exits.

These mechanisms operate without any additional flags. The startup banner reports
the detected board type, the RFC 2217 port, the UF2 relay port, and the board's
USB serial number.

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
and `BTN_THUMBR`. The sticks are reported as `ABS_X` and `ABS_Y` for the left
stick and `ABS_RX` and `ABS_RY` for the right stick, each spanning the signed
16-bit range. The triggers are reported as `ABS_Z` for the left trigger and
`ABS_RZ` for the right trigger, each spanning 0 to 255. The directional pad is
reported as `ABS_HAT0X` and `ABS_HAT0Y` with values of -1, 0, or 1. The stick Y
axes are inverted to follow the Linux convention in which pushing up produces a
negative value.

The forwarded signal matches a real controller at the level of these event codes,
ranges, and resolutions, but it is not bit-for-bit identical to a controller
driven by the kernel `xpad` driver. The timing and latency differ because the
path is polled and piped rather than delivered by a fixed USB interrupt interval.
The Guide button is not reported, because the standard XInput state query does not
expose it. Force feedback is not implemented. These differences are inherent to
the approach.

## Architecture overview

The package is organised around a host process on Windows and a helper process
inside WSL connected by the standard input and output streams of the helper.

`cli.py` parses the command line and dispatches to one of two entry functions in
`host.py`. In serial mode it calls `run_bridge`; in gamepad mode it calls
`run_gamepad_bridge`. `__main__.py` and the console entry point both call
`cli.main`, and `__init__.py` holds the package version.

`host.py` is the Windows side. In serial mode `run_bridge` opens the COM port with
`pyserial`, spawns the WSL helper with `wsl python3 -u bridge.py`, and runs three
threads: one relays bytes from the COM port to the helper's standard input, one
relays bytes from the helper's standard output to the COM port, and one reads the
helper's standard error. The standard error stream carries a line-oriented control
protocol whose messages are prefixed with `[CONTROL]`; these messages drive
dynamic serial-setting changes, the RFC 2217 session lifecycle, and the UF2 upload
sequence. `host.py` also contains the board detection and reset logic and the
routine that writes a transferred UF2 image to the correct Windows drive.

`bridge.py` is the WSL side for serial forwarding. It creates a pseudo terminal
with `openpty`, symlinks the requested path to the pseudo-terminal slave, falling
back to `/tmp` if the requested path is not writable, and runs a `select` loop
that relays data between the helper's standard input and output and the
pseudo-terminal master. It also starts the RFC 2217 forwarder thread and the UF2
relay thread, writes the PlatformIO environment variables into `~/.bashrc`, and
installs the `picotool` interceptor. `rfc2217_server.py` provides the redirector
that implements the RFC 2217 protocol for the forwarder.

The gamepad path reuses the same spawn-and-pipe transport. `xinput.py` is the
Windows side: it polls an XInput controller slot through `ctypes` and packs each
state snapshot into a fixed 16-byte frame, sending a frame only when the state
changes. `pad_bridge.py` is the WSL side: it parses the frames, translates them
into evdev events, and writes them to one of two sinks. The default sink writes to
a `/tmp` FIFO, and the opt-in sink creates a real device through `/dev/uinput`
using raw `ioctl` calls. Both sinks share the same event-encoding code, so the
byte stream they produce is identical.

## Development setup

Install the package in editable mode together with the test tools.

```bash
pip install -e .
pip install pytest pytest-cov
```

Run the test suite with coverage.

```bash
pytest --cov=src/com2tty --cov-report=term-missing tests/
```

A passing run reports all tests passing and full line coverage for the package.
The test suite is cross-platform. The `tests/conftest.py` file substitutes a mock
`termios` module on Windows so that the WSL-side modules import for testing, and
the platform-specific system calls used by the gamepad sinks are mocked so that
the suite runs on both Windows and Linux.

Continuous integration is defined in `.github/workflows/ci.yml`. It runs the test
suite on `windows-latest` and `ubuntu-latest` against Python 3.8, 3.9, 3.10,
3.11, and 3.12, and it enforces 100 percent line coverage by running pytest with
`--cov-fail-under=100`. A separate job publishes the package to PyPI on pushes to
the `main` branch.

## Contributing

Base feature branches on the `develop` branch. Commit messages follow the
Conventional Commits format, for example `feat(gamepad): ...` or
`test(host): ...`, as established in the project history. Every change must keep
the test suite passing with 100 percent line coverage on both Windows and Ubuntu
across the supported Python versions, because continuous integration enforces
this. Add or update tests for any behavioural change. Open pull requests against
`develop`.

## Troubleshooting

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
