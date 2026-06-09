# com2tty

`com2tty` is a Python package designed to forward serial communications from a Windows COM port into a WSL (Windows Subsystem for Linux) instance, presenting it as a virtual `ttyUSB` (or similar) serial device in WSL. 

It does this using a low-latency, firewall-resilient **process-pipe bridge** over standard input/output redirection. It requires **no network or firewall configuration**.

## Architecture Overview

1. The Windows host process opens the physical COM port (using `pyserial`).
2. It spawns the WSL Python bridge helper in the background, redirecting its stdin/stdout.
3. Inside WSL, the bridge helper opens a pseudo-terminal (PTY) and creates a symbolic link to the PTY's slave file.
4. Data is piped bidirectionally between the physical Windows COM port and the WSL virtual device.

```
[Windows COM Port] <--> [com2tty host] <--> (stdin/stdout pipe) <--> [wsl bridge] <--> [PTY Slave /tmp/ttyUSB0]
```

## Requirements

- **Windows Host**: Python 3.8+ and `pyserial` (installed automatically).
- **WSL Guest**: Python 3.x (uses standard library modules only, no dependencies required).

## Installation

Install the package on the Windows host by running the following command in the project root:

```cmd
pip install .
```

For development, you can install it in editable mode:

```cmd
pip install -e .
```

## Usage

Run `com2tty` from any Windows terminal (PowerShell or Command Prompt).

```cmd
com2tty <COM_PORT> [options]
```

### Examples

Bridge **COM3** to the default WSL path `/tmp/ttyUSB0` at 115200 baud:
```cmd
com2tty COM3 --baud 115200
```

Bridge **COM5** to a custom WSL device path `/tmp/my_device`:
```cmd
com2tty COM5 --baud 9600 -w /tmp/my_device
```

### Options

```
positional arguments:
  port                  Windows COM port to connect to (e.g. COM3).

options:
  -h, --help            show this help message and exit
  -b BAUD, --baud BAUD  Baud rate for the serial port (default: 9600).
  -w WSL_TTY, --wsl-tty WSL_TTY
                        Target symlink path inside WSL (default: /tmp/ttyUSB0).
  --bytesize {5,6,7,8}  Serial byte size (default: 8).
  --parity {N,E,O,S,M}  Serial parity: None, Even, Odd, Space, Mark (default: N).
  --stopbits {1,1.5,2}  Serial stop bits: 1, 1.5, or 2 (default: 1).
  --xonxoff             Enable software flow control (XON/XOFF).
  --rtscts              Enable hardware flow control (RTS/CTS).
  --dsrdtr              Enable hardware flow control (DSR/DTR).
  -d, --debug           Enable debug logging output.
```

---

## Gamepad Mode: forward an Xbox / XInput controller into WSL

`usbipd` forwards a *raw USB device* into the WSL2 kernel, which then has to
enumerate it and own the right driver (`xpad`). The stock WSL2 kernel ships
**without** `CONFIG_JOYSTICK_XPAD`, so attaching an Xbox controller fails — the
guest sees the USB device but never produces a usable `/dev/input/js*`.

Gamepad mode sidesteps this entirely. Windows keeps doing what it is good at
(USB enumeration + the rock-solid XInput driver); com2tty reads the controller
state on the Windows side and streams it through the **same firewall-free
stdin/stdout pipe** the serial bridge uses. Inside WSL, a tiny helper (Python
**standard library only** — no `evdev`, no extra packages) turns it into a
Linux **evdev event stream** for a "Microsoft X-Box 360 pad" (VID `045e`,
PID `028e`).

Just like the serial bridge — which defaults to a user-writable `/tmp/ttyUSB0`
and only touches `/dev` when *you* choose to elevate — gamepad mode has **two
tiers**:

| | Endpoint | Root needed | Who can consume it |
|---|---|---|---|
| **Default (`--gamepad`)** | FIFO `/tmp/com2pad0` | **None** | anything that reads the evdev stream from that path |
| **Opt-in (`--gamepad --uinput`)** | real `/dev/input/event*` | one-time setup | **any** SDL2 game / emulator / `evtest`, system-wide |

```
[Xbox controller] -> Windows XInput driver -> [com2tty host] -> stdin/stdout pipe
   -> [WSL helper] --+-- default:  /tmp/com2pad0   (evdev byte stream, no root)
                     '-- --uinput: /dev/uinput  -> /dev/input/event*  (real device)
```

Both tiers emit the **identical evdev `input_event` byte stream**, so one reader
works against the `/tmp` FIFO *and* a real device node. com2tty itself **never
needs administrator at runtime** in either tier.

### Why `--uinput` needs a one-time setup (and the default doesn't)

A `/tmp` FIFO is just a user-owned file — no privileges involved, which is why
the default tier works immediately. A *real* input device is different: Linux's
**only** way to create one from user space is `/dev/uinput`, and that is
root-gated. Unlike the serial PTY, there is no user-space stand-in for a kernel
input device, so the `/tmp` trick cannot conjure a system-wide controller —
that is precisely what `--uinput` (and its one-time grant) buys you.

