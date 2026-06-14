"""Windows-side handlers for the WSL helper's ``[CONTROL]`` stderr protocol.

The serial bridge's stderr channel multiplexes three concerns: dynamic
serial-settings propagation, the RFC 2217 upload session lifecycle, and the
UF2 firmware relay. Each concern is a handler class registered on a
``ControlDispatcher`` (see ``com2tty.core.protocol``); ``read_wsl_stderr``
wires them together and runs the read loop on the helper's stderr.
"""
import logging
import os
import threading
import time

import serial

from ..core import protocol
from ..core.boards import UF2_FAMILIES
from ..core.constants import UF2_FLASH_FILENAME, UF2_INFO_FILENAME
from .board_reset import (
    esp32_manual_reset,
    pico_manual_reset,
    samd_touch_reset,
    stm32_manual_reset,
)
from .os_hacks.autoplay import AutoplaySuppressor
from .os_hacks.explorer import BootselWindowCloser, close_explorer_for_drive
from .rfc2217_redirector import QueuePipeConnection, Redirector
from .serial_host import (
    ResetProofSerial,
    acquire_new_port,
    reopen_serial_port,
    snapshot_ports,
)
from .uf2_flash import get_drive_by_serial, list_removable_drives, md5_hexdigest


class SettingsHandler:
    """Applies ``[CONTROL] SETTINGS`` changes from the WSL PTY to the COM port.

    The port handle can be transiently unavailable while a board resets, so
    the application is retried a few times, cycling the handle in between.
    """

    MAX_RETRIES = 3

    def __init__(self, ser, ser_lock):
        self._ser = ser
        self._lock = ser_lock

    def __call__(self, msg):
        if msg.payload is None:
            return
        parts = msg.payload.strip().split()

        for attempt in range(self.MAX_RETRIES):
            try:
                changes = self._apply(parts)
                if changes:
                    logging.info(f"Dynamic configuration change applied: {', '.join(changes)}")
                break  # Success — exit retry loop
            except (PermissionError, OSError) as e:
                if attempt < self.MAX_RETRIES - 1:
                    logging.warning(f"COM port temporarily unavailable while applying settings (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}")
                    self._cycle_handle()
                else:
                    logging.error(f"Failed to apply dynamic settings after {self.MAX_RETRIES} attempts: {e}")
            except Exception as e:
                logging.error(f"Failed to apply dynamic settings: {e}")
                break

    def _apply(self, parts):
        """Apply each k=v token under the port lock; returns what changed."""
        ser = self._ser
        changes = []
        with self._lock:
            for part in parts:
                # A malformed token (no '=') must not abort the whole update;
                # skip it and keep applying the remaining settings.
                if "=" not in part:
                    logging.warning(f"Skipping malformed settings token: {part!r}")
                    continue
                k, v = part.split("=", 1)
                if k == "baud" and v != "None":
                    new_baud = int(v)
                    # 0 is the termios B0 hangup pseudo-rate;
                    # applying it to a Windows handle fails and
                    # would needlessly cycle the port below.
                    if new_baud > 0 and ser.baudrate != new_baud:
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
                    # Full map: the CLI accepts S/M as well, so
                    # dynamic changes must not silently drop them.
                    parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD,
                                  "S": serial.PARITY_SPACE, "M": serial.PARITY_MARK}
                    target_parity = parity_map.get(v)
                    if target_parity and ser.parity != target_parity:
                        ser.parity = target_parity
                        changes.append(f"parity={v}")
                elif k == "stopbits":
                    stopbits_map = {"1": serial.STOPBITS_ONE, "1.5": serial.STOPBITS_ONE_POINT_FIVE, "2": serial.STOPBITS_TWO}
                    target_stopbits = stopbits_map.get(v)
                    if target_stopbits and ser.stopbits != target_stopbits:
                        ser.stopbits = target_stopbits
                        changes.append(f"stopbits={v}")
        return changes

    def _cycle_handle(self):
        logging.info("Attempting to reopen COM port handle...")
        with self._lock:
            try:
                self._ser.close()
            except Exception:
                pass
        time.sleep(1.0)
        with self._lock:
            try:
                self._ser.open()
                logging.info(f"COM port {self._ser.port} handle reopened.")
            except Exception as reopen_err:
                logging.warning(f"COM port reopen failed (will retry): {reopen_err}")


