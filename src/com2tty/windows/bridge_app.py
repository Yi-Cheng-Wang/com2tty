"""Serial bridge session orchestration on the Windows host.

``run_bridge`` owns one COM-port-to-WSL session: it verifies the
environment, opens the serial port, spawns the WSL helper, starts the three
pump threads (COM->WSL, WSL->COM, stderr control channel), and supervises
until shutdown. ``run_multi_bridge`` runs several sessions side by side and
``run_with_respawn`` rebuilds a session whenever WSL itself goes away.
"""
import logging
import queue
import os
import threading
import time

import serial

from ..core.boards import BOARD_LABELS, UF2_FAMILIES
from .control_handler import read_wsl_stderr
from .os_hacks import device_watcher as devnotify
from .os_hacks.autoplay import restore_orphaned_autoplay
from .os_hacks.console import get_banner_colors
from .serial_host import (
    _poll_wait,
    detect_board_type,
    get_serial_settings,
    get_system_baudrate,
    get_usb_serial_number,
    reopen_serial_port,
    snapshot_ports,
)
from .wsl_process import (
    check_wsl_environment,
    get_wsl_path,
    helper_script_path,
    spawn_wsl_helper,
    terminate_wsl_helper,
    wsl_command,
)


def read_wsl_stdout(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue):
    logging.debug("WSL-to-COM thread started.")
    com_write_failing = False
    try:
        while not shutdown_event.is_set():
            data = proc.stdout.read(1024)
            if not data:
                logging.info("WSL process stdout reached EOF (exited).")
                break

            if uf2_active_event.is_set():
                # UF2 upload mode: route data to UF2 accumulator
                uf2_data_queue.put(data)
            elif rfc2217_active_event.is_set():
                # RFC 2217 mode: route data to the Redirector via queue
                rfc2217_data_queue.put(data)
            else:
                # Normal mode: write directly to COM port. A write failure
                # means the device is unplugged or re-enumerating; the
                # COM-to-WSL thread owns the reconnect, so drop the data and
                # keep this thread alive instead of tearing the bridge down.
                try:
                    logging.debug(f"WSL -> COM: {len(data)} bytes")
                    ser.write(data)
                    ser.flush()
                    com_write_failing = False
                except Exception as e:
                    if not com_write_failing:
                        logging.warning(
                            f"Dropping WSL -> COM data while {ser.port} is "
                            f"unavailable (reconnecting): {e}")
                        com_write_failing = True
    except Exception as e:
        if not shutdown_event.is_set():
            logging.error(f"Error in WSL-to-COM thread: {e}")
    finally:
        shutdown_event.set()


def read_com_port(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event, usb_serial=None,
                  ser_lock=None):
    logging.debug("COM-to-WSL thread started.")
    consecutive_errors = 0
    try:
        while not shutdown_event.is_set():
            if rfc2217_active_event.is_set() or uf2_active_event.is_set():
                time.sleep(0.1)
                continue

            # Short timeout allows periodic checking of shutdown_event
            try:
                # Read whatever has already arrived instead of blocking for a
                # full 1024-byte buffer: read(1024) would sit out the whole
                # 0.2 s timeout on every small payload, adding up to 200 ms
                # of forwarding latency to interactive traffic.
                data = ser.read(ser.in_waiting or 1)
                if data and ser.in_waiting:
                    data += ser.read(ser.in_waiting)
                consecutive_errors = 0
            except (PermissionError, OSError):
                # COM port may be temporarily unavailable during board reset
                # (e.g. Pico rebooting into BOOTSEL). Retry briefly; repeated
                # failures mean the device was unplugged or re-enumerated, so
                # follow it instead of spinning on the stale handle forever.
                if shutdown_event.is_set():
                    continue
                consecutive_errors += 1
                if consecutive_errors < 4:
                    time.sleep(0.5)
                    continue
                logging.warning(
                    f"{ser.port} stopped responding; waiting for the device "
                    f"to come back (unplugged or re-enumerating)...")
                if reopen_serial_port(
                        ser, usb_serial, shutdown_event,
                        abort_events=(rfc2217_active_event, uf2_active_event),
                        ser_lock=ser_lock):
                    logging.info(f"Bridge resumed on {ser.port}.")
                consecutive_errors = 0
                continue

            if data and not rfc2217_active_event.is_set() and not uf2_active_event.is_set():
                logging.debug(f"COM -> WSL: {len(data)} bytes")
                proc.stdin.write(data)
                proc.stdin.flush()
    except Exception as e:
        if not shutdown_event.is_set():
            logging.error(f"Error in COM-to-WSL thread: {e}")
    finally:
        shutdown_event.set()


