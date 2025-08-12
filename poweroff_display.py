#!/usr/bin/env python3
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106

try:
    serial = i2c(port=1, address=0x3C)
    dev = sh1106(serial)
    dev.clear()
    dev.show()
    dev.command(0xAE)  # Display OFF
except Exception:
    pass  # Ignore errors during shutdown