class Rfc2217SessionController:
    """RFC 2217 upload session lifecycle around the WSL forwarder.

    On connect: suspend the PTY bridge, run the board-specific
    bootloader-entry sequence, and wire a ``Redirector`` between the COM
    port and the stdio pipe. On disconnect: tear the redirector down and run
    the board-specific post-upload reset/reopen.
    """

    def __init__(self, proc, ser, board_type, usb_serial, shutdown_event,
                 rfc2217_active_event, rfc2217_data_queue, ser_lock=None):
        self._proc = proc
        self._ser = ser
        self._board_type = board_type
        self._usb_serial = usb_serial
        self._shutdown_event = shutdown_event
        self._active_event = rfc2217_active_event
        self._data_queue = rfc2217_data_queue
        # The same lock the dynamic-SETTINGS handler holds. Passing it upholds
        # reopen_serial_port's documented contract -- a reopen's close/open is
        # serialised against any other thread that cycles this handle -- rather
        # than letting it fall back to a private, uncoordinated lock.
        self._ser_lock = ser_lock
        self._redirector_stop = None
        self._redirector_thread = None
        # Application-port name to restore after a SAMD bootloader upload.
        self._samd_app_port = None

    def on_ready(self, msg):
        port_str = msg.payload if msg.payload is not None else "?"
        logging.info(f"RFC 2217 WSL Forwarder listening on 127.0.0.1:{port_str} in WSL")

    def on_error(self, msg):
        logging.warning(f"[WSL] {msg.raw}")
        if "bind failed" in msg.raw:
            logging.warning(
                "The RFC 2217 TCP port could not be opened in WSL "
                "(already in use?). Uploads will not work; pick a "
                "free port with --rfc2217-port."
            )

    def on_connect(self, msg):
        ser = self._ser
        logging.info("RFC 2217 client connected. PTY bridge suspended.")
        self._active_event.set()
        time.sleep(0.2)

        # Board-specific reset is done upfront based on VID detection
        if self._board_type == 'esp32':
            esp32_manual_reset(ser)
        elif self._board_type == 'samd':
            # Leonardo/SAMD-class boards enter their bootloader via the
            # 1200-baud touch, which closes the application port and
            # re-enumerates a separate bootloader port. Acquire that new
            # port so the upload (bossac over RFC 2217) talks to it.
            self._samd_app_port = ser.port
            before_ports = snapshot_ports()
            samd_touch_reset(ser)
            if acquire_new_port(ser, before_ports, self._shutdown_event) is None:
                logging.error("[SAMD] Bootloader port did not appear; the "
                              "upload will likely fail. Re-opening the "
                              "application port.")
                ser.port = self._samd_app_port
                try:
                    ser.open()
                except Exception:
                    pass

        settings = ser.get_settings()
        self._redirector_stop = threading.Event()

        safe_ser = ResetProofSerial(ser)
        qpc = QueuePipeConnection(self._proc, self._data_queue,
                                  self._redirector_stop)

        def _redirector_runner():
            r = Redirector(safe_ser, qpc)
            try:
                r.shortcircuit()
            except Exception as e:
                logging.debug(f"RFC 2217 session error: {e}")
            finally:
                r.stop()
                ser.apply_settings(settings)

        self._redirector_thread = threading.Thread(target=_redirector_runner,
                                                   daemon=True)
        self._redirector_thread.start()

    def on_disconnect(self, msg):
        ser = self._ser
        if self._redirector_stop:
            self._redirector_stop.set()
        if self._redirector_thread:
            self._redirector_thread.join(timeout=3.0)
            self._redirector_thread = None

        if self._board_type == 'esp32':
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
        elif self._board_type in UF2_FAMILIES:
            pico_manual_reset(ser)
        elif self._board_type == 'stm32':
            stm32_manual_reset(ser)
        elif self._board_type == 'samd':
            # bossac resets the board back into the application when it
            # finishes; the bootloader port disappears and the application
            # port re-enumerates. Restore the original name and reopen.
            if self._samd_app_port:
                ser.port = self._samd_app_port
            if not reopen_serial_port(ser, self._usb_serial,
                                      self._shutdown_event, max_attempts=60,
                                      ser_lock=self._ser_lock):
                logging.warning("[SAMD] Application port did not reappear "
                                "after upload.")
            self._samd_app_port = None

        logging.info("RFC 2217 client disconnected. PTY bridge resumed.")
        self._active_event.clear()


