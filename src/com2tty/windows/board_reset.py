"""Board-family reset and bootloader-entry sequences.

The Windows host performs these directly on the COM port; the WSL side never
needs hardware access. Each sequence is a small strategy keyed by the board
family detected from the USB VID (see ``com2tty.core.boards``); timing
parameters live there too so both the data and its rationale stay together.
"""
import logging
import time

from ..core.boards import (
    ESP32_BOOT_WAIT,
    ESP32_RESET_HOLD,
    SAFE_FALLBACK_BAUDRATE,
    STM32_RESET_PULSE,
    TOUCH_BAUDRATE,
    TOUCH_DWELL,
    TOUCH_REENUMERATE_WAIT,
)


def esp32_manual_reset(ser):
    """Execute the classic ESP32 auto-reset sequence directly on the COM port."""
    logging.info("[ESP32] Executing manual reset for download mode...")
    try:
        ser.dtr = False  # IO0 = HIGH
        ser.rts = True   # EN = LOW (hold in reset)
        time.sleep(ESP32_RESET_HOLD)   # Let capacitor discharge
        ser.dtr = True   # IO0 = LOW (download mode signal)
        ser.rts = False  # EN = HIGH (release reset -> boot with IO0 LOW)
        time.sleep(ESP32_BOOT_WAIT)    # Wait for boot
        ser.dtr = False  # IO0 = HIGH (release GPIO0)
        logging.info("[ESP32] Manual reset complete. Chip should be in download mode.")
    except Exception as e:
        logging.error(f"[ESP32] Manual reset failed: {e}")


def pico_manual_reset(ser):
    """Trigger RP2040/RP2350 BOOTSEL mode via the classic 1200-baud touch."""
    logging.info("[UF2] Performing 1200-baud reset to enter BOOTSEL mode...")
    try:
        old_baud = getattr(ser, 'baudrate', SAFE_FALLBACK_BAUDRATE)
        ser.dtr = True    # Ensure DTR HIGH first
        ser.baudrate = TOUCH_BAUDRATE
        time.sleep(TOUCH_DWELL)
        ser.dtr = False   # DTR LOW triggers BOOTSEL via CDC driver
        time.sleep(TOUCH_REENUMERATE_WAIT)  # Wait for device to reboot into BOOTSEL

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
        ser.baudrate = old_baud if old_baud != TOUCH_BAUDRATE else SAFE_FALLBACK_BAUDRATE
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
        old_baud = getattr(ser, 'baudrate', SAFE_FALLBACK_BAUDRATE)
        ser.dtr = True
        ser.baudrate = TOUCH_BAUDRATE
        time.sleep(TOUCH_DWELL)
        ser.dtr = False   # DTR LOW at 1200 baud triggers the bootloader
        time.sleep(TOUCH_REENUMERATE_WAIT)  # Wait for re-enumeration

        # Same stale-handle rules as the Pico touch: close, then restore the
        # baudrate while closed so a later reopen does not re-trigger it.
        try:
            ser.close()
        except Exception:
            pass
        ser.baudrate = old_baud if old_baud != TOUCH_BAUDRATE else SAFE_FALLBACK_BAUDRATE
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
        time.sleep(STM32_RESET_PULSE)
        ser.rts = False  # release reset
        time.sleep(STM32_RESET_PULSE)
        ser.dtr = False
        logging.info("[STM32] Reset pulse complete.")
    except Exception as e:
        logging.error(f"[STM32] Reset failed: {e}")
