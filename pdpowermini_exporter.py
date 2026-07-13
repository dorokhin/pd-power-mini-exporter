#!/usr/bin/env python3
"""
Prometheus exporter for WeActStudio PDPowerMiniV1-Buck.

stdlib-only Python 3.13 implementation.
No pyserial, no prometheus_client.

Tested/expected on POSIX systems:
  /dev/ttyACM0
  /dev/ttyUSB0

Example:
  python3 pdpowermini_exporter.py /dev/ttyACM0 --baudrate 115200 --web-port 9108

Prometheus scrape config example:
  scrape_configs:
    - job_name: pdpowermini
      static_configs:
        - targets: ['localhost:9108']
"""

from __future__ import annotations

import argparse
import errno
import html
import os
import select
import signal
import sys
import termios
import threading
import time
from enum import IntEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable


class Command(IntEnum):
    CMD_WHO_AM_I = 0x01
    CMD_OUTPUT_EN_STATE = 0x02
    CMD_OUTPUT_ID = 0x03
    CMD_OUTPUT_DATA = 0x04
    CMD_OUTPUT_DISPLAY = 0x05
    CMD_OUTPUT_OCP_EN = 0x06
    CMD_OUTPUT_OFFSET_EN = 0x07
    CMD_BRIGHTNESS = 0x08
    CMD_OUTPUT_DISCHARGE_EN = 0x09

    CMD_SYSTEM_RESET = 0x40
    CMD_SYSTEM_UPGRADE = 0x41
    CMD_SYSTEM_VERSION = 0x42
    CMD_SYSTEM_SERIAL_NUM = 0x43
    CMD_SYSTEM_CONFIG_SAVE = 0x44
    CMD_SYSTEM_FACTORY_RESET = 0x45
    CMD_SYSTEM_LCD_PANEL_TYPE = 0x46
    CMD_SYSTEM_FACTORY_DATA = 0x47

    CMD_END = 0x0A
    CMD_READ = 0x80

    @property
    def response_length(self) -> int:
        """
        Full response frame length for fixed-length responses.

        Non-CRC mode:
          command + payload + CMD_END

        CRC mode:
          command + payload + crc8

        Variable-length string responses return 0 here.
        """
        return {
            Command.CMD_WHO_AM_I: 0,
            Command.CMD_OUTPUT_EN_STATE: 3,
            Command.CMD_OUTPUT_ID: 3,
            Command.CMD_OUTPUT_DATA: 7,
            Command.CMD_OUTPUT_DISPLAY: 6,
            Command.CMD_OUTPUT_OCP_EN: 3,
            Command.CMD_OUTPUT_OFFSET_EN: 3,
            Command.CMD_SYSTEM_FACTORY_DATA: 66,
            Command.CMD_BRIGHTNESS: 3,
            Command.CMD_SYSTEM_VERSION: 0,
            Command.CMD_SYSTEM_SERIAL_NUM: 0,
        }.get(self, 0)


class SerialError(Exception):
    pass


class SerialTimeoutError(SerialError):
    pass


class ProtocolError(Exception):
    pass


