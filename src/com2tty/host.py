import os
import sys
import time
import logging
import subprocess
import threading
import socket
import queue
import serial
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

def read_wsl_stdout(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue):
    logging.debug("WSL-to-COM thread started.")
    try:
        while not shutdown_event.is_set():
            data = proc.stdout.read(1024)
            if not data:
                logging.info("WSL process stdout reached EOF (exited).")
                break

            if rfc2217_active_event.is_set():
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

def read_com_port(ser, proc, shutdown_event, rfc2217_active_event):
    logging.debug("COM-to-WSL thread started.")
    try:
        while not shutdown_event.is_set():
            if rfc2217_active_event.is_set():
                time.sleep(0.1)
                continue

            # Short timeout allows periodic checking of shutdown_event
            data = ser.read(1024)
            if data and not rfc2217_active_event.is_set():
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

    Instead of relying on esptool's DTR/RTS commands (which must travel through
    TCP + pipe relay and suffer timing corruption), we execute the reset sequence
    directly on the Windows COM port. This wrapper silently absorbs esptool's
    DTR/RTS commands so they don't interfere with our manual reset.

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


def read_wsl_stderr(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue):
    """
    Reads control messages from WSL bridge's stderr.
    Handles dynamic serial settings, and RFC 2217 session lifecycle.
    """
    logging.debug("WSL stderr logging thread started.")

    redirector_stop = None
    redirector_thread = None

    def _start_rfc2217_session():
        nonlocal redirector_stop, redirector_thread

        logging.info("RFC 2217 client connected. PTY bridge suspended.")
        rfc2217_active_event.set()
        time.sleep(0.2)

        # Execute reset directly on Windows COM port
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

        logging.info("RFC 2217 client disconnected. PTY bridge resumed.")
        rfc2217_active_event.clear()

    try:
        while not shutdown_event.is_set():
            line = proc.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            if line_str.startswith("[CONTROL] SETTINGS:"):
                try:
                    settings_str = line_str.split(":", 1)[1].strip()
                    parts = settings_str.split()
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
                except Exception as e:
                    logging.error(f"Failed to apply dynamic settings: {e}")

            elif line_str.startswith("[CONTROL] RFC2217_READY"):
                port_str = line_str.split(":")[1] if ":" in line_str else "?"
                logging.info(f"RFC 2217 WSL Forwarder listening on 127.0.0.1:{port_str} in WSL")

            elif line_str.startswith("[CONTROL] RFC2217_CONNECT"):
                _start_rfc2217_session()

            elif line_str.startswith("[CONTROL] RFC2217_DISCONNECT"):
                _stop_rfc2217_session()

            elif line_str.startswith("[CONTROL] RFC2217_ERROR"):
                logging.warning(f"[WSL] {line_str}")

            else:
                logging.info(f"[WSL] {line_str}")

    except Exception as e:
        if not shutdown_event.is_set():
            logging.debug(f"Error in WSL stderr thread: {e}")


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

    # Start thread routing
    t_wsl_to_com = threading.Thread(target=read_wsl_stdout, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue), daemon=True)
    t_com_to_wsl = threading.Thread(target=read_com_port, args=(ser, proc, shutdown_event, rfc2217_active_event), daemon=True)
    t_wsl_stderr = threading.Thread(target=read_wsl_stderr, args=(proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue), daemon=True)

    t_wsl_to_com.start()
    t_com_to_wsl.start()
    t_wsl_stderr.start()

    yellow = "\033[93m"
    reset = "\033[0m"
    print(f"\n{yellow}========================================================================{reset}")
    print(f"{yellow}[WARNING] RFC 2217 auto-switching variables injected into ~/.bashrc{reset}")
    print(f"{yellow}Please OPEN A NEW WSL TERMINAL or run `source ~/.bashrc` to take effect!{reset}")
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