def _derive_indexed_path(base, index):
    """Per-port WSL symlink path for multi-port mode.

    Increments a trailing number when present (/tmp/ttyUSB0 -> /tmp/ttyUSB1),
    otherwise appends the index, so each bridge gets a distinct endpoint.
    """
    if index == 0:
        return base
    import re
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + index}"
    return f"{base}{index}"


def run_with_respawn(target, stop_event=None, respawn_delay=2.0, **kwargs):
    """Run a bridge entry function and rebuild the bridge when WSL dies.

    ``wsl --shutdown``, a WSL servicing update, or a crashed helper normally
    ends the session; with --auto-respawn the host waits until WSL answers
    again and then re-runs ``target`` (run_bridge or run_gamepad_bridge) with
    the same arguments, so the WSL endpoint reappears at the same path. A
    Ctrl+C or a stop request ends the loop like a normal session.
    """
    while True:
        reason = target(stop_event=stop_event, **kwargs)
        if reason in ("interrupt", "stop"):
            return reason
        if stop_event is not None and stop_event.is_set():
            return "stop"
        logging.warning(f"Bridge session ended ({reason}); waiting for WSL "
                        "before respawning (--auto-respawn)...")
        while True:
            if stop_event is not None and stop_event.is_set():
                return "stop"
            try:
                check_wsl_environment(None, kwargs.get("distro"))
                break
            except RuntimeError as exc:
                logging.info(f"WSL is not ready yet: {exc}")
                time.sleep(respawn_delay)
        logging.info("WSL is available again; respawning the bridge.")


def run_multi_bridge(ports, baud, wsl_tty, bytesize, parity, stopbits,
                     xonxoff, rtscts, dsrdtr, rfc2217_port, distro=None,
                     board="auto", wait=False, auto_respawn=False):
    """Bridge several COM ports concurrently from a single invocation.

    Each port gets its own WSL helper, a distinct symlink path (derived from
    --wsl-tty by incrementing its trailing number), and a distinct RFC 2217
    port (base + 2*i, because every bridge also reserves its port + 1 for the
    UF2 relay). Only the first port performs the PlatformIO environment-var
    injection and picotool interception, so the bridges do not overwrite each
    other's shell configuration.
    """
    stop_event = threading.Event()
    threads = []

    def _runner(index, port_name):
        try:
            kwargs = dict(
                port=port_name, baud=baud,
                wsl_tty=_derive_indexed_path(wsl_tty, index),
                bytesize=bytesize, parity=parity, stopbits=stopbits,
                xonxoff=xonxoff, rtscts=rtscts, dsrdtr=dsrdtr,
                rfc2217_port=rfc2217_port + 2 * index,
                distro=distro, board=board, wait=wait,
                env_setup=(index == 0))
            if auto_respawn:
                run_with_respawn(run_bridge, stop_event=stop_event, **kwargs)
            else:
                run_bridge(stop_event=stop_event, **kwargs)
        except Exception as e:
            logging.error(f"Bridge for {port_name} failed: {e}")

    for i, port_name in enumerate(ports):
        t = threading.Thread(target=_runner, args=(i, port_name), daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
        logging.info("All bridges have stopped.")
    except KeyboardInterrupt:
        logging.info("Stopping all bridges...")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5.0)