class PosixSerial:
    """
    Minimal stdlib-only POSIX serial implementation.

    This replaces pyserial for the limited needs of this exporter.
    """

    _BAUD_RATES = {
        50: termios.B50,
        75: termios.B75,
        110: termios.B110,
        134: termios.B134,
        150: termios.B150,
        200: termios.B200,
        300: termios.B300,
        600: termios.B600,
        1200: termios.B1200,
        1800: termios.B1800,
        2400: termios.B2400,
        4800: termios.B4800,
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
    }

    for _baud_name in (
        "B57600",
        "B115200",
        "B230400",
        "B460800",
        "B500000",
        "B576000",
        "B921600",
        "B1000000",
        "B1152000",
        "B1500000",
        "B2000000",
    ):
        if hasattr(termios, _baud_name):
            _BAUD_RATES[int(_baud_name[1:])] = getattr(termios, _baud_name)

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.fd: int | None = None
        self._original_attrs = None

        self.open()

    def open(self) -> None:
        if self.fd is not None:
            return

        if self.baudrate not in self._BAUD_RATES:
            supported = ", ".join(str(x) for x in sorted(self._BAUD_RATES))
            raise SerialError(
                f"Unsupported baudrate {self.baudrate}. Supported: {supported}"
            )

        try:
            self.fd = os.open(
                self.port,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
            )
        except OSError as exc:
            raise SerialError(f"Unable to open serial port {self.port}: {exc}") from exc

        try:
            attrs = termios.tcgetattr(self.fd)
            self._original_attrs = list(attrs)

            speed = self._BAUD_RATES[self.baudrate]

            # input flags
            attrs[0] = 0

            # output flags
            attrs[1] = 0

            # control flags: 8N1, local, receiver enabled
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~termios.PARENB
            attrs[2] &= ~termios.CSTOPB
            attrs[2] &= ~termios.CSIZE
            attrs[2] |= termios.CS8

            if hasattr(termios, "CRTSCTS"):
                attrs[2] &= ~termios.CRTSCTS

            # local flags
            attrs[3] = 0

            # input/output speed
            attrs[4] = speed
            attrs[5] = speed

            # VMIN/VTIME: non-blocking reads, select handles timeout
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0

            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            self.close()
            raise

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def close(self) -> None:
        if self.fd is None:
            return

        fd = self.fd
        self.fd = None

        try:
            if self._original_attrs is not None:
                termios.tcsetattr(fd, termios.TCSANOW, self._original_attrs)
        except Exception:
            pass

        try:
            os.close(fd)
        except OSError:
            pass

    def reset_input_buffer(self) -> None:
        if self.fd is None:
            raise SerialError("Serial port is closed")
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def write(self, data: bytes | bytearray) -> int:
        if self.fd is None:
            raise SerialError("Serial port is closed")

        total = 0
        view = memoryview(bytes(data))

        while total < len(view):
            _, writable, _ = select.select([], [self.fd], [], self.timeout)
            if not writable:
                raise SerialTimeoutError("Timeout while writing to serial port")

            try:
                written = os.write(self.fd, view[total:])
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                    continue
                raise SerialError(f"Serial write failed: {exc}") from exc

            if written <= 0:
                raise SerialError("Serial write returned zero bytes")

            total += written

        try:
            termios.tcdrain(self.fd)
        except OSError:
            pass

        return total

    def read(self, size: int, timeout: float | None = None) -> bytes:
        if self.fd is None:
            raise SerialError("Serial port is closed")

        if size <= 0:
            return b""

        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        remaining = size

        while remaining > 0:
            wait = deadline - time.monotonic()
            if wait <= 0:
                break

            readable, _, _ = select.select([self.fd], [], [], wait)
            if not readable:
                break

            try:
                chunk = os.read(self.fd, remaining)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                    continue
                raise SerialError(f"Serial read failed: {exc}") from exc

            if not chunk:
                continue

            chunks.append(chunk)
            remaining -= len(chunk)

        data = b"".join(chunks)

        if len(data) != size:
            raise SerialTimeoutError(
                f"Timeout while reading serial port: wanted {size} bytes, got {len(data)}"
            )

        return data


