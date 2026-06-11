"""Board family detection and reset/bootloader-entry sequences.

The Windows host performs these directly on the COM port; the WSL side never
needs hardware access. Detection is VID-based because it is far more reliable
than runtime protocol sniffing; use --board to override when a board fronts
its UART with a chip that is not in the map.
"""
import logging
import time

import serial.tools.list_ports

# USB vendor IDs -> board family. Generic USB-UART vendors (Silicon Labs,
# QinHeng, FTDI, Prolific) are classified as 'esp32' because the ESP32
# DTR/RTS auto-reset is the useful default for the boards they typically
# front; override with --board when that guess is wrong.
BOARD_VID_MAP = {
    0x2E8A: "pico",   # Raspberry Pi RP2040/RP2350
    0x10C4: "esp32",  # Silicon Labs CP210x
    0x1A86: "esp32",  # QinHeng CH340/CH9102
    0x0403: "esp32",  # FTDI
    0x067B: "esp32",  # Prolific PL2303
    0x303A: "esp32",  # Espressif native USB
    0x239A: "nrf52",  # Adafruit nRF52 (UF2 mass-storage bootloader)
    0x2341: "samd",   # Arduino (Leonardo/Micro/Zero/MKR)
    0x2886: "samd",   # Seeed Studio
    0x1B4F: "samd",   # SparkFun
    0x0483: "stm32",  # STMicroelectronics
}

# Families whose firmware arrives as a UF2 file on a mass-storage bootloader
# and therefore go through the picotool interception / UF2 relay path.
UF2_FAMILIES = ("pico", "nrf52")

BOARD_LABELS = {
    "pico": "RP2040/RP2350 (Pico)",
    "esp32": "ESP32",
    "nrf52": "nRF52 (UF2 bootloader)",
    "samd": "SAMD/AVR (1200-baud touch)",
    "stm32": "STM32",
    "unknown": "Unknown",
}

BOARD_CHOICES = ["auto", "esp32", "pico", "nrf52", "samd", "stm32", "none"]


def classify_vid(vid):
    """Map a USB vendor id to a board family name ('unknown' when absent)."""
    if vid is None:
        return "unknown"
    return BOARD_VID_MAP.get(vid, "unknown")


def detect_board_type(port_name):
    """Detect board type from USB VID/PID at startup. Much more reliable than runtime detection."""
    for port in serial.tools.list_ports.comports():
        if port.device == port_name:
            return classify_vid(port.vid)
    return "unknown"


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


def samd_touch_reset(ser):
    """Enter the SAMD/AVR (Arduino Leonardo-style) bootloader via the
    1200-baud touch.

    Unlike the Pico, the bootloader re-enumerates as a *new CDC serial port*
    (no mass-storage drive), and the upload tool (bossac/avrdude) then talks
    to it over serial -- which flows through the RFC 2217 forwarder. The
    sequence is the same 1200-baud open/close with DTR de-asserted.
    """
    logging.info("[SAMD] Performing 1200-baud touch to enter bootloader...")
    try:
        old_baud = getattr(ser, 'baudrate', 115200)
        ser.dtr = True
        ser.baudrate = 1200
        time.sleep(0.1)
        ser.dtr = False   # DTR LOW at 1200 baud triggers the bootloader
        time.sleep(0.5)   # Wait for re-enumeration

        # Same stale-handle rules as the Pico touch: close, then restore the
        # baudrate while closed so a later reopen does not re-trigger it.
        try:
            ser.close()
        except Exception:
            pass
        ser.baudrate = old_baud if old_baud != 1200 else 115200
        ser.dtr = True

        logging.info("[SAMD] 1200-baud touch complete. Bootloader port should appear.")
    except Exception as e:
        logging.error(f"[SAMD] Failed to enter bootloader: {e}")


def stm32_manual_reset(ser):
    """Generic DTR/RTS hard-reset pulse for STM32-class boards.

    Common USB-UART wiring ties RTS to NRST (and DTR to BOOT0). This pulses
    reset so the board reboots; entering the ROM bootloader additionally needs
    BOOT0 high, which we assert via DTR during the pulse for boards wired the
    usual way. Boards using ST-LINK or DFU instead are unaffected by this and
    simply reboot.
    """
    logging.info("[STM32] Pulsing reset (RTS=NRST, DTR=BOOT0 wiring assumed)...")
    try:
        ser.dtr = True   # BOOT0 high on conventionally wired adapters
        ser.rts = True   # NRST low (hold in reset)
        time.sleep(0.1)
        ser.rts = False  # release reset
        time.sleep(0.1)
        ser.dtr = False
        logging.info("[STM32] Reset pulse complete.")
    except Exception as e:
        logging.error(f"[STM32] Reset failed: {e}")
