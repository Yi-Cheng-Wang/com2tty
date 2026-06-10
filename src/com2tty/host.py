import os
import sys
import time
import logging
import subprocess
import threading
import socket
import queue
import serial
import serial.tools.list_ports
import json
try:
    import winreg
except ImportError:
    winreg = None
from .rfc2217_server import Redirector

def get_wsl_path(win_path):
    cmd = ["wsl", "wslpath", "-u", win_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e:
        logging.debug(f"wslpath failed: {e}. Using fallback conversion.")
        # Fallback to mounting convention /mnt/<drive>/...
        drive = win_path[0].lower()
        path = win_path[2:].replace("\\", "/")
        return f"/mnt/{drive}{path}"

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
        # mode.com prints system states for COM ports. 
        # The keys may be localized, but the baudrate value is consistently the first large number.
        res = subprocess.run(["mode.com", port], capture_output=True, text=True)
        if res.returncode == 0:
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

def read_com_port(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event):
    logging.debug("COM-to-WSL thread started.")
    try:
        while not shutdown_event.is_set():
            if rfc2217_active_event.is_set() or uf2_active_event.is_set():
                time.sleep(0.1)
                continue

            # Short timeout allows periodic checking of shutdown_event
            try:
                data = ser.read(1024)
            except (PermissionError, OSError):
                # COM port may be temporarily unavailable during board reset
                # (e.g. Pico rebooting into BOOTSEL). Just retry.
                if not shutdown_event.is_set():
                    time.sleep(0.5)
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


def esp32_manual_reset(ser):
    """Execute the classic ESP32 auto-reset sequence directly on the COM port."""
    logging.info("[ESP32] Executing manual reset for download mode...")
    try:
        ser.dtr = False  # IO0 = HIGH
        ser.rts = True   # EN = LOW (hold in reset)
        time.sleep(0.1)  # Let capacitor discharge
        ser.dtr = True   # IO0 = LOW (download mode signal)
        ser.rts = False  # EN = HIGH (release reset -> boot with IO0 LOW)
        time.sleep(0.05) # Wait for boot
        ser.dtr = False  # IO0 = HIGH (release GPIO0)
        logging.info("[ESP32] Manual reset complete. Chip should be in download mode.")
    except Exception as e:
        logging.error(f"[ESP32] Manual reset failed: {e}")


class AutoplaySuppressor:
    """Temporarily disables Windows AutoPlay to prevent Explorer windows from popping up during device reboot."""
    def __init__(self):
        self.key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers"
        self.value_name = "DisableAutoplay"
        self.original_value = None
        self.existed = False
        self.modified = False

    def __enter__(self):
        if not winreg:
            return self
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            try:
                self.original_value, val_type = winreg.QueryValueEx(key, self.value_name)
                self.existed = True
            except FileNotFoundError:
                self.existed = False
                self.original_value = 0
            
            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
            self.modified = True
        except Exception as e:
            logging.debug(f"Failed to temporarily disable AutoPlay: {e}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not winreg or not self.modified:
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_WRITE)
            if self.existed:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_DWORD, self.original_value)
            else:
                winreg.DeleteValue(key, self.value_name)
            winreg.CloseKey(key)
        except Exception as e:
            logging.debug(f"Failed to restore AutoPlay settings: {e}")


def pico_manual_reset(ser):
    """Trigger RP2040/RP2350 BOOTSEL mode via the classic 1200-baud touch."""
    logging.info("[UF2] Performing 1200-baud reset to enter BOOTSEL mode...")
    try:
        old_baud = getattr(ser, 'baudrate', 115200)
        ser.dtr = True    # Ensure DTR HIGH first
        ser.baudrate = 1200
        time.sleep(0.1)
        ser.dtr = False   # DTR LOW triggers BOOTSEL via CDC driver
        time.sleep(0.5)   # Wait for device to reboot into BOOTSEL
        
        # The device has rebooted into BOOTSEL, so the old COM port handle is stale.
        # We MUST close it and restore the original baudrate state. If we don't, 
        # when we reopen the new COM port after the flash, pyserial will apply 
        # baudrate=1200 again, which will IMMEDIATELY trigger another reboot 
        # back into BOOTSEL mode!
        try:
            ser.close()
        except Exception:
            pass
            
        # Restore the baudrate and DTR properties safely while the port is closed.
        ser.baudrate = old_baud if old_baud != 1200 else 115200
        ser.dtr = True  # Ensure DTR is asserted when reopened so CDC driver sends data
        
        logging.info("[UF2] 1200-baud reset complete. Device should be in BOOTSEL mode.")
    except Exception as e:
        logging.error(f"[UF2] Failed to trigger BOOTSEL mode: {e}")


