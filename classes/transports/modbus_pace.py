# Description: scraper for PACE BMS devices over RS-232/RS-485 serial using the proprietary binary protocol. Since pymodbus 3.x dropped the binary framer, this transport handles raw serial communication directly
# File: modbus_pace.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# scraper for PACE BMS devices over RS-232/RS-485 serial using the proprietary binary protocol.
# Since pymodbus 3.x dropped the binary framer, this transport handles raw serial communication directly
# via pyserial, mapping register reads to standard Modbus function codes framed in PACE's binary envelope.
# modbus_base is used for the register mapping and processing logic, while this transport focuses on
# the framing, CRC, and serial I/O specific to PACE BMS devices.
"""
PACE BMS serial transport.

PACE BMS devices use a proprietary binary protocol over RS-232/RS-485 serial.
Since pymodbus 3.x dropped the binary framer, raw serial communication is
handled via pyserial directly. Register reads are mapped to standard
Modbus function codes (0x03/0x04) framed in PACE's binary envelope.

Protocol frame structure:
    [ Address ][ Function ][ Data ][ CRC-16 ]
         1b          1b       Nb       2b
CRC-16 is computed over address + function + data using the standard
Modbus CRC-16 table (same polynomial as RTU).
Rewrite of the original pace.py module to incorporate modern Pymodbus versions.
"""
from __future__ import annotations

import struct
import threading
from typing import Any, Optional

import serial

from classes.protocol_settings import Registry_Type
from defs.common import TransportSettings

from .modbus_base import modbus_base

# ---------------------------------------------------------------------------
# CRC-16 lookup tables (Modbus standard polynomial 0xA001)
# Kept as module-level tuples for performance — computed once at import time
# ---------------------------------------------------------------------------
_CRC_HI = (
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0,
    0x80, 0x41, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1,
    0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1,
    0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40,
    0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1,
    0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40,
    0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0,
    0x80, 0x41, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40,
    0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1,
    0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0,
    0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
)

_CRC_LO = (
    0x00, 0xC0, 0xC1, 0x01, 0xC3, 0x03, 0x02, 0xC2, 0xC6, 0x06,
    0x07, 0xC7, 0x05, 0xC5, 0xC4, 0x04, 0xCC, 0x0C, 0x0D, 0xCD,
    0x0F, 0xCF, 0xCE, 0x0E, 0x0A, 0xCA, 0xCB, 0x0B, 0xC9, 0x09,
    0x08, 0xC8, 0xD8, 0x18, 0x19, 0xD9, 0x1B, 0xDB, 0xDA, 0x1A,
    0x1E, 0xDE, 0xDF, 0x1F, 0xDD, 0x1D, 0x1C, 0xDC, 0x14, 0xD4,
    0xD5, 0x15, 0xD7, 0x17, 0x16, 0xD6, 0xD2, 0x12, 0x13, 0xD3,
    0x11, 0xD1, 0xD0, 0x10, 0xF0, 0x30, 0x31, 0xF1, 0x33, 0xF3,
    0xF2, 0x32, 0x36, 0xF6, 0xF7, 0x37, 0xF5, 0x35, 0x34, 0xF4,
    0x3C, 0xFC, 0xFD, 0x3D, 0xFF, 0x3F, 0x3E, 0xFE, 0xFA, 0x3A,
    0x3B, 0xFB, 0x39, 0xF9, 0xF8, 0x38, 0x28, 0xE8, 0xE9, 0x29,
    0xEB, 0x2B, 0x2A, 0xEA, 0xEE, 0x2E, 0x2F, 0xEF, 0x2D, 0xED,
    0xEC, 0x2C, 0xE4, 0x24, 0x25, 0xE5, 0x27, 0xE7, 0xE6, 0x26,
    0x22, 0xE2, 0xE3, 0x23, 0xE1, 0x21, 0x20, 0xE0, 0xA0, 0x60,
    0x61, 0xA1, 0x63, 0xA3, 0xA2, 0x62, 0x66, 0xA6, 0xA7, 0x67,
    0xA5, 0x65, 0x64, 0xA4, 0x6C, 0xAC, 0xAD, 0x6D, 0xAF, 0x6F,
    0x6E, 0xAE, 0xAA, 0x6A, 0x6B, 0xAB, 0x69, 0xA9, 0xA8, 0x68,
    0x78, 0xB8, 0xB9, 0x79, 0xBB, 0x7B, 0x7A, 0xBA, 0xBE, 0x7E,
    0x7F, 0xBF, 0x7D, 0xBD, 0xBC, 0x7C, 0xB4, 0x74, 0x75, 0xB5,
    0x77, 0xB7, 0xB6, 0x76, 0x72, 0xB2, 0xB3, 0x73, 0xB1, 0x71,
    0x70, 0xB0, 0x50, 0x90, 0x91, 0x51, 0x93, 0x53, 0x52, 0x92,
    0x96, 0x56, 0x57, 0x97, 0x55, 0x95, 0x94, 0x54, 0x9C, 0x5C,
    0x5D, 0x9D, 0x5F, 0x9F, 0x9E, 0x5E, 0x5A, 0x9A, 0x9B, 0x5B,
    0x99, 0x59, 0x58, 0x98, 0x88, 0x48, 0x49, 0x89, 0x4B, 0x8B,
    0x8A, 0x4A, 0x4E, 0x8E, 0x8F, 0x4F, 0x8D, 0x4D, 0x4C, 0x8C,
    0x44, 0x84, 0x85, 0x45, 0x87, 0x47, 0x46, 0x86, 0x82, 0x42,
    0x43, 0x83, 0x41, 0x81, 0x80, 0x40,
)


