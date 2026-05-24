"""Tests for firmata_client.client — no hardware required."""

import threading
import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from firmata_client import FirmataClient, PinMode
from firmata_client.client import Cmd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_serial() -> MagicMock:
    """Return a pre-configured serial port mock."""
    ser = MagicMock()
    ser.in_waiting = 0
    ser.read.return_value = b""
    return ser


@pytest.fixture
def client(mock_serial: MagicMock) -> Generator[FirmataClient, None, None]:
    """Yield a FirmataClient backed by a mock serial port."""
    with patch("firmata_client.client.serial.Serial", return_value=mock_serial), \
         patch("firmata_client.client.time.sleep"):
        c = FirmataClient("COM_TEST")
    yield c
    c._running = False


# ---------------------------------------------------------------------------
# Category 1: Protocol parsing
# ---------------------------------------------------------------------------


def test_digital_message_sets_single_pin(client: FirmataClient) -> None:
    """DIGITAL_MESSAGE with pin 5 HIGH updates _pin_values[5] to 1."""
    # port 0, pin 5 HIGH: bit 5 set in LSB byte
    buf = bytearray([0x90, 0b00100000, 0x00])
    client._parse(buf)
    assert client._pin_values[5] == 1
    assert client._pin_values[0] == 0


def test_digital_message_sets_all_port_pins(client: FirmataClient) -> None:
    """DIGITAL_MESSAGE 0xFF sets all 8 pins of port 1 (pins 8–15) to HIGH."""
    # value 0xFF = 0x7F | (0x01 << 7)
    buf = bytearray([0x91, 0x7F, 0x01])
    client._parse(buf)
    for pin in range(8, 16):
        assert client._pin_values[pin] == 1


def test_analog_message_stores_channel_value(client: FirmataClient) -> None:
    """ANALOG_MESSAGE for channel 0 stores the value under key 'a0'."""
    buf = bytearray([0xE0, 0x64, 0x00])  # ch 0, value = 100
    client._parse(buf)
    assert client._pin_values["a0"] == 100


def test_analog_message_max_value(client: FirmataClient) -> None:
    """ANALOG_MESSAGE value 1023 is correctly decoded from 7-bit encoding."""
    # 1023 = 0x7F | (0x07 << 7)
    buf = bytearray([0xE2, 0x7F, 0x07])  # ch 2, value = 1023
    client._parse(buf)
    assert client._pin_values["a2"] == 1023


def test_incomplete_3byte_message_stays_in_buffer(client: FirmataClient) -> None:
    """A 2-byte fragment of a 3-byte message is not consumed."""
    fragment = bytearray([0x90, 0b00000001])  # missing third byte
    remainder = client._parse(fragment)
    assert remainder == fragment
    assert client._pin_values.get(0) is None


def test_incomplete_sysex_stays_in_buffer(client: FirmataClient) -> None:
    """A SysEx frame without END_SYSEX (0xF7) is held in the buffer."""
    fragment = bytearray([0xF0, 0x79, 0x02, 0x06])  # no 0xF7 terminator
    remainder = client._parse(fragment)
    assert remainder == fragment


def test_unknown_byte_is_skipped(client: FirmataClient) -> None:
    """An unrecognised leading byte is dropped without error."""
    # 0x01 is unknown; the following DIGITAL_MESSAGE should still be parsed
    buf = bytearray([0x01, 0x90, 0b00000001, 0x00])
    client._parse(buf)
    assert client._pin_values[0] == 1


def test_multiple_messages_parsed_in_sequence(client: FirmataClient) -> None:
    """Two back-to-back messages are both dispatched from one buffer."""
    buf = bytearray([
        0x90, 0b00000001, 0x00,  # DIGITAL port 0, pin 0 HIGH
        0xE0, 0x32, 0x00,        # ANALOG ch 0 = 50
    ])
    client._parse(buf)
    assert client._pin_values[0] == 1
    assert client._pin_values["a0"] == 50


# ---------------------------------------------------------------------------
# Category 2: SysEx payload parsing
# ---------------------------------------------------------------------------


def _encode_name(name: str) -> bytes:
    """7-bit encode a firmware name string (LSB, MSB per character)."""
    result = bytearray()
    for ch in name:
        v = ord(ch)
        result.append(v & 0x7F)
        result.append((v >> 7) & 0x7F)
    return bytes(result)


def test_parse_firmware_decodes_name_and_version(client: FirmataClient) -> None:
    """_parse_firmware sets firmware_name and firmware_version from the payload."""
    payload = bytearray([2, 6]) + bytearray(_encode_name("Hi"))
    client._parse_firmware(payload)
    assert client.firmware_name == "Hi"
    assert client.firmware_version == (2, 6)


def test_parse_firmware_short_payload_is_ignored(client: FirmataClient) -> None:
    """_parse_firmware does nothing when payload is shorter than 2 bytes."""
    client._parse_firmware(bytearray([2]))
    assert client.firmware_name is None
    assert client.firmware_version is None


def test_parse_capabilities_builds_mode_dict(client: FirmataClient) -> None:
    """_parse_capabilities maps each pin index to its list of supported modes."""
    # pin 0: INPUT(res=1) + OUTPUT(res=1); pin 1: OUTPUT(res=1) only
    payload = bytearray([0, 1, 1, 1, 0x7F, 1, 1, 0x7F])
    client._parse_capabilities(payload)
    assert len(client.capabilities) == 2
    assert {"mode": 0, "resolution": 1} in client.capabilities[0]
    assert {"mode": 1, "resolution": 1} in client.capabilities[0]
    assert client.capabilities[1] == [{"mode": 1, "resolution": 1}]