def _wait_for_port(port, stop_event):
    """Block until the named COM port is enumerated (or a stop is requested).

    Implements --wait: the device may not be plugged in yet; poll for it
    instead of failing. Board detection in run_bridge needs the port
    enumerated anyway. Returns False when stopped before it appeared.
    """
    logging.info(f"Port {port} is not present; waiting for it to appear (--wait)...")
    while port not in snapshot_ports():
        if stop_event is not None and stop_event.is_set():
            logging.info(f"Stop requested while waiting for {port}.")
            return False
        _poll_wait(0.5)
    logging.info(f"Port {port} appeared.")
    return True


def _resolve_baudrate(port, baud):
    """Resolve 'auto' against the Windows-configured rate (9600 fallback)."""
    if str(baud).lower() == "auto":
        detected_baud = get_system_baudrate(port)
        if detected_baud:
            logging.info(f"Auto-detected Windows COM port baudrate: {detected_baud} baud")
            return detected_baud
        logging.warning("Failed to auto-detect baudrate, falling back to 9600 baud.")
        return 9600
    return int(baud)


def _resolve_board_type(port, board):
    """Board family from --board override or USB VID detection."""
    if board and board != "auto":
        board_type = "unknown" if board == "none" else board
        logging.info(f"Board type set manually: {board_type}")
        return board_type
    board_type = detect_board_type(port)
    logging.info(f"Detected board type: {board_type} (VID-based)")
    return board_type


def _print_bridge_banner(port, board_type, rfc2217_port, usb_serial, env_setup):
    yellow, cyan, green, reset = get_banner_colors()
    board_label = BOARD_LABELS.get(board_type, board_type)
    print(f"\n{yellow}========================================================================{reset}")
    print(f"{yellow}  com2tty Bridge Active - {port}{reset}")
    print(f"{yellow}------------------------------------------------------------------------{reset}")
    print(f"{cyan}  Board type (VID)     : {green}{board_label}{reset}")
    print(f"{cyan}  RFC 2217 upload port : rfc2217://127.0.0.1:{rfc2217_port} (in WSL){reset}")
    print(f"{cyan}  UF2 relay port       : 127.0.0.1:{rfc2217_port + 1} (in WSL){reset}")
    print(f"{cyan}  USB Serial Number    : {usb_serial or 'N/A (fallback mode)'}{reset}")
    print(f"{cyan}  Picotool interceptor : {'Active' if board_type in UF2_FAMILIES else 'N/A'}{reset}")
    print(f"{yellow}------------------------------------------------------------------------{reset}")
    if env_setup:
        print(f"{yellow}  [WARNING] Environment variables injected into your WSL shell rc (~/.bashrc, ~/.zshrc){reset}")
        print(f"{yellow}  Please OPEN A NEW WSL TERMINAL or run `source ~/.bashrc` (or ~/.zshrc){reset}")
    else:
        print(f"{cyan}  Secondary bridge: PlatformIO env vars were set up once by the{reset}")
        print(f"{cyan}  primary (first) port -- no shell changes are needed here. Use the{reset}")
        print(f"{cyan}  primary bridge's terminal output for the `source` instructions.{reset}")
    print(f"{yellow}========================================================================{reset}\n")