class PDPowerMini:
    MAX_VARIABLE_RESPONSE_LENGTH = 256

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        use_crc8: bool = False,
        retries: int = 1,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.use_crc8 = use_crc8
        self.retries = retries
        self._lock = threading.RLock()
        self._serial = PosixSerial(port=port, baudrate=baudrate, timeout=timeout)

        # Small delay after opening serial port.
        time.sleep(0.1)

    def close(self) -> None:
        self._serial.close()

    @property
    def is_open(self) -> bool:
        return self._serial.is_open

    def _build_request(
        self,
        command: Command,
        payload: bytes | bytearray = b"",
        read: bool = True,
    ) -> bytes:
        first = int(command) | (int(Command.CMD_READ) if read else 0)
        frame = bytearray([first])
        frame.extend(payload)

        if self.use_crc8:
            frame.append(self.calculate_crc8(frame))
        else:
            frame.append(int(Command.CMD_END))

        return bytes(frame)

    def _read_exact(self, size: int) -> bytes:
        return self._serial.read(size, timeout=self.timeout)

    def _read_one(self) -> int:
        return self._read_exact(1)[0]

    def _scan_first_byte(self, expected_command: Command) -> int:
        """
        Skip noise until a byte matching expected command appears.

        Device may set read bit in command byte, so compare lower 7 bits.
        """
        deadline = time.monotonic() + self.timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialTimeoutError(
                    f"Timeout waiting for command 0x{int(expected_command):02x}"
                )

            byte = self._serial.read(1, timeout=remaining)[0]
            if (byte & 0x7F) == int(expected_command):
                return byte

    def _read_response(self, command: Command) -> bytes:
        response_length = command.response_length

        if response_length > 0:
            return self._read_fixed_response(command, response_length)

        if self.use_crc8:
            return self._read_variable_crc_response(command)

        return self._read_variable_end_response(command)

    def _read_fixed_response(self, command: Command, response_length: int) -> bytes:
        first = self._scan_first_byte(command)
        rest = self._read_exact(response_length - 1)
        frame = bytes([first]) + rest

        if self.use_crc8:
            received_crc = frame[-1]
            calculated_crc = self.calculate_crc8(frame[:-1])
            if received_crc != calculated_crc:
                raise ProtocolError(
                    f"CRC8 mismatch for {command.name}: "
                    f"received=0x{received_crc:02x}, calculated=0x{calculated_crc:02x}"
                )
            return frame[1:-1]

        if frame[-1] != int(Command.CMD_END):
            raise ProtocolError(
                f"Invalid frame terminator for {command.name}: "
                f"expected 0x0a, got 0x{frame[-1]:02x}, frame={frame.hex()}"
            )

        return frame[1:-1]

    def _read_variable_end_response(self, command: Command) -> bytes:
        self._scan_first_byte(command)
        payload = bytearray()

        while len(payload) <= self.MAX_VARIABLE_RESPONSE_LENGTH:
            byte = self._read_one()
            if byte == int(Command.CMD_END):
                return bytes(payload)
            payload.append(byte)

        raise ProtocolError(f"Variable response is too long for {command.name}")

    def _read_variable_crc_response(self, command: Command) -> bytes:
        first = self._scan_first_byte(command)
        length = self._read_one()

        if length > self.MAX_VARIABLE_RESPONSE_LENGTH:
            raise ProtocolError(
                f"Variable CRC response is too long for {command.name}: {length}"
            )

        payload = self._read_exact(length)
        received_crc = self._read_one()

        frame_without_crc = bytes([first, length]) + payload
        calculated_crc = self.calculate_crc8(frame_without_crc)

        if received_crc != calculated_crc:
            raise ProtocolError(
                f"CRC8 mismatch for {command.name}: "
                f"received=0x{received_crc:02x}, calculated=0x{calculated_crc:02x}"
            )

        return payload

    def query(
        self,
        command: Command,
        payload: bytes | bytearray = b"",
    ) -> bytes:
        request = self._build_request(command, payload=payload, read=True)
        last_error: Exception | None = None

        with self._lock:
            for _ in range(self.retries + 1):
                try:
                    self._serial.reset_input_buffer()
                    self._serial.write(request)
                    return self._read_response(command)
                except (SerialError, ProtocolError) as exc:
                    last_error = exc
                    time.sleep(0.05)

        assert last_error is not None
        raise last_error

    def who_am_i(self) -> str:
        return self.query(Command.CMD_WHO_AM_I).decode("utf-8", errors="replace")

    def system_version(self) -> str:
        return self.query(Command.CMD_SYSTEM_VERSION).decode("utf-8", errors="replace")

    def system_serial_num(self) -> str:
        return self.query(Command.CMD_SYSTEM_SERIAL_NUM).decode(
            "utf-8",
            errors="replace",
        )

    def output_state(self) -> int:
        payload = self.query(Command.CMD_OUTPUT_EN_STATE)
        if len(payload) < 1:
            raise ProtocolError("Short CMD_OUTPUT_EN_STATE response")
        return payload[0]

    def output_id_get(self) -> int:
        payload = self.query(Command.CMD_OUTPUT_ID)
        if len(payload) < 1:
            raise ProtocolError("Short CMD_OUTPUT_ID response")
        return payload[0]

    def output_data_get(self, profile_id: int) -> tuple[int, int]:
        """
        Returns configured output data for profile.

        Units returned by device:
          voltage: millivolts
          current: milliamperes
        """
        payload = self.query(Command.CMD_OUTPUT_DATA, bytes([profile_id & 0xFF]))

        if len(payload) < 5:
            raise ProtocolError("Short CMD_OUTPUT_DATA response")

        response_profile_id = payload[0]
        if response_profile_id != (profile_id & 0xFF):
            raise ProtocolError(
                f"Unexpected profile id in response: "
                f"wanted={profile_id}, got={response_profile_id}"
            )

        voltage_mv = payload[1] | (payload[2] << 8)
        current_ma = payload[3] | (payload[4] << 8)
        return voltage_mv, current_ma

    def output_display_get(self) -> tuple[int, int]:
        """
        Returns measured/displayed output values.

        Units returned by device:
          voltage: millivolts
          current: milliamperes
        """
        payload = self.query(Command.CMD_OUTPUT_DISPLAY)

        if len(payload) < 4:
            raise ProtocolError("Short CMD_OUTPUT_DISPLAY response")

        voltage_mv = payload[0] | (payload[1] << 8)
        current_ma = payload[2] | (payload[3] << 8)
        return voltage_mv, current_ma

    @staticmethod
    def calculate_crc8(data: bytes | bytearray | Iterable[int]) -> int:
        """
        CRC-8:
          polynomial: 0x31
          init:       0xff
        """
        crc = 0xFF
        polynomial = 0x31

        for byte in data:
            crc ^= byte & 0xFF
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ polynomial
                else:
                    crc <<= 1
                crc &= 0xFF

        return crc