def _compute_crc16(data: bytes) -> int:
    """
    Compute Modbus CRC-16 over data bytes.
    Returns the 16-bit CRC value.
    """
    crc_hi: int = 0xFF
    crc_lo: int = 0xFF
    for byte in data:
        index: int = crc_lo ^ byte
        crc_lo = crc_hi ^ _CRC_HI[index]
        crc_hi = _CRC_LO[index]
    return (crc_hi << 8) | crc_lo


# ---------------------------------------------------------------------------
# Mock response object — mimics pymodbus response interface so
# read_modbus_registers in modbus_base can process results uniformly
# ---------------------------------------------------------------------------
class _PaceResponse:
    """
    Wraps raw register bytes from a PACE BMS response into an object
    that satisfies the interface expected by modbus_base.read_modbus_registers.
    Exposes either .registers (INPUT/HOLDING) or .bits (COIL/DISCRETE)
    depending on how the instance was constructed, mirroring the two
    pymodbus response shapes handled by modbus_base._extract_response_values.
    """

    def __init__(self, registers: list[int]) -> None:
        self.registers: list[int] = registers
        self.bits: list[bool] | None = None   # populated for COIL/DISCRETE responses
        self._error: bool = False

    def isError(self) -> bool:
        return self._error

    @classmethod
    def error(cls) -> "_PaceResponse":
        """Create an error response."""
        resp = cls([])
        resp._error = True
        return resp

    @classmethod
    def from_bits(cls, bits: list[bool]) -> "_PaceResponse":
        """Create a coil/discrete response carrying boolean bit values."""
        resp = cls([])
        resp.bits = bits
        return resp


