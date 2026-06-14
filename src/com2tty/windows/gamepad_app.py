"""Gamepad bridge session orchestration on the Windows host.

``run_gamepad_bridge`` polls one XInput controller slot and streams its
state into the WSL gamepad helper over the same stdio-pipe transport the
serial bridge uses; the helper's stdout carries force-feedback (rumble)
frames back. ``run_multi_gamepad_bridge`` forwards several slots at once.
"""
import logging
import os
import threading
import time

from .bridge_app import _derive_indexed_path, run_with_respawn
from .os_hacks.console import get_banner_colors
from .wsl_process import (
    check_wsl_environment,
    get_wsl_path,
    helper_script_path,
    spawn_wsl_helper,
    terminate_wsl_helper,
    wsl_command,
)


def _log_wsl_line(msg):
    """Log one line of WSL gamepad-helper stderr at a severity that matches
    its control marker, so a degraded run (e.g. /dev/uinput not accessible,
    permission too low) surfaces as a warning instead of hiding among the
    INFO chatter -- the dashboard turns warnings into toasts.
    """
    if "PAD_ERROR" in msg:
        logging.error(f"[WSL] {msg}")
    elif "PAD_UINPUT_UNAVAILABLE" in msg or "Falling back" in msg:
        logging.warning(f"[WSL] {msg}")
    else:
        logging.info(f"[WSL] {msg}")


def run_gamepad_bridge(pad_index=0, poll_hz=250, name="Microsoft X-Box 360 pad",
                       use_uinput=False, tmp_path="/tmp/com2pad0", distro=None,
                       stop_event=None):
    """
    Forward a Windows XInput controller into WSL as a virtual gamepad.

    The Windows host polls XInput (native driver, no usbipd needed) and streams
    fixed-length frames through the same stdin/stdout pipe mechanism used by the
    serial bridge. The WSL helper (pad_bridge.py) turns them into an evdev event
    stream.

    By default (``use_uinput=False``) the WSL side writes the event stream to a
    user-writable FIFO under /tmp -- no root required, exactly like the serial
    bridge's default /tmp/ttyUSB0 endpoint. With ``use_uinput=True`` it creates
    a real system-wide /dev/input device (one-time root setup; it falls back to
    the /tmp stream and prints instructions if /dev/uinput is not accessible).
    com2tty itself never needs administrator at runtime.

    Returns the exit reason ("interrupt", "stop", "wsl-exited", or
    "shutdown"), which run_with_respawn uses to decide whether to rebuild
    the bridge.
    """
    from .gamepad_host import GamepadSource

    # Construct the source first so a missing controller/DLL fails fast on the
    # Windows side with a clear message.
    src = GamepadSource(pad_index)

    pad_script = helper_script_path("pad_bridge.py")
    if not os.path.exists(pad_script):
        raise FileNotFoundError(f"WSL gamepad bridge script not found at: {pad_script}")

    wsl_pad_path = get_wsl_path(pad_script, distro)
    logging.info(f"WSL gamepad bridge script resolved to: {wsl_pad_path}")

    check_wsl_environment(wsl_pad_path, distro)

    cmd = wsl_command(distro, "python3", "-u", wsl_pad_path,
                      "--pad-index", str(pad_index), "--name", name,
                      "--tmp-path", tmp_path)
    if use_uinput:
        cmd.append("--uinput")
    proc = spawn_wsl_helper(cmd)

    shutdown_event = threading.Event()

    def read_wsl_logs():
        """Surface WSL bridge stderr (status + one-time setup instructions)."""
        try:
            while not shutdown_event.is_set():
                line = proc.stderr.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="replace").rstrip()
                if msg:
                    _log_wsl_line(msg)
        except Exception as e:
            if not shutdown_event.is_set():
                logging.debug(f"Error in WSL log thread: {e}")
        finally:
            shutdown_event.set()

    def drain_wsl_stdout():
        """Reverse channel: rumble (force feedback) frames from the WSL helper.

        The uinput sink forwards effect playback as 6-byte frames; anything
        else on the pipe is discarded by the resynchronising reader.
        """
        from .gamepad_host import RumbleReader
        reader = RumbleReader()
        try:
            while not shutdown_event.is_set():
                data = proc.stdout.read(64)
                if not data:
                    break
                for left, right in reader.feed(data):
                    src.set_rumble(left, right)
        except Exception as e:
            # Don't die silently: without this the rumble channel just stops
            # with no clue why. Shutdown-time pipe errors are expected, so
            # only surface a failure while the bridge is meant to be running.
            if not shutdown_event.is_set():
                logging.warning(f"Gamepad rumble reader thread stopped: {e}")

    t_logs = threading.Thread(target=read_wsl_logs, daemon=True)
    t_out = threading.Thread(target=drain_wsl_stdout, daemon=True)
    t_logs.start()
    t_out.start()

    _print_gamepad_banner(pad_index, name, poll_hz, use_uinput, tmp_path)

    logging.info("Gamepad bridge is active. Press Ctrl+C to stop.")

    interval = 1.0 / float(poll_hz)
    heartbeat = 0.5  # seconds; keep the pipe warm even when idle
    last_send = 0.0

    exit_reason = "shutdown"
    try:
        while not shutdown_event.is_set():
            if stop_event is not None and stop_event.is_set():
                logging.info("Stop requested; shutting down gamepad bridge.")
                exit_reason = "stop"
                break
            if proc.poll() is not None:
                logging.info("WSL gamepad subprocess exited.")
                exit_reason = "wsl-exited"
                break

            changed, frame = src.poll()
            now = time.time()
            if changed or (now - last_send) >= heartbeat:
                try:
                    proc.stdin.write(frame)
                    proc.stdin.flush()
                    last_send = now
                except (BrokenPipeError, OSError):
                    logging.info("WSL pipe closed.")
                    exit_reason = "wsl-exited"
                    break

            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Stopping gamepad bridge due to KeyboardInterrupt...")
        exit_reason = "interrupt"
    finally:
        shutdown_event.set()
        logging.info("Cleaning up gamepad bridge...")
        # terminate_wsl_helper closes the helper's stdin, which the helper's
        # select loop sees as EOF and exits on; the daemon reader threads then
        # unblock when proc.stdout/stderr reach EOF. Do NOT close those read
        # pipes here: closing a pipe another thread is blocked reading
        # deadlocks on Windows (the reader holds the file lock).
        terminate_wsl_helper(proc)
        logging.info("Gamepad bridge stopped successfully.")
    return exit_reason


