# pdpowermini_exporter

Prometheus exporter for the WeActStudio PDPowerMiniV1-Buck. Talks to the device over serial and exposes metrics on `/metrics`.

No external dependencies — stdlib only. Serial is implemented by hand via `termios`/`os.open`, no pyserial. Metric formatting is done manually too, no `prometheus_client`.

Runs on POSIX (Linux), needs Python 3.13+.

## Running

```
python3 pdpowermini_exporter.py /dev/ttyACM0 --baudrate 115200 --web-port 9108
```

Port is usually `/dev/ttyACM0` or `/dev/ttyUSB0`.

## Arguments

| Argument | Default | Description |
|---|---|---|
| `port` | — | Serial port of the device (required) |
| `--baudrate`, `-b` | 115200 | Port speed |
| `--crc8` | off | Use CRC8 protocol instead of the `0x0A` terminator |
| `--timeout` | 1.0 | Serial read/write timeout, sec |
| `--retries` | 1 | Number of command retries on error |
| `--listen` | 0.0.0.0 | HTTP server address |
| `--web-port` | 9108 | HTTP server port |
| `--cache-ttl` | 0.5 | How many seconds to cache collected metrics |

## Device protocol

The device replies in one of two modes:
- **plain mode** — frame ends with byte `0x0A`;
- **CRC8 mode** (`--crc8`) — instead of a terminator, the frame ends with a CRC-8 checksum (polynomial `0x31`, init `0xFF`).

For commands with variable-length responses (`WHO_AM_I`, `SYSTEM_VERSION`, `SYSTEM_SERIAL_NUM`), reading either stops at the terminator or uses a length field, depending on the mode.

## Metrics

- `pdpowermini_up` — whether the last device scrape succeeded (1/0)
- `pdpowermini_device_info` — static device info (port, baudrate, firmware version, serial number, etc.) as labels
- `pdpowermini_output_enabled` — whether the output is on
- `pdpowermini_output_voltage_volts` / `pdpowermini_output_current_amperes` — measured voltage and current
- `pdpowermini_output_power_watts` — calculated power (V×A)
- `pdpowermini_output_profile_id` — active profile
- `pdpowermini_output_set_voltage_volts` / `pdpowermini_output_set_current_amperes` — configured values for the active profile
- `pdpowermini_scrape_duration_seconds` — how long polling the device took
- `pdpowermini_scrape_errors_total` — cumulative scrape error counter

If a command doesn't get a response, its metric is simply left out of the output, and `pdpowermini_up` drops to 0.

## Prometheus

```yaml
scrape_configs:
  - job_name: pdpowermini
    static_configs:
      - targets: ['localhost:9108']
```

## Notes

- Data is cached for `--cache-ttl` seconds so frequent scrapes don't hammer the device.
- Device info (`who_am_i`, version, serial number) is read once at startup and reused — the device isn't asked for it on every scrape.
- Graceful shutdown on SIGINT/SIGTERM: the server stops and the port is closed properly.