def test_parse_capabilities_pin_with_no_modes(client: FirmataClient) -> None:
    """A pin terminated immediately by 0x7F receives an empty mode list."""
    payload = bytearray([0x7F])
    client._parse_capabilities(payload)
    assert client.capabilities[0] == []


def test_parse_analog_mapping_filters_non_analog_pins(client: FirmataClient) -> None:
    """_parse_analog_mapping maps analog channel → digital pin, skipping 0x7F entries."""
    # digital pin 0, 1 = non-analog (0x7F); digital pin 2 = analog ch 0
    payload = bytearray([0x7F, 0x7F, 0x00])
    client._parse_analog_mapping(payload)
    # key = analog channel, value = digital pin number
    assert client.analog_mapping == {0: 2}


def test_parse_analog_mapping_all_non_analog(client: FirmataClient) -> None:
    """_parse_analog_mapping produces an empty dict when all values are 0x7F."""
    client._parse_analog_mapping(bytearray([0x7F, 0x7F, 0x7F]))
    assert client.analog_mapping == {}


# ---------------------------------------------------------------------------
# Category 3: Outgoing byte encoding
# ---------------------------------------------------------------------------


def test_set_pin_mode_sends_correct_bytes(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """set_pin_mode(13, OUTPUT) writes the expected 3-byte command."""
    mock_serial.write.reset_mock()
    client.set_pin_mode(13, PinMode.OUTPUT)
    mock_serial.write.assert_called_once_with(bytes([0xF4, 13, 0x01]))


def test_digital_write_high(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """digital_write(pin, True) sends SET_DIGITAL_PIN_VALUE with value byte 1."""
    mock_serial.write.reset_mock()
    client.digital_write(13, True)
    mock_serial.write.assert_called_once_with(bytes([0xF5, 13, 1]))


def test_digital_write_low(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """digital_write(pin, False) sends SET_DIGITAL_PIN_VALUE with value byte 0."""
    mock_serial.write.reset_mock()
    client.digital_write(13, False)
    mock_serial.write.assert_called_once_with(bytes([0xF5, 13, 0]))


def test_analog_write_7bit_encoding(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """analog_write encodes value 255 as SysEx 0x6F with LSB=127, MSB=1."""
    mock_serial.write.reset_mock()
    client.analog_write(9, 255)  # 255 → lsb=127 (0x7F), msb=1
    expected = bytes([Cmd.START_SYSEX, 0x6F, 9, 127, 1, Cmd.END_SYSEX])
    mock_serial.write.assert_called_once_with(expected)


def test_report_analog_enable(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """report_analog(ch, True) sends REPORT_ANALOG | ch with enable byte 1."""
    mock_serial.write.reset_mock()
    client.report_analog(2, True)
    mock_serial.write.assert_called_once_with(bytes([0xC2, 1]))


def test_report_digital_disable(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """report_digital(port, False) sends REPORT_DIGITAL | port with enable byte 0."""
    mock_serial.write.reset_mock()
    client.report_digital(1, False)
    mock_serial.write.assert_called_once_with(bytes([0xD1, 0]))


def test_send_sysex_wraps_payload_in_frame(
    client: FirmataClient, mock_serial: MagicMock
) -> None:
    """_send_sysex surrounds the payload with START_SYSEX / END_SYSEX bytes."""
    mock_serial.write.reset_mock()
    client._send_sysex(0x69, b"\x01\x02")
    expected = bytes([0xF0, 0x69, 0x01, 0x02, 0xF7])
    mock_serial.write.assert_called_once_with(expected)


# ---------------------------------------------------------------------------
# Category 4: Timeout and event synchronisation
# ---------------------------------------------------------------------------


def test_query_board_info_raises_on_timeout() -> None:
    """query_board_info raises TimeoutError when no Firmata response arrives."""
    mock_ser = MagicMock()
    mock_ser.in_waiting = 0
    mock_ser.read.return_value = b""
    with patch("firmata_client.client.serial.Serial", return_value=mock_ser), \
         patch("firmata_client.client.time.sleep"):
        c = FirmataClient("COM_TEST", timeout=0.01)
        with pytest.raises(TimeoutError):
            c.query_board_info()
        c._running = False


def _fire_board_info_events(c: FirmataClient) -> None:
    """Wait for query_board_info to register its events, then fire all three."""
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(c._events) < 3:
        time.sleep(0.001)
    c.firmware_name = "TestFirmata"
    c.firmware_version = (2, 6)
    c.capabilities = {0: []}
    c.analog_mapping = {0: 14}
    for ev in c._events.values():
        ev.set()


def test_query_board_info_returns_data_on_success(client: FirmataClient) -> None:
    """query_board_info returns the correct dict when all response events fire."""
    t = threading.Thread(target=_fire_board_info_events, args=(client,), daemon=True)
    t.start()
    result = client.query_board_info()
    t.join(timeout=1.0)
    assert result["firmware"] == "TestFirmata"
    assert result["version"] == (2, 6)
    assert result["analog_mapping"] == {0: 14}


def test_set_event_signals_registered_event(client: FirmataClient) -> None:
    """_set_event sets a threading.Event registered under the given key."""
    ev = threading.Event()
    client._events["probe"] = ev
    client._set_event("probe")
    assert ev.is_set()


def test_set_event_ignores_unknown_key(client: FirmataClient) -> None:
    """_set_event silently ignores a key that has no registered event."""
    client._set_event("nonexistent")  # must not raise