def run_bridge(port, baud, wsl_tty, bytesize, parity, stopbits, xonxoff,
               rtscts, dsrdtr, rfc2217_port, distro=None, board="auto",
               env_setup=True, stop_event=None, wait=False):
    # Recover any AutoPlay setting a previous run left disabled after a crash.
    restore_orphaned_autoplay()

    # Resolve serial settings
    ser_bytesize, ser_parity, ser_stopbits = get_serial_settings(bytesize, parity, stopbits)

    # Locate WSL bridge.py script and verify the WSL environment before
    # touching the serial port, so a broken WSL setup fails fast and clean.
    bridge_script = helper_script_path("bridge.py")
    if not os.path.exists(bridge_script):
        raise FileNotFoundError(f"WSL bridge script not found at: {bridge_script}")

    wsl_bridge_path = get_wsl_path(bridge_script, distro)
    logging.info(f"WSL bridge script resolved to: {wsl_bridge_path}")

    check_wsl_environment(wsl_bridge_path, distro)

    # Event-driven plug/unplug wake-ups for the polling loops below; plain
    # sleeps remain the fallback when the watcher cannot start.
    devnotify.start_device_watcher()

    if wait and port not in snapshot_ports():
        if not _wait_for_port(port, stop_event):
            return "stop"

    baud = _resolve_baudrate(port, baud)

    logging.info(f"Opening Windows serial port {port} at {baud} baud...")
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=ser_bytesize,
        parity=ser_parity,
        stopbits=ser_stopbits,
        xonxoff=xonxoff,
        rtscts=rtscts,
        dsrdtr=dsrdtr,
        timeout=0.2  # Enable timeout for shutdown check
    )

    cmd = wsl_command(distro, "python3", "-u", wsl_bridge_path,
                      "--symlink", wsl_tty, "--rfc2217-port", str(rfc2217_port))
    if not env_setup:
        # Secondary bridge in multi-port mode: do not overwrite the primary
        # bridge's PlatformIO env vars or picotool interception.
        cmd.append("--no-env-setup")
    proc = spawn_wsl_helper(cmd)

    shutdown_event = threading.Event()
    rfc2217_active_event = threading.Event()
    rfc2217_data_queue = queue.Queue()
    uf2_active_event = threading.Event()
    uf2_data_queue = queue.Queue()
    # Serialises close/open/setting changes on `ser` across the COM reader's
    # hot-plug reconnect and the stderr thread's dynamic-SETTINGS handling.
    ser_lock = threading.Lock()
    # Detect board type from USB VID/PID, unless overridden via --board
    # (covers boards whose USB-UART chip is not in the VID whitelist).
    board_type = _resolve_board_type(port, board)

    # Determine USB Serial Number for hardware path matching
    usb_serial = get_usb_serial_number(port)
    if usb_serial:
        logging.info(f"Target USB Serial Number identified: {usb_serial}")
    else:
        logging.warning("Could not identify USB Serial Number. UF2 upload will fallback to first available RP2 drive.")

    # Start thread routing
    t_wsl_to_com = threading.Thread(target=read_wsl_stdout, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue), daemon=True)
    t_com_to_wsl = threading.Thread(target=read_com_port, args=(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event, usb_serial, ser_lock), daemon=True)
    t_wsl_stderr = threading.Thread(target=read_wsl_stderr, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue, usb_serial, board_type, ser_lock), daemon=True)

    t_wsl_to_com.start()
    t_com_to_wsl.start()
    t_wsl_stderr.start()

    _print_bridge_banner(port, board_type, rfc2217_port, usb_serial, env_setup)

    logging.info("Bridge is fully active. Press Ctrl+C to stop.")

    exit_reason = "shutdown"
    try:
        while not shutdown_event.is_set():
            if stop_event is not None and stop_event.is_set():
                logging.info(f"Stop requested; shutting down bridge for {port}.")
                exit_reason = "stop"
                break
            # Wait and check if the WSL process is still running
            if proc.poll() is not None:
                logging.info("WSL subprocess exited unexpectedly.")
                exit_reason = "wsl-exited"
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        logging.info("Stopping bridge due to KeyboardInterrupt...")
        exit_reason = "interrupt"
    finally:
        shutdown_event.set()

        logging.info("Cleaning up resources...")
        # Wait for threads to exit to avoid pyserial close() deadlock on Windows
        t_com_to_wsl.join(timeout=0.5)

        # Close serial port first
        try:
            ser.close()
            logging.info("Closed Windows serial port.")
        except Exception as e:
            logging.debug(f"Error closing serial port: {e}")

        # Terminate WSL subprocess
        terminate_wsl_helper(proc)
        logging.info("Bridge stopped successfully.")
    return exit_reason