class Uf2UploadController:
    """UF2 firmware relay: receive the image over the stdout pipe, flash it.

    The WSL relay announces an upload with ``UF2_UPLOAD_START:<size>:<md5>``;
    the host acknowledges over stdin, accumulates the binary from the stdout
    pipe (routed into ``uf2_data_queue`` while ``uf2_active_event`` is set),
    verifies the checksum, and writes it to the BOOTSEL mass-storage drive
    with AutoPlay suppressed and Explorer windows held closed.
    """

    DATA_TIMEOUT = 1.0           # per-chunk queue wait
    DATA_TIMEOUT_RETRIES = 10    # consecutive timeouts before giving up
    DRIVE_SCAN_ATTEMPTS = 20     # x 0.5 s = up to 10 seconds
    REOPEN_ATTEMPTS = 60         # x 0.5 s = up to 30 s for post-flash reboot

    def __init__(self, proc, ser, board_type, usb_serial, shutdown_event,
                 uf2_active_event, uf2_data_queue, ser_lock=None):
        self._proc = proc
        self._ser = ser
        self._board_type = board_type
        self._usb_serial = usb_serial
        self._shutdown_event = shutdown_event
        self._active_event = uf2_active_event
        self._data_queue = uf2_data_queue
        # The same lock the dynamic-SETTINGS handler holds. Passing it upholds
        # reopen_serial_port's documented contract -- the post-flash reopen's
        # close/open is serialised against any other thread that cycles this
        # handle -- rather than falling back to a private, uncoordinated lock.
        self._ser_lock = ser_lock

    def on_ready(self, msg):
        port_str = msg.payload if msg.payload is not None else "?"
        logging.info(f"UF2 Relay Server listening on 127.0.0.1:{port_str} in WSL")

    def on_error(self, msg):
        logging.warning(f"[WSL] {msg.raw}")
        if "bind failed" in msg.raw:
            logging.warning(
                "The UF2 relay TCP port could not be opened in WSL "
                "(already in use?). UF2 uploads will not work; the "
                "relay listens on --rfc2217-port + 1, so pick a "
                "different --rfc2217-port."
            )

    def on_upload_start(self, msg):
        """Accumulate UF2 data from stdout pipe and flash to the correct drive."""
        info_str = msg.payload if msg.payload is not None else "0"
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
        self._active_event.set()

        # Send ACK to WSL bridge to signal we are ready for the data
        try:
            self._proc.stdin.write(protocol.UF2_ACK_LINE)
            self._proc.stdin.flush()
        except Exception as e:
            logging.error(f"[UF2] Failed to write ACK to WSL: {e}")
            self._active_event.clear()
            return

        uf2_data = self._receive(expected_size)
        logging.info(f"[UF2] Received {len(uf2_data)}/{expected_size} bytes.")

        # Verify MD5 checksum
        if expected_md5:
            received_md5 = md5_hexdigest(uf2_data)
            if received_md5 != expected_md5:
                logging.error(f"[UF2] MD5 checksum mismatch! Expected: {expected_md5}, Got: {received_md5}")
                return
            else:
                logging.info(f"[UF2] MD5 verification successful! Checksum: {received_md5}")

        # Hold AutoPlay/Explorer windows closed for the whole mount window.
        closer = None
        target_letters = []
        if os.name == 'nt':
            closer = BootselWindowCloser(target_letters)
            closer.start()

        try:
            with AutoplaySuppressor():
                # Trigger BOOTSEL mode directly from host — this is the reliable path.
                # The RFC2217 1200bps open/close from PlatformIO may not reliably
                # reach the COM port through the relay chain, so we do it ourselves.
                if self._board_type in UF2_FAMILIES:
                    pico_manual_reset(self._ser)

                logging.info("[UF2] Locating target drive...")
                self._flash(uf2_data, target_letters)
        finally:
            # Always stop the window-closer thread, even if the flash raises,
            # so it does not leak and keep polling Explorer indefinitely.
            if closer is not None:
                closer.stop()

    def on_upload_end(self, msg):
        # After UF2 flash, the Pico reboots and the COM port
        # disconnects/reconnects. The old Windows file handle is
        # permanently stale (ERROR_BAD_COMMAND). We must close
        # and reopen it. uf2_active_event is still set here, so
        # the COM and stdout pumps won't touch ser.
        if self._board_type in UF2_FAMILIES:
            logging.info(f"[UF2] Reopening {self._ser.port} after device reboot...")
            # Reboot after a UF2 flash can take 15-20 seconds: UF2
            # processing on the mass-storage drive, device reboot,
            # then USB CDC re-enumeration by Windows.
            if not reopen_serial_port(self._ser, self._usb_serial,
                                      self._shutdown_event,
                                      max_attempts=self.REOPEN_ATTEMPTS,
                                      ser_lock=self._ser_lock):
                logging.error("[UF2] COM port did not reappear after 30s. Bridge may not function until device reconnects.")
        self._active_event.clear()
        logging.info("[UF2] Upload pipeline complete.")

    def _receive(self, expected_size):
        """Drain the stdout-pipe queue until the full image (or timeout)."""
        uf2_data = bytearray()
        timeout_count = 0
        while len(uf2_data) < expected_size:
            try:
                chunk = self._data_queue.get(timeout=self.DATA_TIMEOUT)
                uf2_data.extend(chunk)
                timeout_count = 0
            except Exception:
                timeout_count += 1
                if timeout_count > self.DATA_TIMEOUT_RETRIES:
                    logging.error(f"[UF2] Timeout waiting for data. Got {len(uf2_data)}/{expected_size} bytes.")
                    break
        return uf2_data

    def _flash(self, uf2_data, target_letters=None):
        """Find the correct UF2 drive and write the firmware."""
        target_drive = None
        for attempt in range(self.DRIVE_SCAN_ATTEMPTS):
            if self._usb_serial:
                target_drive = get_drive_by_serial(self._usb_serial)

            # Fallback: scan for INFO_UF2.TXT
            if not target_drive:
                for d in list_removable_drives():
                    if os.path.exists(os.path.join(d, UF2_INFO_FILENAME)):
                        target_drive = d
                        break

            if target_drive and os.path.exists(os.path.join(target_drive, UF2_INFO_FILENAME)):
                break

            time.sleep(0.5)

        if target_drive:
            if target_letters is not None:
                dl = target_drive[0].upper()
                if dl not in target_letters:
                    target_letters.append(dl)

            # Close any Explorer windows Windows may have auto-opened for
            # this drive. (Guarded here rather than in the helper so the
            # platform check shares this module's os binding.)
            if os.name == 'nt':
                close_explorer_for_drive(target_drive)

            logging.info(f"[UF2] Flashing to drive {target_drive} ...")
            target_file = os.path.join(target_drive, UF2_FLASH_FILENAME)
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
                if os.name == 'nt':
                    close_explorer_for_drive(target_drive)
        else:
            logging.error("[UF2] Could not find a valid RP2 drive with INFO_UF2.TXT")


