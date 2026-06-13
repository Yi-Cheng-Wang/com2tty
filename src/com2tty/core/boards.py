"""Board family data: USB VID classification and reset timing parameters.

Pure data and pure functions only -- the actual reset sequences (which need
a live pyserial handle) live on the Windows side. Detection is VID-based
because it is far more reliable than runtime protocol sniffing; use --board
to override when a board fronts its UART with a chip that is not in the map.
"""

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

# --------------------------------------------------------------------------
# Reset / bootloader-entry timing (seconds unless noted)
# --------------------------------------------------------------------------

#: The bootloader-touch pseudo baud rate. Opening (or reconfiguring) a CDC
#: port at 1200 baud and dropping DTR asks Pico/SAMD-class firmware to
#: reboot into its bootloader -- which is also why the bridge must never
#: *reopen* a port at 1200 baud accidentally.
TOUCH_BAUDRATE = 1200

#: Baud rate restored when a port was last configured at the touch rate.
SAFE_FALLBACK_BAUDRATE = 115200

#: ESP32 auto-reset: hold EN low this long (capacitor discharge), then the
#: short wait after releasing reset with IO0 low.
ESP32_RESET_HOLD = 0.1
ESP32_BOOT_WAIT = 0.05

#: ESP32 post-upload hard reset: EN low pulse, then boot settle.
ESP32_POST_UPLOAD_RESET_PULSE = 0.2
ESP32_POST_UPLOAD_BOOT_WAIT = 0.1

#: 1200-baud touch (Pico BOOTSEL and SAMD bootloader): dwell at 1200 baud
#: before dropping DTR, then the wait for the device to re-enumerate.
TOUCH_DWELL = 0.1
TOUCH_REENUMERATE_WAIT = 0.5

#: STM32 DTR/RTS pulse: NRST hold and post-release settle.
STM32_RESET_PULSE = 0.1


def classify_vid(vid):
    """Map a USB vendor id to a board family name ('unknown' when absent)."""
    if vid is None:
        return "unknown"
    return BOARD_VID_MAP.get(vid, "unknown")