### Default tier — zero setup

```cmd
com2tty --gamepad
```

Streams controller slot 0 to `/tmp/com2pad0` (override with `--wsl-pad`). No
root, nothing to install. Consume it from WSL by reading 24-byte Linux
`input_event` records from the FIFO (see the axis/button profile below).

### Opt-in tier — `--uinput` (system-wide real device)

```cmd
com2tty --gamepad --uinput
```

Two things need a **one-time** root action (com2tty itself never elevates):

1. `/dev/uinput` is `root 0600`, so com2tty can't open it to *create* the device.
2. The resulting `/dev/input/event*` node is `root:input 0660`, so games can't
   *read* it unless your user is in the `input` group.

Run once inside WSL (use `sudo`, or `wsl -u root` from Windows — no password):

```bash
sudo modprobe uinput
sudo chmod 0666 /dev/uinput
sudo usermod -aG input "$USER"     # so apps can read /dev/input/event*
```

To make the uinput part survive `wsl --shutdown`, add this to `/etc/wsl.conf`
(the `[boot]` command runs as root automatically on every WSL start, so you
never need `sudo` again):

```ini
[boot]
command = modprobe uinput && chmod 0666 /dev/uinput
```

Then run `wsl --shutdown` from Windows once (this also refreshes your group
membership). If `/dev/uinput` is unavailable, com2tty prints these exact
instructions and **automatically falls back to the `/tmp` stream** so it keeps
working.

> Note: the stock WSL2 kernel has `CONFIG_INPUT_UINPUT=m` (works) but
> `CONFIG_INPUT_JOYDEV` is **not** set, so the legacy `/dev/input/js*` node is
> absent. This is fine — modern apps and SDL2 read `/dev/input/event*` directly.

### Options

```
--gamepad             Enable gamepad mode (no COM port needed).
--pad-index {0,1,2,3} Which XInput controller slot to forward (default: 0).
--pad-name NAME       Virtual device name (default: "Microsoft X-Box 360 pad").
--poll-hz HZ          XInput polling rate, send-on-change (default: 250).
--uinput              Create a real /dev/input device (needs one-time setup).
--wsl-pad PATH        FIFO path for the default /tmp stream (default: /tmp/com2pad0).
```

### Device profile (both tiers)

Linux event codes emitted: buttons `BTN_A/B/X/Y`, `BTN_TL/TR`, `BTN_SELECT`,
`BTN_START`, `BTN_THUMBL/THUMBR`; axes `ABS_X/Y/RX/RY` (sticks, ±32768),
`ABS_Z`/`ABS_RZ` (triggers, 0–255), `ABS_HAT0X/Y` (D-pad, −1..1). Stick Y axes
are inverted to match Linux convention.

### Verifying `--uinput` in WSL

```bash
sudo apt install evtest
evtest                     # pick the "Microsoft X-Box 360 pad" device
```

Press buttons / move sticks on Windows and watch the events appear in WSL.

---

## Configuring `/dev/ttyUSB0` in WSL (Highly Recommended)

In Linux, the `/dev` directory is owned by `root`. Running `com2tty` as a normal Windows user means the WSL subprocess cannot write directly to `/dev/ttyUSB0`.

To work around this cleanly without requiring root permissions or passwordless `sudo` at runtime:

1. Run `com2tty` with the default path (or any `/tmp/` path):
   ```cmd
   com2tty COM3 --wsl-tty /tmp/ttyUSB0
   ```
2. In your WSL terminal, run the following command **once** to create a permanent symlink pointing from `/dev/` to the stable `/tmp/` path:
   ```bash
   sudo ln -sf /tmp/ttyUSB0 /dev/ttyUSB0
   ```
3. Now, any WSL application (such as `minicom`, `screen`, `esp-idf`, or Python scripts) can read and write to `/dev/ttyUSB0`. 

Whenever `com2tty` starts up, it updates `/tmp/ttyUSB0` to point to the active pseudo-terminal (`/dev/pts/N`), and `/dev/ttyUSB0` resolves to the correct endpoint automatically.

---

## Troubleshooting

### WSL Bridge warns about Permission Denied
If you specify `-w /dev/ttyUSB0` directly and see:
`[WSL] Warning: Permission denied creating symlink at /dev/ttyUSB0.`
This is expected behavior. The script will automatically fall back to `/tmp/ttyUSB0` and output instructions on how to link them.

### Serial Port Busy / Access Denied
Ensure that no other application on the Windows host (like PuTTY, Serial Monitor, or another instance of `com2tty`) is currently holding the COM port open.

### Debugging
Run `com2tty` with the `-d` or `--debug` flag to view detailed logs and data transfer statistics:
```cmd
com2tty COM3 --debug
```

## License

MIT