def build_control_dispatcher(proc, ser, shutdown_event, rfc2217_active_event,
                             rfc2217_data_queue, uf2_active_event,
                             uf2_data_queue, usb_serial, board_type,
                             ser_lock):
    """Wire the three handler groups onto a ControlDispatcher."""
    dispatcher = protocol.ControlDispatcher(
        fallback=lambda line: logging.info(f"[WSL] {line}"))

    dispatcher.register(protocol.SETTINGS, SettingsHandler(ser, ser_lock))

    rfc = Rfc2217SessionController(proc, ser, board_type, usb_serial,
                                   shutdown_event, rfc2217_active_event,
                                   rfc2217_data_queue, ser_lock=ser_lock)
    dispatcher.register(protocol.RFC2217_READY, rfc.on_ready)
    dispatcher.register(protocol.RFC2217_CONNECT, rfc.on_connect)
    dispatcher.register(protocol.RFC2217_DISCONNECT, rfc.on_disconnect)
    dispatcher.register(protocol.RFC2217_ERROR, rfc.on_error)

    uf2 = Uf2UploadController(proc, ser, board_type, usb_serial,
                              shutdown_event, uf2_active_event,
                              uf2_data_queue, ser_lock=ser_lock)
    dispatcher.register(protocol.UF2_READY, uf2.on_ready)
    dispatcher.register(protocol.UF2_UPLOAD_START, uf2.on_upload_start)
    dispatcher.register(protocol.UF2_UPLOAD_END, uf2.on_upload_end)
    dispatcher.register(protocol.UF2_ERROR, uf2.on_error)
    return dispatcher


def read_wsl_stderr(proc, ser, shutdown_event, rfc2217_active_event,
                    rfc2217_data_queue, uf2_active_event, uf2_data_queue,
                    usb_serial, board_type, ser_lock=None):
    """
    Reads control messages from WSL bridge's stderr.
    Handles dynamic serial settings, RFC 2217 session lifecycle, and UF2 uploads.
    """
    logging.debug("WSL stderr logging thread started.")
    if ser_lock is None:
        ser_lock = threading.Lock()

    dispatcher = build_control_dispatcher(
        proc, ser, shutdown_event, rfc2217_active_event, rfc2217_data_queue,
        uf2_active_event, uf2_data_queue, usb_serial, board_type, ser_lock)

    try:
        while not shutdown_event.is_set():
            line = proc.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            dispatcher.dispatch(line_str)
    except Exception as e:
        if not shutdown_event.is_set():
            logging.debug(f"Error in WSL stderr thread: {e}")
