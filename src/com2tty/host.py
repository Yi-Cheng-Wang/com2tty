import os
import time
import logging
import shutil
import subprocess
import threading
import queue
import serial
import serial.tools.list_ports
from .rfc2217_server import Redirector
from .banner import enable_vt_mode, get_banner_colors  # noqa: F401 (re-export)
from .boards import (  # noqa: F401 (re-exports kept for backwards compatibility)
    BOARD_LABELS,
    UF2_FAMILIES,
    detect_board_type,
    esp32_manual_reset,
    get_usb_serial_number,
    pico_manual_reset,
    samd_touch_reset,
    stm32_manual_reset,
)
from .uf2 import (  # noqa: F401 (re-exports kept for backwards compatibility)
    AutoplaySuppressor,
    get_drive_by_serial,
    list_removable_drives,
    md5_hexdigest,
    restore_orphaned_autoplay,
)

def wsl_command(distro, *argv):
    """Build a wsl.exe invocation that bypasses the WSL login shell.

    Without ``--exec``, wsl.exe re-joins its arguments and hands them to the
    distro's default shell, which re-splits on whitespace. Any path containing
    a space (e.g. /mnt/c/Program Files/...) would break. ``--exec`` launches
    the binary directly with the arguments preserved as-is.
    """
    cmd = ["wsl"]
    if distro:
        cmd += ["-d", distro]
    cmd.append("--exec")
    cmd.extend(argv)
    return cmd

def get_wsl_path(win_path, distro=None):
    cmd = wsl_command(distro, "wslpath", "-u", win_path)
    try:
        # wslpath emits UTF-8 regardless of the Windows locale; decoding with
        # the ANSI codepage would corrupt non-ASCII paths (e.g. CJK usernames).
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=True)
        return res.stdout.strip()
    except Exception as e:
        logging.debug(f"wslpath failed: {e}. Using fallback conversion.")
        # Fallback to mounting convention /mnt/<drive>/...
        drive = win_path[0].lower()
        path = win_path[2:].replace("\\", "/")
        return f"/mnt/{drive}{path}"

def check_wsl_environment(wsl_script_path=None, distro=None):
    """Verify WSL prerequisites before spawning the bridge.

    Each failure mode otherwise surfaces as a cryptic error (WinError 2, an
    instantly-exiting subprocess, a 'No such file' from deep inside WSL), so
    fail fast here with an actionable message instead.
    """
    target = f"WSL distribution '{distro}'" if distro else "the default WSL distribution"

    if shutil.which("wsl") is None:
        raise RuntimeError(
            "wsl.exe was not found in PATH. com2tty requires Windows Subsystem "
            "for Linux. Install it with 'wsl --install' and try again."
        )

    try:
        res = subprocess.run(
            wsl_command(distro, "python3", "--version"),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to start {target}: {e}")

    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(
            f"'python3' is not available in {target} ({detail or 'no output'}). "
            "Check 'wsl -l -v' for installed distributions, select one with "
            "--distro, or install Python inside WSL (e.g. 'sudo apt install python3')."
        )

    if wsl_script_path:
        res = subprocess.run(
            wsl_command(distro, "test", "-r", wsl_script_path),
            capture_output=True, timeout=30,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"The com2tty bridge script is not readable from WSL at "
                f"{wsl_script_path}. Make sure Windows drive automounting is "
                "enabled in WSL ([automount] in /etc/wsl.conf must not be "
                "disabled) and that the install path is accessible from WSL."
            )

def get_serial_settings(bytesize, parity, stopbits):
    bytesize_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS
    }
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "S": serial.PARITY_SPACE,
        "M": serial.PARITY_MARK
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO
    }
    
    return (
        bytesize_map.get(bytesize, serial.EIGHTBITS),
        parity_map.get(parity, serial.PARITY_NONE),
        stopbits_map.get(stopbits, serial.STOPBITS_ONE)
    )