def prometheus_escape_label(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def metric_line(
    name: str,
    value: int | float,
    labels: dict[str, object] | None = None,
) -> str:
    if labels:
        label_text = ",".join(
            f'{key}="{prometheus_escape_label(val)}"' for key, val in labels.items()
        )
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


class PDPowerMiniExporter:
    def __init__(self, device: PDPowerMini, cache_ttl: float = 0.5):
        self.device = device
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._cached_body: bytes | None = None
        self._cached_until = 0.0

        self._device_info: dict[str, str] | None = None
        self._scrape_errors_total = 0

    def collect(self) -> bytes:
        now = time.monotonic()

        with self._lock:
            if self._cached_body is not None and now < self._cached_until:
                return self._cached_body

            body = self._collect_uncached()
            self._cached_body = body
            self._cached_until = time.monotonic() + self.cache_ttl
            return body

    def _read_device_info_once(self) -> dict[str, str]:
        if self._device_info is not None:
            return self._device_info

        info = {
            "port": self.device.port,
            "baudrate": str(self.device.baudrate),
            "crc8": "true" if self.device.use_crc8 else "false",
            "who_am_i": "unknown",
            "version": "unknown",
            "serial_number": "unknown",
        }

        try:
            info["who_am_i"] = self.device.who_am_i()
        except Exception:
            pass

        try:
            info["version"] = self.device.system_version()
        except Exception:
            pass

        try:
            info["serial_number"] = self.device.system_serial_num()
        except Exception:
            pass

        self._device_info = info
        return info

    def _collect_uncached(self) -> bytes:
        started = time.monotonic()
        lines: list[str] = []
        errors: list[str] = []

        lines.extend(
            [
                "# HELP pdpowermini_up 1 if the last device scrape was successful, 0 otherwise.",
                "# TYPE pdpowermini_up gauge",
                "# HELP pdpowermini_device_info Static device information.",
                "# TYPE pdpowermini_device_info gauge",
                "# HELP pdpowermini_output_enabled Output enabled state reported by device.",
                "# TYPE pdpowermini_output_enabled gauge",
                "# HELP pdpowermini_output_voltage_volts Measured output voltage.",
                "# TYPE pdpowermini_output_voltage_volts gauge",
                "# HELP pdpowermini_output_current_amperes Measured output current.",
                "# TYPE pdpowermini_output_current_amperes gauge",
                "# HELP pdpowermini_output_power_watts Calculated output power.",
                "# TYPE pdpowermini_output_power_watts gauge",
                "# HELP pdpowermini_output_profile_id Active output profile id.",
                "# TYPE pdpowermini_output_profile_id gauge",
                "# HELP pdpowermini_output_set_voltage_volts Configured output voltage for active profile.",
                "# TYPE pdpowermini_output_set_voltage_volts gauge",
                "# HELP pdpowermini_output_set_current_amperes Configured output current for active profile.",
                "# TYPE pdpowermini_output_set_current_amperes gauge",
                "# HELP pdpowermini_scrape_duration_seconds Time spent collecting metrics from device.",
                "# TYPE pdpowermini_scrape_duration_seconds gauge",
                "# HELP pdpowermini_scrape_errors_total Total number of device/protocol errors during scrapes.",
                "# TYPE pdpowermini_scrape_errors_total counter",
            ]
        )

        info = self._read_device_info_once()
        lines.append(metric_line("pdpowermini_device_info", 1, info))

        output_enabled: int | None = None
        measured_voltage_v: float | None = None
        measured_current_a: float | None = None
        measured_power_w: float | None = None
        profile_id: int | None = None
        set_voltage_v: float | None = None
        set_current_a: float | None = None

        try:
            output_enabled = 1 if self.device.output_state() else 0
        except Exception as exc:
            errors.append(f"output_state: {exc}")

        try:
            voltage_mv, current_ma = self.device.output_display_get()
            measured_voltage_v = voltage_mv / 1000.0
            measured_current_a = current_ma / 1000.0
            measured_power_w = measured_voltage_v * measured_current_a
        except Exception as exc:
            errors.append(f"output_display_get: {exc}")

        try:
            profile_id = self.device.output_id_get()
        except Exception as exc:
            errors.append(f"output_id_get: {exc}")

        if profile_id is not None:
            try:
                set_voltage_mv, set_current_ma = self.device.output_data_get(profile_id)
                set_voltage_v = set_voltage_mv / 1000.0
                set_current_a = set_current_ma / 1000.0
            except Exception as exc:
                errors.append(f"output_data_get: {exc}")

        up = 0 if errors else 1

        if errors:
            self._scrape_errors_total += len(errors)

        lines.append(metric_line("pdpowermini_up", up))

        if output_enabled is not None:
            lines.append(metric_line("pdpowermini_output_enabled", output_enabled))

        if measured_voltage_v is not None:
            lines.append(
                metric_line(
                    "pdpowermini_output_voltage_volts",
                    f"{measured_voltage_v:.6f}",
                )
            )

        if measured_current_a is not None:
            lines.append(
                metric_line(
                    "pdpowermini_output_current_amperes",
                    f"{measured_current_a:.6f}",
                )
            )

        if measured_power_w is not None:
            lines.append(
                metric_line(
                    "pdpowermini_output_power_watts",
                    f"{measured_power_w:.6f}",
                )
            )

        if profile_id is not None:
            lines.append(metric_line("pdpowermini_output_profile_id", profile_id))

        if set_voltage_v is not None:
            lines.append(
                metric_line(
                    "pdpowermini_output_set_voltage_volts",
                    f"{set_voltage_v:.6f}",
                )
            )

        if set_current_a is not None:
            lines.append(
                metric_line(
                    "pdpowermini_output_set_current_amperes",
                    f"{set_current_a:.6f}",
                )
            )

        duration = time.monotonic() - started
        lines.append(metric_line("pdpowermini_scrape_duration_seconds", f"{duration:.6f}"))
        lines.append(metric_line("pdpowermini_scrape_errors_total", self._scrape_errors_total))

        return ("\n".join(lines) + "\n").encode("utf-8")


def make_handler(exporter: PDPowerMiniExporter):
    class MetricsHandler(BaseHTTPRequestHandler):
        server_version = "PDPowerMiniExporter/1.0"

        def do_GET(self) -> None:
            if self.path == "/" or self.path == "":
                self._send_index()
                return

            if self.path.split("?", 1)[0] == "/metrics":
                self._send_metrics()
                return

            self.send_error(404, "Not found")

        def _send_index(self) -> None:
            content = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>PDPowerMini Exporter</title>
</head>
<body>
  <h1>PDPowerMini Exporter</h1>
  <p><a href="/metrics">/metrics</a></p>
</body>
</html>
""".encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _send_metrics(self) -> None:
            try:
                content = exporter.collect()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as exc:
                message = html.escape(str(exc)).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (
                    self.address_string(),
                    self.log_date_time_string(),
                    fmt % args,
                )
            )

    return MetricsHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for WeActStudio PDPowerMiniV1-Buck"
    )

    parser.add_argument(
        "port",
        help="Serial port, for example /dev/ttyACM0 or /dev/ttyUSB0",
    )
    parser.add_argument(
        "--baudrate",
        "-b",
        type=int,
        default=115200,
        help="Serial baudrate, default: 115200",
    )
    parser.add_argument(
        "--crc8",
        action="store_true",
        help="Use CRC8 protocol mode",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Serial read/write timeout in seconds, default: 1.0",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Serial command retries, default: 1",
    )
    parser.add_argument(
        "--listen",
        default="0.0.0.0",
        help="HTTP listen address, default: 0.0.0.0",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=9108,
        help="HTTP listen port, default: 9108",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=0.5,
        help="Metrics cache TTL in seconds, default: 0.5",
    )

    return parser.parse_args()


class PDPowerMiniHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    args = parse_args()

    device = PDPowerMini(
        port=args.port,
        baudrate=args.baudrate,
        timeout=args.timeout,
        use_crc8=args.crc8,
        retries=args.retries,
    )

    exporter = PDPowerMiniExporter(device=device, cache_ttl=args.cache_ttl)

    server = PDPowerMiniHTTPServer(
        (args.listen, args.web_port),
        make_handler(exporter),
    )

    shutdown_started = threading.Event()

    def shutdown_server() -> None:
        """
        ThreadingHTTPServer.shutdown() must be called from another thread,
        not from the same thread where serve_forever() is running.
        """
        server.shutdown()

    def handle_signal(signum: int, _frame: object) -> None:
        if shutdown_started.is_set():
            return

        shutdown_started.set()
        print(f"Received signal {signum}, shutting down...", file=sys.stderr)

        thread = threading.Thread(target=shutdown_server, daemon=True)
        thread.start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(
        f"Starting PDPowerMini exporter on http://{args.listen}:{args.web_port}/metrics",
        file=sys.stderr,
    )
    print(
        f"Serial: port={args.port}, baudrate={args.baudrate}, crc8={args.crc8}",
        file=sys.stderr,
    )

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        print("Stopping HTTP server...", file=sys.stderr)
        server.server_close()

        print("Closing serial device...", file=sys.stderr)
        device.close()

        print("Stopped.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
