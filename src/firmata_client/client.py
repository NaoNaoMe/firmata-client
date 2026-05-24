"""
firmata_client.py
~~~~~~~~~~~~~~~~~
Minimal Firmata client. Dependency: pyserial only.
Target: StandardFirmata (UNO / MEGA)
"""

import logging
import threading
import time
from enum import IntEnum
from typing import Optional

import serial

_log = logging.getLogger("firmata")
_log_serial = logging.getLogger("firmata.serial")


class Cmd(IntEnum):
    """Firmata command bytes."""

    DIGITAL_MESSAGE = 0x90
    ANALOG_MESSAGE = 0xE0
    REPORT_ANALOG = 0xC0
    REPORT_DIGITAL = 0xD0
    SET_PIN_MODE = 0xF4
    SET_DIGITAL_PIN_VALUE = 0xF5
    REPORT_VERSION = 0xF9
    SYSTEM_RESET = 0xFF
    START_SYSEX = 0xF0
    END_SYSEX = 0xF7


class Sysex(IntEnum):
    """Firmata SysEx sub-command bytes."""

    ANALOG_MAPPING_QUERY = 0x69
    ANALOG_MAPPING_RESPONSE = 0x6A
    CAPABILITY_QUERY = 0x6B
    CAPABILITY_RESPONSE = 0x6C
    FIRMWARE_QUERY = 0x79


class PinMode(IntEnum):
    """Firmata pin mode values."""

    INPUT = 0x00
    OUTPUT = 0x01
    ANALOG = 0x02
    PWM = 0x03
    SERVO = 0x04


