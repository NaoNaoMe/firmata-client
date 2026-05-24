# firmata-client

A minimal [Firmata](https://github.com/firmata/protocol) protocol client for Arduino boards running **StandardFirmata**.

The only dependency is [pyserial](https://pypi.org/project/pyserial/).

## Requirements

- Python 3.13+
- Arduino flashed with **StandardFirmata** (included in the Arduino IDE: *File → Examples → Firmata → StandardFirmata*)

## Installation

```bash
pip install firmata-client
```

## Quick start

```python
import logging
import sys
import time

from firmata_client import FirmataClient, PinMode

# Enable all firmata messages including serial traffic:
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# Suppress serial traffic
logging.getLogger("firmata.serial").setLevel(logging.WARNING)

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"

print(f"Connecting to {PORT} ...")
client = FirmataClient(PORT)

print("Querying board info ...")
info = client.query_board_info()
print(f"  Firmware : {info['firmware']}  v{info['version']}")
print(f"  Pin count: {len(info['capabilities'])}")
print(f"  Analog ch: {info['analog_mapping']}")

print("Read analog A0 ...")
A0_CH = 0
client.set_pin_mode(info["analog_mapping"][A0_CH], PinMode.ANALOG)
client.report_analog(A0_CH, True)  # enable reporting
time.sleep(0.2)
print(f"  A0 value : {client.analog_read(A0_CH)}")

print("Blink D13 several times ...")
D13 = 13
client.set_pin_mode(D13, PinMode.OUTPUT)
for _ in range(10):
    client.digital_write(D13, True)
    time.sleep(0.5)
    client.digital_write(D13, False)
    time.sleep(0.5)

client.close()
print("Done.")
```

## API reference

### `FirmataClient(port, baudrate=57600, timeout=5.0)`


| Method | Description |
|---|---|
| `close()` | Stop the reader thread and close the port. |
| `query_board_info() -> dict` | Fetch firmware name, protocol version, pin capabilities, and analog mapping. |
| `set_pin_mode(pin, mode)` | Set a pin mode (`PinMode.INPUT/OUTPUT/ANALOG/PWM/SERVO`). |
| `digital_write(pin, value)` | Write `True`/`False` to a digital output pin. |
| `digital_read(pin) -> int \| None` | Read the latest cached digital value (requires `report_digital`). |
| `analog_write(pin, value)` | Write a PWM (0–255) or servo (0–180) value. |
| `analog_read(channel) -> int \| None` | Read the latest cached analog value (requires `report_analog`). |
| `report_analog(channel, enable)` | Enable/disable streaming analog readings for a channel. |
| `report_digital(port, enable)` | Enable/disable streaming digital readings for a port. |


## License

MIT
