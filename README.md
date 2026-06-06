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