def detect_board_type(port_name):
    """Detect board type from USB VID/PID at startup. Much more reliable than runtime detection."""
    for port in serial.tools.list_ports.comports():
        if port.device == port_name:
            if port.vid == 0x2E8A:  # Raspberry Pi USB VID
                return 'pico'
            elif port.vid in (0x10C4, 0x1A86, 0x0403, 0x067B, 0x303A):
                # Silicon Labs, QinHeng, FTDI, Prolific, Espressif
                return 'esp32'
    return 'unknown'


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
        elif board_type == 'pico':
            pico_manual_reset(ser)

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
            import hashlib
            received_md5 = hashlib.md5(uf2_data).hexdigest()
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
                            # Match known volume labels or the target drive letter
                            match = "RPI-RP2" in t_val or "RP2350" in t_val
                            if not match and target_letters:
                                for dl in target_letters:
                                    if f"{dl}:" in t_val or f"({dl}:)" in t_val:
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
            if board_type == 'pico':
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
            targets = ["RPI-RP2", "RP2350", f"{dl}:", f"({dl}:)"]

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
        import string
        target_drive = None
        for attempt in range(20):  # Up to 10 seconds
            if usb_serial_num:
                target_drive = get_drive_by_serial(usb_serial_num)

            # Fallback: scan for INFO_UF2.TXT
            if not target_drive:
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
                for d in drives:
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
                if board_type == 'pico':
                    port_name = ser.port
                    logging.info(f"[UF2] Reopening {port_name} after device reboot...")
                    try:
                        ser.close()
                    except Exception:
                        pass
                        
                    # Double check that we don't trigger BOOTSEL during reconnect!
                    if getattr(ser, 'baudrate', 115200) == 1200:
                        ser.baudrate = 115200
                        
                    reopened = False
                    # Pico reboot after UF2 flash can take 15-20 seconds:
                    #   - UF2 processing on mass storage drive
                    #   - Device reboot
                    #   - USB CDC re-enumeration by Windows
                    for i in range(60):  # Up to 30 seconds
                        time.sleep(0.5)
                        # First try the original port name
                        try:
                            ser.open()
                            logging.info(f"[UF2] {port_name} reopened successfully.")
                            reopened = True
                            break
                        except Exception:
                            pass
                            
                        # If original port didn't work, scan by USB serial number
                        # (port number may have changed after reboot)
                        if usb_serial:
                            for p in serial.tools.list_ports.comports():
                                if p.serial_number == usb_serial and p.device != port_name:
                                    logging.info(f"[UF2] Device reappeared as {p.device} (was {port_name}).")
                                    ser.port = p.device
                                    try:
                                        ser.open()
                                        logging.info(f"[UF2] {p.device} opened successfully.")
                                        reopened = True
                                        break
                                    except Exception:
                                        pass
                            if reopened:
                                break
                                
                    if not reopened:
                        logging.error(f"[UF2] COM port did not reappear after 30s. Bridge may not function until device reconnects.")
                uf2_active_event.clear()
                logging.info("[UF2] Upload pipeline complete.")

            elif line_str.startswith("[CONTROL] UF2_ERROR"):
                logging.warning(f"[WSL] {line_str}")

            else:
                logging.info(f"[WSL] {line_str}")

    except Exception as e:
        if not shutdown_event.is_set():
            logging.debug(f"Error in WSL stderr thread: {e}")


def get_usb_serial_number(port_name):
    """Find the USB Serial Number (hwid SER=...) for a given COM port."""
    for port in serial.tools.list_ports.comports():
        if port.device == port_name:
            # hwid format example: USB VID:PID=2E8A:F00F SER=98C4FFA253A63FB7 LOCATION=1-6:x.0
            if 'SER=' in port.hwid:
                parts = port.hwid.split()
                for part in parts:
                    if part.startswith('SER='):
                        return part[4:]
    return None


