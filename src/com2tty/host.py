import os
import sys
import time
import logging
import subprocess
import threading
import serial

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

def read_wsl_stdout(proc, ser, shutdown_event):
    logging.debug("WSL-to-COM thread started.")
    try:
        while not shutdown_event.is_set():
            data = proc.stdout.read(1024)
            if not data:
                logging.info("WSL process stdout reached EOF (exited).")
                break
            logging.debug(f"WSL -> COM: {len(data)} bytes")
            ser.write(data)
            ser.flush()
    except Exception as e:
        if not shutdown_event.is_set():
            logging.error(f"Error in WSL-to-COM thread: {e}")
    finally:
        shutdown_event.set()

def read_com_port(ser, proc, shutdown_event):
    logging.debug("COM-to-WSL thread started.")
    try:
        while not shutdown_event.is_set():
            # Short timeout allows periodic checking of shutdown_event
            data = ser.read(1024)
            if data:
                logging.debug(f"COM -> WSL: {len(data)} bytes")
                proc.stdin.write(data)
                proc.stdin.flush()
    except Exception as e:
        if not shutdown_event.is_set():
            logging.error(f"Error in COM-to-WSL thread: {e}")
    finally:
        shutdown_event.set()

def read_wsl_stderr(proc, shutdown_event):
    logging.debug("WSL stderr logging thread started.")
    try:
        while not shutdown_event.is_set():
            line = proc.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str:
                logging.info(f"[WSL] {line_str}")
    except Exception as e:
        if not shutdown_event.is_set():
            logging.debug(f"Error in WSL stderr thread: {e}")

def run_bridge(port, baud, wsl_tty, bytesize, parity, stopbits, xonxoff, rtscts, dsrdtr):
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
    
    cmd = ["wsl", "python3", "-u", wsl_bridge_path, "--symlink", wsl_tty]
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
    
    # Start thread routing
    t_wsl_to_com = threading.Thread(target=read_wsl_stdout, args=(proc, ser, shutdown_event), daemon=True)
    t_com_to_wsl = threading.Thread(target=read_com_port, args=(ser, proc, shutdown_event), daemon=True)
    t_wsl_stderr = threading.Thread(target=read_wsl_stderr, args=(proc, shutdown_event), daemon=True)
    
    t_wsl_to_com.start()
    t_com_to_wsl.start()
    t_wsl_stderr.start()
    
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