class pace(modbus_base):

    transport_type: str = "scraper"
    """
    PACE BMS transport bridge for MPG.

    Communicates with PACE BMS devices over RS-232/RS-485 serial using
    the PACE proprietary binary protocol. Since pymodbus 3.x dropped the
    binary framer, raw serial I/O is handled via pyserial directly.

    The protocol wraps standard Modbus function codes (0x03 read holding,
    0x04 read input) in a binary envelope with CRC-16 error checking.
    """

    # Serial communication lock — shared across instances on the same port
    _serial_lock: threading.Lock = threading.Lock()

    def __init__(self, settings: TransportSettings) -> None:
        super().__init__(settings)

        # Instance-level serial attributes — no class-level declarations
        # to avoid the invariance conflict with modbus_base.port: int | str
        self.port = settings.get("port", fallback="/dev/ttyUSB0")
        self.baudrate = settings.getint("baudrate", fallback=9600)
        self.stopbits: int = settings.getint("stopbits", fallback=1)
        self.bytesize: int = settings.getint("bytesize", fallback=8)
        self.parity: str = settings.get("parity", fallback="N")
        self.serial_timeout: float = settings.getfloat("timeout", fallback=2.0)
        self.slave_id: int = settings.getint("slave_id", fallback=1)

        # Raw serial connection — managed directly since pymodbus
        # no longer supports the binary framer needed for PACE BMS
        self._serial: Optional[serial.Serial] = None

    # -----------------------------------------------------------------------
    # Connection lifecycle
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and mark transport as connected."""
        try:
            self._serial = serial.Serial(
                port=str(self.port),
                baudrate=self.baudrate,
                stopbits=self.stopbits,
                bytesize=self.bytesize,
                parity=self.parity,
                timeout=self.serial_timeout
            )
            self.connected = self._serial.is_open
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()

            if self.connected:
                self._log.info(f"PACE BMS connected on {self.port} @ {self.baudrate} baud")
                super().connect()
            else:
                self._log.error(f"PACE BMS failed to open port {self.port}")
        except serial.SerialException as e:
            self._log.error(f"PACE BMS serial open failed: {e}")
            self.connected = False

    def cleanup(self) -> None:
        """Close the serial port and clean up resources."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
                self._log.info(f"PACE BMS serial port {self.port} closed")
            except serial.SerialException as e:
                self._log.warning(f"Error closing PACE BMS serial port: {e}")
        self._serial = None
        self.connected = False
        super().cleanup()

    # -----------------------------------------------------------------------
    # Frame construction and CRC
    # -----------------------------------------------------------------------

    def _build_request(self, function_code: int, start_register: int, count: int) -> bytes:
        """
        Build a PACE binary protocol request frame.

        Frame structure:
            [ slave_id ][ function_code ][ reg_hi ][ reg_lo ][ count_hi ][ count_lo ][ crc_hi ][ crc_lo ]
                  1b            1b            1b        1b         1b          1b           1b        1b

        Args:
            function_code:   0x03 for holding registers, 0x04 for input registers
            start_register:  Starting register address
            count:           Number of registers to read

        Returns:
            Complete framed request as bytes including CRC
        """
        # Build payload without CRC
        payload: bytes = struct.pack(">BBHH", self.slave_id, function_code, start_register, count)
        crc: int = _compute_crc16(payload)
        # Append CRC big-endian — PACE uses big-endian CRC unlike standard
        # Modbus RTU which appends CRC little-endian
        return payload + struct.pack(">H", crc)

    def _verify_response_crc(self, data: bytes) -> bool:
        """
        Verify the CRC-16 of a received response frame.
        The last two bytes of data are the CRC — the check runs over all
        preceding bytes.
        """
        if len(data) < 3:
            return False
        payload: bytes = data[:-2]
        received_crc: int = struct.unpack(">H", data[-2:])[0]
        expected_crc: int = _compute_crc16(payload)
        return received_crc == expected_crc

    def _parse_register_response(self, data: bytes, expected_count: int) -> Optional[list[int]]:
        """
        Parse a PACE register read response into a list of register values.

        Expected response structure:
            [ slave_id ][ func_code ][ byte_count ][ data... ][ crc_hi ][ crc_lo ]

        Args:
            data:           Raw response bytes including CRC
            expected_count: Expected number of registers

        Returns:
            List of int register values, or None if malformed/CRC error
        """
        # Minimum: slave_id + func_code + byte_count + 2 data bytes + 2 CRC
        min_length: int = 5
        if len(data) < min_length:
           self._log.warning(f"PACE response too short: {len(data)} bytes, (expected at least {min_length})")
           return None

        if not self._verify_response_crc(data):
            self._log.error(f"PACE response CRC mismatch — discarding frame. Data: {data.hex()}")
            return None

        # Byte 2 is the byte count of register data
        byte_count: int = data[2]
        expected_bytes: int = expected_count * 2

        if byte_count != expected_bytes:
            self._log.warning(f"PACE response byte count mismatch: got {byte_count}, expected {expected_bytes}")
            return None

        # Extract register values — each register is 2 bytes big-endian
        registers: list[int] = []
        offset: int = 3  # skip slave_id, func_code, byte_count
        for _ in range(expected_count):
            if offset + 2 > len(data) - 2:  # don't read into CRC bytes
                self._log.warning("PACE response truncated during register parse")
                return None
            reg_val: int = struct.unpack_from(">H", data, offset)[0]
            registers.append(reg_val)
            offset += 2

        return registers

    def _parse_bit_response(self, data: bytes, expected_count: int) -> Optional[list[bool]]:
        """
        Parse a PACE coil/discrete read response (FC 0x01 / 0x02) into a list of bools.

        Response structure:
            [ slave_id ][ func_code ][ byte_count ][ data bytes... ][ crc_hi ][ crc_lo ]

        Coil/discrete values are packed 8 per byte, LSB first per the Modbus spec.

        Args:
            data:           Raw response bytes including CRC
            expected_count: Expected number of coil/discrete bits

        Returns:
            List of bool values, or None if malformed/CRC error
        """
        min_length: int = 5
        if len(data) < min_length:
            self._log.warning(
                f"PACE bit response too short: {len(data)} bytes (expected at least {min_length})"
            )
            return None

        if not self._verify_response_crc(data):
            self._log.error(f"PACE bit response CRC mismatch — discarding frame. Data: {data.hex()}")
            return None

        byte_count: int = data[2]
        expected_bytes: int = (expected_count + 7) // 8   # ceil(count / 8)

        if byte_count != expected_bytes:
            self._log.warning(
                f"PACE bit response byte count mismatch: got {byte_count}, expected {expected_bytes}"
            )
            return None

        bits: list[bool] = []
        for byte_idx in range(byte_count):
            byte_val: int = data[3 + byte_idx]
            for bit_idx in range(8):
                if len(bits) >= expected_count:
                    break
                bits.append(bool((byte_val >> bit_idx) & 1))

        return bits

    # -----------------------------------------------------------------------
    # Register reads — overrides modbus_base.read_registers
    # -----------------------------------------------------------------------

    def read_registers(self, start: int, count: int = 1, registry_type: Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        """
        Read registers from the PACE BMS device over raw serial.
        Returns a _PaceResponse object compatible with modbus_base's
        read_modbus_registers processing loop.
        """
        if self._serial is None or not self._serial.is_open:
            self._log.error("PACE BMS read_registers called but serial port is not open")
            return _PaceResponse.error()

        # Map registry type to Modbus function code
        if registry_type == Registry_Type.INPUT:
            function_code: int = 0x04
        elif registry_type == Registry_Type.HOLDING:
            function_code = 0x03
        elif registry_type == Registry_Type.COIL:
            function_code = 0x01
        elif registry_type == Registry_Type.DISCRETE:
            function_code = 0x02
        else:
            self._log.warning(
                f"PACE BMS: unsupported registry_type "
                f"'{registry_type.name}' — returning error response"
            )
            return _PaceResponse.error()

        request: bytes = self._build_request(function_code, start, count)

        with self._serial_lock:
            try:
                # Flush any stale data in the buffer before sending
                self._serial.reset_input_buffer()
                self._serial.write(request)
                self._serial.flush()

                # Expected response size:
                # slave_id(1) + func_code(1) + byte_count(1) + data(count*2) + crc(2)
                # For coils/discrete: data is ceil(count/8) bytes, but we use count*2
                # as an upper bound and let _parse_register_response validate the byte count.
                expected_size: int = 5 + (count * 2)
                response: bytes = self._serial.read(expected_size)

                if len(response) < 5:
                    self._log.warning(
                        f"PACE BMS short response: got {len(response)} bytes, "
                        f"expected at least 5 for {count} registers "
                        f"starting at {start}"
                    )
                    return _PaceResponse.error()

            except serial.SerialException as e:
                self._log.error(f"PACE BMS serial read error: {e}")
                self.connected = False
                return _PaceResponse.error()

        # Route response parsing based on register type
        if registry_type in (Registry_Type.COIL, Registry_Type.DISCRETE):
            bits = self._parse_bit_response(response, count)
            if bits is None:
                return _PaceResponse.error()
            return _PaceResponse.from_bits(bits)
        else:
            registers = self._parse_register_response(response, count)
            if registers is None:
                return _PaceResponse.error()
            return _PaceResponse(registers)

    def write_register(self, register: int, value: int, **kwargs: Any) -> None:
        """
        Write a single holding register to the PACE BMS device.
        Uses Modbus function code 0x06 (Write Single Register).
        """
        if self._serial is None or not self._serial.is_open:
            self._log.error("PACE BMS write_register called but serial port is not open")
            return

        # Build write request: slave_id + 0x06 + register_addr + value
        payload: bytes = struct.pack(">BBHH", self.slave_id, 0x06, register, value)
        crc: int = _compute_crc16(payload)
        request: bytes = payload + struct.pack(">H", crc)

        with self._serial_lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write(request)
                self._serial.flush()

                # Echo response for write single register is same length as request
                response: bytes = self._serial.read(8)
                if len(response) < 8:
                    self._log.warning(
                        f"PACE BMS write_register short echo: {len(response)} bytes for register {register}")
                    return

                if not self._verify_response_crc(response):
                    self._log.error(
                        f"PACE BMS write_register CRC mismatch for register {register}")

            except serial.SerialException as e:
                self._log.error(f"PACE BMS write_register serial error: {e}")
                self.connected = False

    def write_coil(self, register: int, value: bool, **kwargs: Any) -> None:
        """
        Write a single coil to the PACE BMS device.
        Uses Modbus function code 0x05 (Write Single Coil).
        Coil ON = 0xFF00, coil OFF = 0x0000 per the Modbus specification.
        """
        if not self.write_enabled:
            return
        if self._serial is None or not self._serial.is_open:
            self._log.error("PACE BMS write_coil called but serial port is not open")
            return

        coil_value: int = 0xFF00 if value else 0x0000
        payload: bytes = struct.pack(">BBHH", self.slave_id, 0x05, register, coil_value)
        crc: int = _compute_crc16(payload)
        request: bytes = payload + struct.pack(">H", crc)

        with self._serial_lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write(request)
                self._serial.flush()

                # Echo response for write single coil is the same 8 bytes as the request
                response: bytes = self._serial.read(8)
                if len(response) < 8:
                    self._log.warning(
                        f"PACE BMS write_coil short echo: {len(response)} bytes for coil {register}"
                    )
                    return

                if not self._verify_response_crc(response):
                    self._log.error(f"PACE BMS write_coil CRC mismatch for coil {register}")

            except serial.SerialException as e:
                self._log.error(f"PACE BMS write_coil serial error: {e}")
                self.connected = False