def get_drive_by_serial(serial_num):
    """Use PowerShell/CIM to map a USB Serial Number to a logical Windows Drive Letter."""
    ps_cmd = r'''
$drives = Get-CimInstance Win32_DiskDrive
$partitions = Get-Partition
$result = @()
foreach ($d in $drives) {
    if ($d.PNPDeviceID -match "%s") {
        foreach ($p in $partitions) {
            if ($p.DiskNumber -eq $d.Index -and $p.DriveLetter) {
                $result += [PSCustomObject]@{DriveLetter=($p.DriveLetter + ":\"); PNPDeviceID=$d.PNPDeviceID}
            }
        }
    }
}
$result | ConvertTo-Json -Compress
    ''' % serial_num.replace("'", "''")
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, creationflags=0x08000000)
        output = res.stdout.strip()
        if not output:
            return None
        
        data = json.loads(output)
        if isinstance(data, dict):
            return data.get('DriveLetter')
        elif isinstance(data, list) and len(data) > 0:
            return data[0].get('DriveLetter')
    except Exception as e:
        logging.debug(f"Failed to map USB serial to drive: {e}")
    return None



def run_gamepad_bridge(pad_index=0, poll_hz=250, name="Microsoft X-Box 360 pad",
                       use_uinput=False, tmp_path="/tmp/com2pad0"):
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

    wsl_pad_path = get_wsl_path(pad_script)
    logging.info(f"WSL gamepad bridge script resolved to: {wsl_pad_path}")

    cmd = ["wsl", "python3", "-u", wsl_pad_path,
           "--pad-index", str(pad_index), "--name", name,
           "--tmp-path", tmp_path]
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
        """Reverse channel (reserved for future rumble); drain to avoid blocking."""
        try:
            while not shutdown_event.is_set():
                if not proc.stdout.read(1):
                    break
        except Exception:
            pass

    t_logs = threading.Thread(target=read_wsl_logs, daemon=True)
    t_out = threading.Thread(target=drain_wsl_stdout, daemon=True)
    t_logs.start()
    t_out.start()

    yellow = "\033[93m"
    cyan = "\033[96m"
    green = "\033[92m"
    reset = "\033[0m"
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


def run_bridge(port, baud, wsl_tty, bytesize, parity, stopbits, xonxoff, rtscts, dsrdtr, rfc2217_port):
    # Resolve serial settings
    ser_bytesize, ser_parity, ser_stopbits = get_serial_settings(bytesize, parity, stopbits)

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

    # Locate WSL bridge.py script
    current_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_script = os.path.join(current_dir, "bridge.py")
    if not os.path.exists(bridge_script):
        raise FileNotFoundError(f"WSL bridge script not found at: {bridge_script}")

    wsl_bridge_path = get_wsl_path(bridge_script)
    logging.info(f"WSL bridge script resolved to: {wsl_bridge_path}")

    cmd = ["wsl", "python3", "-u", wsl_bridge_path, "--symlink", wsl_tty, "--rfc2217-port", str(rfc2217_port)]
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
    # Detect board type from USB VID/PID
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
    t_com_to_wsl = threading.Thread(target=read_com_port, args=(ser, proc, shutdown_event, rfc2217_active_event, uf2_active_event), daemon=True)
    t_wsl_stderr = threading.Thread(target=read_wsl_stderr, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue, uf2_active_event, uf2_data_queue, usb_serial, board_type), daemon=True)

    t_wsl_to_com.start()
    t_com_to_wsl.start()
    t_wsl_stderr.start()

    yellow = "\033[93m"
    cyan = "\033[96m"
    green = "\033[92m"
    reset = "\033[0m"
    board_label = {'pico': 'RP2040/RP2350 (Pico)', 'esp32': 'ESP32', 'unknown': 'Unknown'}.get(board_type, board_type)
    print(f"\n{yellow}========================================================================{reset}")
    print(f"{yellow}  com2tty Bridge Active - {port}{reset}")
    print(f"{yellow}------------------------------------------------------------------------{reset}")
    print(f"{cyan}  Board type (VID)     : {green}{board_label}{reset}")
    print(f"{cyan}  RFC 2217 upload port : rfc2217://127.0.0.1:{rfc2217_port} (in WSL){reset}")
    print(f"{cyan}  UF2 relay port       : 127.0.0.1:{rfc2217_port + 1} (in WSL){reset}")
    print(f"{cyan}  USB Serial Number    : {usb_serial or 'N/A (fallback mode)'}{reset}")
    print(f"{cyan}  Picotool interceptor : {'Active' if board_type == 'pico' else 'N/A'}{reset}")
    print(f"{yellow}------------------------------------------------------------------------{reset}")
    print(f"{yellow}  [WARNING] Environment variables injected into ~/.bashrc{reset}")
    print(f"{yellow}  Please OPEN A NEW WSL TERMINAL or run `source ~/.bashrc`{reset}")
    print(f"{yellow}========================================================================{reset}\n")

    logging.info("Bridge is fully active. Press Ctrl+C to stop.")

    try:
        while not shutdown_event.is_set():
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