def get_system_baudrate(port):
    import re
    try:
        # mode.com prints the COM port state. The console codepage may not match
        # Python's locale decoding; the digits we need are ASCII, so replace
        # anything undecodable.
        res = subprocess.run(["mode.com", port], capture_output=True, text=True,
                             errors="replace")
        if res.returncode == 0:
            # Prefer the number on the line that names the baud field. Most
            # locales still print the English word "Baud", but other numbers
            # (e.g. a localized date or the COM index) can precede it, so a
            # bare "first large number" scan would mis-detect on those systems.
            match = re.search(r"[Bb]aud[^0-9]*(\d{3,7})", res.stdout)
            if not match:
                match = re.search(r"\b(\d{3,7})\b", res.stdout)
            if match:
                return int(match.group(1))
    except Exception as e:
        logging.debug(f"Failed to auto-detect baudrate for {port}: {e}")
    return None

def read_wsl_stdout(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue):
    logging.debug("WSL-to-COM thread started.")
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
                # Normal mode: write directly to COM port
                logging.debug(f"WSL -> COM: {len(data)} bytes")
                ser.write(data)
                ser.flush()
    except Exception as e:
        if not shutdown_event.is_set():
            logging.error(f"Error in WSL-to-COM thread: {e}")
    finally:
        shutdown_event.set()

def reopen_serial_port(ser, usb_serial, shutdown_event, abort_events=(),
                       max_attempts=None, poll_interval=0.5):
    """Reopen a dead COM handle, following the device across re-enumeration.

    Windows invalidates the open handle when a device is unplugged or reboots
    (e.g. into a bootloader), and may assign a different COM number when it
    returns. This closes the stale handle, then retries the original name and
    falls back to locating the device by its USB serial number.

    Returns True once the port is open again. Returns False when
    ``max_attempts`` (None = unlimited) is exhausted, ``shutdown_event`` is
    set, or any event in ``abort_events`` becomes set (used to yield to the
    UF2 upload path, which manages its own reopen).
    """
    original_port = ser.port
    try:
        ser.close()
    except Exception:
        pass
    # Never reopen at 1200 baud: that would immediately re-trigger the
    # bootloader touch on boards that interpret it.
    if getattr(ser, 'baudrate', 115200) == 1200:
        ser.baudrate = 115200

    attempts = 0
    while not shutdown_event.is_set():
        if any(evt.is_set() for evt in abort_events):
            return False
        if max_attempts is not None and attempts >= max_attempts:
            return False
        attempts += 1
        time.sleep(poll_interval)

        # First try the port under its current name.
        try:
            ser.open()
            logging.info(f"{ser.port} reopened successfully.")
            return True
        except Exception:
            pass

        # The port number may have changed after re-enumeration; locate the
        # device by USB serial number.
        if usb_serial:
            for p in serial.tools.list_ports.comports():
                if p.serial_number == usb_serial and p.device != ser.port:
                    logging.info(f"Device reappeared as {p.device} (was {original_port}).")
                    ser.port = p.device
                    try:
                        ser.open()
                        logging.info(f"{p.device} opened successfully.")
                        return True
                    except Exception:
                        pass
    return False


def read_com_port(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event, usb_serial=None):
    logging.debug("COM-to-WSL thread started.")
    consecutive_errors = 0
    try:
        while not shutdown_event.is_set():
            if rfc2217_active_event.is_set() or uf2_active_event.is_set():
                time.sleep(0.1)
                continue

            # Short timeout allows periodic checking of shutdown_event
            try:
                data = ser.read(1024)
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
                        abort_events=(rfc2217_active_event, uf2_active_event)):
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


class QueuePipeConnection:
    """
    Socket-like adapter that reads from a queue (fed by read_wsl_stdout)
    and writes to proc.stdin. Used by the Redirector during RFC 2217 sessions.
    """
    def __init__(self, proc, data_queue, stop_event):
        self.proc = proc
        self._queue = data_queue
        self._stop = stop_event

    def recv(self, size):
        while not self._stop.is_set():
            try:
                return self._queue.get(timeout=0.2)
            except Exception:
                continue
        return b""

    def sendall(self, data):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception:
            pass

    def close(self):
        self._stop.set()