def _print_gamepad_banner(pad_index, name, poll_hz, use_uinput, tmp_path):
    from .os_hacks.console import banners_enabled
    if not banners_enabled():
        return
    yellow, cyan, green, reset = get_banner_colors()
    if use_uinput:
        sink_mode = "uinput (real /dev/input device, one-time root)"
        sink_node = "/dev/input/event* (SDL2-ready)"
    else:
        sink_mode = "/tmp evdev stream (no root, default)"
        sink_node = tmp_path
    print(f"\n{yellow}========================================================================{reset}")
    print(f"{yellow}  com2tty Gamepad Bridge Active{reset}")
    print(f"{yellow}------------------------------------------------------------------------{reset}")
    print(f"{cyan}  Mode                 : {green}XInput -> WSL ({sink_mode}){reset}")
    print(f"{cyan}  Controller slot      : {green}{pad_index}{reset}")
    print(f"{cyan}  Virtual device       : {green}{name}{reset}")
    print(f"{cyan}  WSL endpoint         : {green}{sink_node}{reset}")
    print(f"{cyan}  Poll rate            : {poll_hz} Hz (send-on-change){reset}")
    print(f"{yellow}========================================================================{reset}\n")


def run_multi_gamepad_bridge(pad_indices, poll_hz=250,
                             name="Microsoft X-Box 360 pad",
                             use_uinput=False, tmp_path="/tmp/com2pad0",
                             distro=None, auto_respawn=False):
    """Forward several XInput controller slots concurrently.

    Each slot gets its own WSL helper and its own endpoint: the FIFO path is
    derived from ``tmp_path`` by incrementing its trailing number
    (/tmp/com2pad0, /tmp/com2pad1, ...). In the uinput tier each helper
    creates its own /dev/input device, exactly like plugging in several
    physical controllers.
    """
    stop_event = threading.Event()
    threads = []

    def _runner(position, pad_index):
        try:
            kwargs = dict(pad_index=pad_index, poll_hz=poll_hz, name=name,
                          use_uinput=use_uinput,
                          tmp_path=_derive_indexed_path(tmp_path, position),
                          distro=distro)
            if auto_respawn:
                run_with_respawn(run_gamepad_bridge, stop_event=stop_event,
                                 **kwargs)
            else:
                run_gamepad_bridge(stop_event=stop_event, **kwargs)
        except Exception as e:
            logging.error(f"Gamepad bridge for slot {pad_index} failed: {e}")

    for position, pad_index in enumerate(pad_indices):
        t = threading.Thread(target=_runner, args=(position, pad_index),
                             daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
        logging.info("All gamepad bridges have stopped.")
    except KeyboardInterrupt:
        logging.info("Stopping all gamepad bridges...")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5.0)