class FirmataClient:
    """Thin wrapper around the Firmata protocol over a serial port.

    Incoming messages are processed in a background thread; the latest values
    are cached and accessible from the main thread at any time.
    """

    def __init__(self, port: str, baudrate: int = 57600, timeout: float = 5.0) -> None:
        """Open the serial port and start the background reader thread."""
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0.1)
        self._timeout = timeout
        self._lock = threading.Lock()
        self.firmware_name: Optional[str] = None
        self.firmware_version: Optional[tuple] = None
        self.capabilities: dict = {}
        self.analog_mapping: dict = {}
        self._pin_values: dict = {}
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        _log.debug("Waiting for Arduino to boot ...")
        time.sleep(2.0)

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._running = False
        self._serial.close()
        _log.debug("Serial port closed.")

    def _send(self, data: bytes) -> None:
        """Write raw bytes to the serial port."""
        _log_serial.debug("TX: %s", data.hex(" "))
        with self._lock:
            self._serial.write(data)

    def _send_sysex(self, command: int, payload: bytes = b"") -> None:
        """Wrap a payload in a SysEx frame and send it."""
        self._send(bytes([Cmd.START_SYSEX, command]) + payload + bytes([Cmd.END_SYSEX]))

    def _wait_until(self, condition, label: str):
        """Poll until condition() is truthy or timeout is reached."""
        deadline = time.time() + self._timeout
        while not condition():
            if time.time() > deadline:
                raise TimeoutError(f"Timeout waiting for '{label}' response")
            time.sleep(0.05)

    def query_board_info(self) -> dict:
        """Fetch firmware name/version and pin capabilities in one call."""
        _log.debug("Querying firmware ...")
        self._send_sysex(Sysex.FIRMWARE_QUERY)
        self._wait_until(lambda: self.firmware_name is not None, "firmware")

        _log.debug("Querying capabilities ...")
        self._send_sysex(Sysex.CAPABILITY_QUERY)
        self._wait_until(lambda: self.capabilities, "capability")

        _log.debug("Querying analog mapping ...")
        self._send_sysex(Sysex.ANALOG_MAPPING_QUERY)
        self._wait_until(lambda: self.analog_mapping, "analog_mapping")

        return {
            "firmware": self.firmware_name,
            "version": self.firmware_version,
            "capabilities": self.capabilities,
            "analog_mapping": self.analog_mapping,
        }

    def set_pin_mode(self, pin: int, mode: PinMode) -> None:
        """Set the mode of a pin (INPUT, OUTPUT, ANALOG, PWM, SERVO)."""
        _log.debug("set_pin_mode  pin=%d  mode=%s", pin, mode.name)
        self._send(bytes([Cmd.SET_PIN_MODE, pin, int(mode)]))

    def digital_write(self, pin: int, value: bool) -> None:
        """Write a digital HIGH/LOW value using SET_DIGITAL_PIN_VALUE (0xF5)."""
        _log.debug("digital_write pin=%d  value=%s", pin, value)
        self._send(bytes([Cmd.SET_DIGITAL_PIN_VALUE, pin, int(value)]))

    def analog_write(self, pin: int, value: int) -> None:
        """Write a PWM/servo value via EXTENDED_ANALOG SysEx. PWM: 0-255, Servo: 0-180."""
        _log.debug("analog_write  pin=%d  value=%d", pin, value)
        lsb = value & 0x7F
        msb = (value >> 7) & 0x7F
        self._send_sysex(0x6F, bytes([pin, lsb, msb]))

    def digital_read(self, pin: int) -> Optional[int]:
        """Return the latest cached digital input value (report_digital must be enabled)."""
        return self._pin_values.get(pin)

    def analog_read(self, channel: int) -> Optional[int]:
        """Return the latest cached analog input value (report_analog must be enabled)."""
        return self._pin_values.get(f"a{channel}")

    def report_analog(self, channel: int, enable: bool) -> None:
        """Enable or disable automatic analog value reporting for a channel."""
        _log.debug("report_analog ch=%d  enable=%s", channel, enable)
        self._send(bytes([Cmd.REPORT_ANALOG | (channel & 0x0F), int(enable)]))

    def report_digital(self, port: int, enable: bool) -> None:
        """Enable or disable automatic digital port reporting."""
        _log.debug("report_digital port=%d  enable=%s", port, enable)
        self._send(bytes([Cmd.REPORT_DIGITAL | (port & 0x0F), int(enable)]))

    def _read_loop(self) -> None:
        """Background thread: continuously read serial data and feed the parser."""
        buf = bytearray()
        while self._running:
            try:
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if chunk:
                    _log_serial.debug("RX: %s", chunk.hex(" "))
                    buf.extend(chunk)
                    buf = self._parse(buf)
            except Exception as exc:
                _log.warning("Read error: %s", exc)

    def _parse(self, buf: bytearray) -> bytearray:
        """Consume and dispatch complete messages from buf; return unconsumed bytes."""
        while buf:
            n = self._dispatch_one(buf)
            if n is None:
                break
            buf = buf[n:]
        return buf

    def _dispatch_one(self, buf: bytearray) -> Optional[int]:
        """Dispatch the first message in buf; return bytes consumed, or None if incomplete."""
        b = buf[0]
        if b == Cmd.START_SYSEX:
            end = buf.find(Cmd.END_SYSEX)
            if end == -1:
                return None
            self._on_sysex(buf[1:end])
            return end + 1
        if b == Cmd.REPORT_VERSION:
            if len(buf) < 3:
                return None
            self._on_report_version(buf[1], buf[2])
            return 3
        if 0x90 <= b <= 0x9F:
            return self._dispatch_digital(b, buf)
        if 0xE0 <= b <= 0xEF:
            return self._dispatch_analog(b, buf)
        _log.debug("Unknown byte: 0x%02X - skipping", b)
        return 1

    def _dispatch_digital(self, b: int, buf: bytearray) -> Optional[int]:
        """Decode a DIGITAL_MESSAGE; return bytes consumed, or None if incomplete."""
        if len(buf) < 3:
            return None
        port = b & 0x0F
        value = buf[1] | (buf[2] << 7)
        for bit in range(8):
            self._pin_values[port * 8 + bit] = (value >> bit) & 1
        return 3

    def _dispatch_analog(self, b: int, buf: bytearray) -> Optional[int]:
        """Decode an ANALOG_MESSAGE; return bytes consumed, or None if incomplete."""
        if len(buf) < 3:
            return None
        ch = b & 0x0F
        self._pin_values[f"a{ch}"] = buf[1] | (buf[2] << 7)
        return 3

    def _on_report_version(self, major: int, minor: int) -> None:
        """Handle a REPORT_VERSION message."""
        self.firmware_version = (major, minor)
        _log.debug("REPORT_VERSION: %d.%d", major, minor)

    def _on_sysex(self, data: bytearray) -> None:
        """Dispatch an incoming SysEx message to the appropriate handler."""
        if not data:
            return
        cmd = data[0]
        payload = data[1:]
        _log.debug("SYSEX cmd=0x%02X  len=%d", cmd, len(payload))
        if cmd == Sysex.FIRMWARE_QUERY:
            self._parse_firmware(payload)
        elif cmd == Sysex.CAPABILITY_RESPONSE:
            self._parse_capabilities(payload)
        elif cmd == Sysex.ANALOG_MAPPING_RESPONSE:
            self._parse_analog_mapping(payload)

    def _parse_firmware(self, payload: bytearray) -> None:
        """Decode a FIRMWARE_QUERY response and update firmware_name/version."""
        if len(payload) < 2:
            return
        major = payload[0]
        minor = payload[1]
        name_bytes = payload[2:]
        name = ""
        for i in range(0, len(name_bytes) - 1, 2):
            name += chr(name_bytes[i] | (name_bytes[i + 1] << 7))
        self.firmware_name = name
        self.firmware_version = (major, minor)
        _log.debug("Firmware: %s v%d.%d", name, major, minor)

    def _parse_capabilities(self, payload: bytearray) -> None:
        """Decode a CAPABILITY_RESPONSE and update the capabilities dict."""
        caps: dict = {}
        pin, i = 0, 0
        while i < len(payload):
            modes = []
            while i < len(payload) and payload[i] != 0x7F:
                mode = payload[i]
                resolution = payload[i + 1] if i + 1 < len(payload) else 0
                modes.append({"mode": mode, "resolution": resolution})
                i += 2
            caps[pin] = modes
            pin += 1
            i += 1
        self.capabilities = caps
        _log.debug("Capabilities: %d pins", len(caps))

    def _parse_analog_mapping(self, payload: bytearray) -> None:
        """Decode an ANALOG_MAPPING_RESPONSE and update the analog_mapping dict."""
        mapping = {}
        for digital_pin, analog_ch in enumerate(payload):
            if analog_ch != 0x7F:
                mapping[analog_ch] = digital_pin
        self.analog_mapping = mapping
        _log.debug("Analog mapping: %s", mapping)