class ResetProofSerial:
    """
    Wraps a serial port to block DTR/RTS changes from the RFC 2217 client.

    Both ESP32 and Pico resets are handled manually by the host, so we
    silently absorb all DTR/RTS commands from the RFC 2217 protocol
    to prevent them from interfering with our controlled sequences.

    All other attribute access (baudrate, timeout, read, write, etc.) is
    transparently forwarded to the real serial port via __getattr__/__setattr__.
    """
    def __init__(self, ser):
        object.__setattr__(self, '_ser', ser)
        object.__setattr__(self, '_dtr', False)
        object.__setattr__(self, '_rts', False)

    def __getattr__(self, name):
        if name == 'dtr':
            return object.__getattribute__(self, '_dtr')
        if name == 'rts':
            return object.__getattribute__(self, '_rts')
        return getattr(object.__getattribute__(self, '_ser'), name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        elif name in ('dtr', 'rts'):
            # Silently cache value without applying to hardware
            object.__setattr__(self, f'_{name}', value)
            logging.debug(f"[ResetProof] Blocked {name.upper()} = {value}")
        else:
            # Forward everything else (baudrate, timeout, etc.) to real serial
            setattr(object.__getattribute__(self, '_ser'), name, value)


def read_wsl_stderr(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue, usb_serial, board_type):
    """
    Reads control messages from WSL bridge's stderr.
    Handles dynamic serial settings, RFC 2217 session lifecycle, and UF2 uploads.
    """
    logging.debug("WSL stderr logging thread started.")

    redirector_stop = None
    redirector_thread = None

    def _start_rfc2217_session():
        nonlocal redirector_stop, redirector_thread

        logging.info("RFC 2217 client connected. PTY bridge suspended.")
        rfc2217_active_event.set()
        time.sleep(0.2)

        # Board-specific reset is done upfront based on VID detection
        if board_type == 'esp32':
            esp32_manual_reset(ser)
        elif board_type == 'samd':
            # Leonardo/SAMD-class boards enter their bootloader via the
            # 1200-baud touch; the upload then proceeds over RFC 2217.
            samd_touch_reset(ser)

        settings = ser.get_settings()
        redirector_stop = threading.Event()

        safe_ser = ResetProofSerial(ser)
        qpc = QueuePipeConnection(proc, rfc2217_data_queue, redirector_stop)

        def _redirector_runner():
            r = Redirector(safe_ser, qpc)
            try:
                r.shortcircuit()
            except Exception as e:
                logging.debug(f"RFC 2217 session error: {e}")
            finally:
                r.stop()
                ser.apply_settings(settings)

        redirector_thread = threading.Thread(target=_redirector_runner, daemon=True)
        redirector_thread.start()

    def _stop_rfc2217_session():
        nonlocal redirector_stop, redirector_thread

        if redirector_stop:
            redirector_stop.set()
        if redirector_thread:
            redirector_thread.join(timeout=3.0)
            redirector_thread = None

        if board_type == 'esp32':
            # Post-upload: hard reset to boot chip with new firmware
            try:
                logging.info("[ESP32] Post-upload reset: booting with new firmware...")
                ser.dtr = False
                ser.rts = True   # EN LOW (reset)
                time.sleep(0.2)
                ser.rts = False  # EN HIGH (boot)
                time.sleep(0.1)
            except Exception:
                pass
        elif board_type in UF2_FAMILIES:
            pico_manual_reset(ser)
        elif board_type == 'stm32':
            stm32_manual_reset(ser)

        logging.info("RFC 2217 client disconnected. PTY bridge resumed.")
        rfc2217_active_event.clear()

    def _handle_uf2_upload(info_str):
        """Accumulate UF2 data from stdout pipe and flash to the correct drive."""
        expected_md5 = None
        try:
            parts = info_str.split(":")
            expected_size = int(parts[0])
            if len(parts) > 1:
                expected_md5 = parts[1]
        except ValueError:
            logging.error(f"[UF2] Invalid upload size: {info_str}")
            return

        logging.info(f"[UF2] Receiving {expected_size} bytes from WSL pipe...")
        uf2_active_event.set()

        # Send ACK to WSL bridge to signal we are ready for the data
        try:
            proc.stdin.write(b"[CONTROL] UF2_ACK\n")
            proc.stdin.flush()
        except Exception as e:
            logging.error(f"[UF2] Failed to write ACK to WSL: {e}")
            uf2_active_event.clear()
            return

        # Accumulate data from the uf2_data_queue (fed by read_wsl_stdout)
        uf2_data = bytearray()
        timeout_count = 0
        while len(uf2_data) < expected_size:
            try:
                chunk = uf2_data_queue.get(timeout=1.0)
                uf2_data.extend(chunk)
                timeout_count = 0
            except Exception:
                timeout_count += 1
                if timeout_count > 10:
                    logging.error(f"[UF2] Timeout waiting for data. Got {len(uf2_data)}/{expected_size} bytes.")
                    break

        logging.info(f"[UF2] Received {len(uf2_data)}/{expected_size} bytes.")

        # Verify MD5 checksum
        if expected_md5:
            received_md5 = md5_hexdigest(uf2_data)
            if received_md5 != expected_md5:
                logging.error(f"[UF2] MD5 checksum mismatch! Expected: {expected_md5}, Got: {received_md5}")
                return
            else:
                logging.info(f"[UF2] MD5 verification successful! Checksum: {received_md5}")

        # Start a background thread to close AutoPlay/Explorer windows instantly.
        close_event = threading.Event()
        closer_thread = None
        target_letters = []

        if os.name == 'nt':
            def _window_closer():
                import ctypes
                try:
                    EnumWindows = ctypes.windll.user32.EnumWindows
                    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                    GetClassNameW = ctypes.windll.user32.GetClassNameW
                    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
                    ShowWindow = ctypes.windll.user32.ShowWindow
                    PostMessageW = ctypes.windll.user32.PostMessageW
                    WM_CLOSE = 0x0010
                    SW_HIDE = 0

                    def foreach_window(hwnd, lParam):
                        class_name = ctypes.create_unicode_buffer(256)
                        GetClassNameW(hwnd, class_name, 256)
                        if class_name.value == "CabinetWClass":
                            title = ctypes.create_unicode_buffer(512)
                            GetWindowTextW(hwnd, title, 512)
                            t_val = title.value.upper()
                            # Match the BOOTSEL volume labels, or the target
                            # drive only in the parenthesised "(X:)" form that
                            # Explorer actually uses in window titles. A bare
                            # "X:" substring would also hit unrelated windows
                            # whose title merely contains that path prefix.
                            match = "RPI-RP2" in t_val or "RP2350" in t_val
                            if not match and target_letters:
                                for dl in target_letters:
                                    if f"({dl}:)" in t_val:
                                        match = True
                                        break
                            if match:
                                ShowWindow(hwnd, SW_HIDE)
                                PostMessageW(hwnd, WM_CLOSE, 0, 0)
                        return True

                    cb = EnumWindowsProc(foreach_window)
                    while not close_event.is_set():
                        EnumWindows(cb, 0)
                        time.sleep(0.01) # Check every 10ms
                except Exception as e:
                    logging.debug(f"Error in window closer thread: {e}")

            closer_thread = threading.Thread(target=_window_closer, daemon=True)
            closer_thread.start()

        with AutoplaySuppressor():
            # Trigger BOOTSEL mode directly from host — this is the reliable path.
            # The RFC2217 1200bps open/close from PlatformIO may not reliably
            # reach the COM port through the relay chain, so we do it ourselves.
            if board_type in UF2_FAMILIES:
                pico_manual_reset(ser)

            logging.info("[UF2] Locating target drive...")
            _flash_uf2(uf2_data, usb_serial, target_letters)

        if closer_thread:
            close_event.set()
            closer_thread.join(timeout=1.0)

    def _close_explorer_for_drive(drive_letter):
        """Close any File Explorer windows showing the BOOTSEL drive to prevent user interference."""
        if os.name != 'nt':
            return
        try:
            import ctypes
            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            GetClassNameW = ctypes.windll.user32.GetClassNameW
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            ShowWindow = ctypes.windll.user32.ShowWindow
            PostMessageW = ctypes.windll.user32.PostMessageW
            WM_CLOSE = 0x0010
            SW_HIDE = 0

            dl = drive_letter[0].upper()
            # Use the parenthesised "(X:)" form Explorer renders in titles; a
            # bare "X:" would also match unrelated windows showing that path.
            targets = ["RPI-RP2", "RP2350", f"({dl}:)"]

            def foreach_window(hwnd, lParam):
                class_name = ctypes.create_unicode_buffer(256)
                GetClassNameW(hwnd, class_name, 256)
                if class_name.value == "CabinetWClass":
                    title = ctypes.create_unicode_buffer(512)
                    GetWindowTextW(hwnd, title, 512)
                    t_val = title.value.upper()
                    for target in targets:
                        if target in t_val:
                            ShowWindow(hwnd, SW_HIDE)
                            PostMessageW(hwnd, WM_CLOSE, 0, 0)
                            break
                return True

            EnumWindows(EnumWindowsProc(foreach_window), 0)
        except Exception as e:
            logging.debug(f"Failed to close explorer window: {e}")

    def _flash_uf2(uf2_data, usb_serial_num, target_letters=None):
        """Find the correct UF2 drive and write the firmware."""
        target_drive = None
        for attempt in range(20):  # Up to 10 seconds
            if usb_serial_num:
                target_drive = get_drive_by_serial(usb_serial_num)

            # Fallback: scan for INFO_UF2.TXT
            if not target_drive:
                for d in list_removable_drives():
                    if os.path.exists(os.path.join(d, "INFO_UF2.TXT")):
                        target_drive = d
                        break

            if target_drive and os.path.exists(os.path.join(target_drive, "INFO_UF2.TXT")):
                break

            time.sleep(0.5)

        if target_drive:
            if target_letters is not None:
                dl = target_drive[0].upper()
                if dl not in target_letters:
                    target_letters.append(dl)

            # Close any Explorer windows Windows may have auto-opened for this drive
            _close_explorer_for_drive(target_drive)

            logging.info(f"[UF2] Flashing to drive {target_drive} ...")
            target_file = os.path.join(target_drive, "flash.uf2")
            try:
                with open(target_file, "wb") as f:
                    f.write(uf2_data)
                    f.flush()
                    os.fsync(f.fileno())
                logging.info("[UF2] Flash successful!")
            except Exception as e:
                logging.error(f"[UF2] Flash failed: {e}")
            finally:
                # Close again in case Explorer reopened during write
                _close_explorer_for_drive(target_drive)
        else:
            logging.error("[UF2] Could not find a valid RP2 drive with INFO_UF2.TXT")

    try:
        while not shutdown_event.is_set():
            line = proc.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            if line_str.startswith("[CONTROL] SETTINGS:"):
                settings_str = line_str.split(":", 1)[1].strip()
                parts = settings_str.split()

                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        changes = []
                        for part in parts:
                            k, v = part.split("=")
                            if k == "baud" and v != "None":
                                new_baud = int(v)
                                if ser.baudrate != new_baud:
                                    ser.baudrate = new_baud
                                    changes.append(f"baud={new_baud}")
                            elif k == "bytesize":
                                new_bytesize = int(v)
                                bytesize_map = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
                                target_bytesize = bytesize_map.get(new_bytesize)
                                if target_bytesize and ser.bytesize != target_bytesize:
                                    ser.bytesize = target_bytesize
                                    changes.append(f"bytesize={new_bytesize}")
                            elif k == "parity":
                                parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
                                target_parity = parity_map.get(v)
                                if target_parity and ser.parity != target_parity:
                                    ser.parity = target_parity
                                    changes.append(f"parity={v}")
                            elif k == "stopbits":
                                stopbits_map = {"1": serial.STOPBITS_ONE, "2": serial.STOPBITS_TWO}
                                target_stopbits = stopbits_map.get(v)
                                if target_stopbits and ser.stopbits != target_stopbits:
                                    ser.stopbits = target_stopbits
                                    changes.append(f"stopbits={v}")
                        if changes:
                            logging.info(f"Dynamic configuration change applied: {', '.join(changes)}")
                        break  # Success — exit retry loop
                    except (PermissionError, OSError) as e:
                        if attempt < max_retries - 1:
                            logging.warning(f"COM port temporarily unavailable while applying settings (attempt {attempt + 1}/{max_retries}): {e}")
                            logging.info("Attempting to reopen COM port handle...")
                            try:
                                ser.close()
                            except Exception:
                                pass
                            time.sleep(1.0)
                            try:
                                ser.open()
                                logging.info(f"COM port {ser.port} handle reopened.")
                            except Exception as reopen_err:
                                logging.warning(f"COM port reopen failed (will retry): {reopen_err}")
                        else:
                            logging.error(f"Failed to apply dynamic settings after {max_retries} attempts: {e}")
                    except Exception as e:
                        logging.error(f"Failed to apply dynamic settings: {e}")
                        break

            elif line_str.startswith("[CONTROL] RFC2217_READY"):
                port_str = line_str.split(":")[1] if ":" in line_str else "?"
                logging.info(f"RFC 2217 WSL Forwarder listening on 127.0.0.1:{port_str} in WSL")

            elif line_str.startswith("[CONTROL] RFC2217_CONNECT"):
                _start_rfc2217_session()

            elif line_str.startswith("[CONTROL] RFC2217_DISCONNECT"):
                _stop_rfc2217_session()

            elif line_str.startswith("[CONTROL] RFC2217_ERROR"):
                logging.warning(f"[WSL] {line_str}")
                if "bind failed" in line_str:
                    logging.warning(
                        "The RFC 2217 TCP port could not be opened in WSL "
                        "(already in use?). Uploads will not work; pick a "
                        "free port with --rfc2217-port."
                    )

            elif line_str.startswith("[CONTROL] UF2_READY"):
                port_str = line_str.split(":")[1] if ":" in line_str else "?"
                logging.info(f"UF2 Relay Server listening on 127.0.0.1:{port_str} in WSL")

            elif line_str.startswith("[CONTROL] UF2_UPLOAD_START"):
                info_str = line_str.split(":", 1)[1] if ":" in line_str else "0"
                _handle_uf2_upload(info_str)

            elif line_str.startswith("[CONTROL] UF2_UPLOAD_END"):
                # After UF2 flash, the Pico reboots and the COM port
                # disconnects/reconnects. The old Windows file handle is
                # permanently stale (ERROR_BAD_COMMAND). We must close
                # and reopen it. uf2_active_event is still set here, so
                # read_com_port and read_wsl_stdout won't touch ser.
                if board_type in UF2_FAMILIES:
                    logging.info(f"[UF2] Reopening {ser.port} after device reboot...")
                    # Reboot after a UF2 flash can take 15-20 seconds: UF2
                    # processing on the mass-storage drive, device reboot,
                    # then USB CDC re-enumeration by Windows.
                    if not reopen_serial_port(ser, usb_serial, shutdown_event,
                                              max_attempts=60):  # up to 30 s
                        logging.error("[UF2] COM port did not reappear after 30s. Bridge may not function until device reconnects.")
                uf2_active_event.clear()
                logging.info("[UF2] Upload pipeline complete.")

            elif line_str.startswith("[CONTROL] UF2_ERROR"):
                logging.warning(f"[WSL] {line_str}")
                if "bind failed" in line_str:
                    logging.warning(
                        "The UF2 relay TCP port could not be opened in WSL "
                        "(already in use?). UF2 uploads will not work; the "
                        "relay listens on --rfc2217-port + 1, so pick a "
                        "different --rfc2217-port."
                    )

            else:
                logging.info(f"[WSL] {line_str}")

    except Exception as e:
        if not shutdown_event.is_set():
            logging.debug(f"Error in WSL stderr thread: {e}")


def run_gamepad_bridge(pad_index=0, poll_hz=250, name="Microsoft X-Box 360 pad",
                       use_uinput=False, tmp_path="/tmp/com2pad0", distro=None):
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
    """
    from .xinput import GamepadSource

    # Construct the source first so a missing controller/DLL fails fast on the
    # Windows side with a clear message.
    src = GamepadSource(pad_index)

    current_dir = os.path.dirname(os.path.abspath(__file__))
    pad_script = os.path.join(current_dir, "pad_bridge.py")
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
    logging.info(f"Spawning WSL process: {' '.join(cmd)}")

    CREATE_NO_WINDOW = 0x08000000
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW,
    )

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
                    logging.info(f"[WSL] {msg}")
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
        from .xinput import RumbleReader
        reader = RumbleReader()
        try:
            while not shutdown_event.is_set():
                data = proc.stdout.read(64)
                if not data:
                    break
                for left, right in reader.feed(data):
                    src.set_rumble(left, right)
        except Exception:
            pass

    t_logs = threading.Thread(target=read_wsl_logs, daemon=True)
    t_out = threading.Thread(target=drain_wsl_stdout, daemon=True)
    t_logs.start()
    t_out.start()

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

    logging.info("Gamepad bridge is active. Press Ctrl+C to stop.")

    interval = 1.0 / float(poll_hz)
    heartbeat = 0.5  # seconds; keep the pipe warm even when idle
    last_send = 0.0

    try:
        while not shutdown_event.is_set():
            if proc.poll() is not None:
                logging.info("WSL gamepad subprocess exited.")
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
                    break

            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Stopping gamepad bridge due to KeyboardInterrupt...")
    finally:
        shutdown_event.set()
        logging.info("Cleaning up gamepad bridge...")
        if proc.poll() is None:
            logging.info("Terminating WSL process...")
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logging.warning("WSL process did not exit. Killing it.")
                proc.kill()
        logging.info("Gamepad bridge stopped successfully.")


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


def run_multi_bridge(ports, baud, wsl_tty, bytesize, parity, stopbits,
                     xonxoff, rtscts, dsrdtr, rfc2217_port, distro=None,
                     board="auto"):
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
            run_bridge(
                port=port_name, baud=baud,
                wsl_tty=_derive_indexed_path(wsl_tty, index),
                bytesize=bytesize, parity=parity, stopbits=stopbits,
                xonxoff=xonxoff, rtscts=rtscts, dsrdtr=dsrdtr,
                rfc2217_port=rfc2217_port + 2 * index,
                distro=distro, board=board,
                env_setup=(index == 0), stop_event=stop_event)
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


def run_bridge(port, baud, wsl_tty, bytesize, parity, stopbits, xonxoff,
               rtscts, dsrdtr, rfc2217_port, distro=None, board="auto",
               env_setup=True, stop_event=None):
    # Recover any AutoPlay setting a previous run left disabled after a crash.
    restore_orphaned_autoplay()

    # Resolve serial settings
    ser_bytesize, ser_parity, ser_stopbits = get_serial_settings(bytesize, parity, stopbits)

    # Locate WSL bridge.py script and verify the WSL environment before
    # touching the serial port, so a broken WSL setup fails fast and clean.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_script = os.path.join(current_dir, "bridge.py")
    if not os.path.exists(bridge_script):
        raise FileNotFoundError(f"WSL bridge script not found at: {bridge_script}")

    wsl_bridge_path = get_wsl_path(bridge_script, distro)
    logging.info(f"WSL bridge script resolved to: {wsl_bridge_path}")

    check_wsl_environment(wsl_bridge_path, distro)

    if str(baud).lower() == "auto":
        detected_baud = get_system_baudrate(port)
        if detected_baud:
            logging.info(f"Auto-detected Windows COM port baudrate: {detected_baud} baud")
            baud = detected_baud
        else:
            logging.warning("Failed to auto-detect baudrate, falling back to 9600 baud.")
            baud = 9600
    else:
        baud = int(baud)

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
        timeout=0.2 # Enable timeout for shutdown check
    )

    cmd = wsl_command(distro, "python3", "-u", wsl_bridge_path,
                      "--symlink", wsl_tty, "--rfc2217-port", str(rfc2217_port))
    if not env_setup:
        # Secondary bridge in multi-port mode: do not overwrite the primary
        # bridge's PlatformIO env vars or picotool interception.
        cmd.append("--no-env-setup")
    logging.info(f"Spawning WSL process: {' '.join(cmd)}")

    # Use CREATE_NO_WINDOW to prevent wsl.exe from modifying the Windows console mode,
    # which would otherwise disable Ctrl+C (ENABLE_PROCESSED_INPUT) for the Python CLI.
    CREATE_NO_WINDOW = 0x08000000
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW
    )

    shutdown_event = threading.Event()
    rfc2217_active_event = threading.Event()
    rfc2217_data_queue = queue.Queue()
    uf2_active_event = threading.Event()
    uf2_data_queue = queue.Queue()
    # Detect board type from USB VID/PID, unless overridden via --board
    # (covers boards whose USB-UART chip is not in the VID whitelist).
    if board and board != "auto":
        board_type = "unknown" if board == "none" else board
        logging.info(f"Board type set manually: {board_type}")
    else:
        board_type = detect_board_type(port)
        logging.info(f"Detected board type: {board_type} (VID-based)")

    # Determine USB Serial Number for hardware path matching
    usb_serial = get_usb_serial_number(port)
    if usb_serial:
        logging.info(f"Target USB Serial Number identified: {usb_serial}")
    else:
        logging.warning("Could not identify USB Serial Number. UF2 upload will fallback to first available RP2 drive.")

    # Start thread routing
    t_wsl_to_com = threading.Thread(target=read_wsl_stdout, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue), daemon=True)
    t_com_to_wsl = threading.Thread(target=read_com_port, args=(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event, usb_serial), daemon=True)
    t_wsl_stderr = threading.Thread(target=read_wsl_stderr, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue, usb_serial, board_type), daemon=True)

    t_wsl_to_com.start()
    t_com_to_wsl.start()
    t_wsl_stderr.start()

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
        print(f"{cyan}  Secondary bridge: PlatformIO env vars are owned by the first port.{reset}")
    print(f"{yellow}========================================================================{reset}\n")

    logging.info("Bridge is fully active. Press Ctrl+C to stop.")

    try:
        while not shutdown_event.is_set():
            if stop_event is not None and stop_event.is_set():
                logging.info(f"Stop requested; shutting down bridge for {port}.")
                break
            # Wait and check if the WSL process is still running
            if proc.poll() is not None:
                logging.info("WSL subprocess exited unexpectedly.")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        logging.info("Stopping bridge due to KeyboardInterrupt...")
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
        if proc.poll() is None:
            logging.info("Terminating WSL process...")
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logging.warning("WSL process did not exit. Killing it.")
                proc.kill()
        logging.info("Bridge stopped successfully.")

