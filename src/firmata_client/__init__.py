"""
firmata-client — Minimal Firmata protocol client for Arduino.

Requires only pyserial. Target board: StandardFirmata (UNO / MEGA).

Basic usage:
    from firmata_client import FirmataClient, PinMode

    client = FirmataClient("COM9")            # Windows
    # client = FirmataClient("/dev/ttyUSB0")  # Linux

    info = client.query_board_info()
    print(info["firmware"], info["version"])

    client.set_pin_mode(13, PinMode.OUTPUT)
    client.digital_write(13, True)
    client.close()
"""

from .client import Cmd, FirmataClient, PinMode, Sysex

__version__ = "0.1.0"
__all__ = ["FirmataClient", "PinMode", "Cmd", "Sysex"]
