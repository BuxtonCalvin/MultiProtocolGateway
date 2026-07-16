# Description: This module uses a long obsolete CRC-16 algorithm that is not compatible with the standard
# CRC-16 implementations in pymodbus and elsewhere.
# File: pace.py
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

"""
Re-factored from the original PACE 5048 transport implementation to support a custom CRC-16 algorithm,
but using the modern pymodbus v3 client and framer architecture.  Should work with the PACE 5048 inverter, but is not recommended
for new implementations due to the obsolescence of the CRC-16 algorithm used by this inverter.
Only used for the PACE 5048 inverter, which has a custom Modbus implementation
that requires this specific CRC. The code includes a custom CRC calculation function
and a transport class that implements the necessary Modbus communication using this CRC.
This transport is not recommended for new implementations, but is included for legacy support of the PACE 5048 inverter.
Use the modbus_rtu transport or the modbus_pace transport instead for new projects, as they use the
standard CRC-16 algorithm and are compatible with a wider range of Modbus devices.
"""

from __future__ import annotations

import logging
import struct
import sys
import time
from typing import Any, Callable, Dict, Final, Optional, Tuple, cast

# Top-level Pymodbus v3 client & framer implementations
from pymodbus.client import ModbusSerialClient
from pymodbus.framer import FramerRTU

# Internal application layer imports
from classes.protocol_settings import Registry_Type

_logger: logging.Logger = logging.getLogger(__name__)

# Native system fallbacks for configurations removed in Pymodbus v3
BYTE_ORDER: Final[str] = sys.byteorder  # System byte order ('little' or 'big')
FRAME_HEADER: Final[bytes] = b""         # Empty byte literal for RTU framing concatenation

RTU_FRAME_HEADER: Final[bytes] = BYTE_ORDER.encode("utf-8") + FRAME_HEADER

# Pre-computed Modbus CRC-16 Lookup Tables for exact PACE CRC validation
auchCRCHi: Final[Tuple[int, ...]] = (
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x00, 0xC1, 0x81, 0x40,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40, 0x01, 0xC0, 0x80, 0x41, 0x01, 0xC0, 0x80, 0x41,
    0x00, 0xC1, 0x81, 0x40
)

auchCRCLo: Final[Tuple[int, ...]] = (
    0x00, 0xC0, 0xC1, 0x01, 0xC3, 0x03, 0x02, 0xC2, 0xC6, 0x06, 0x07, 0xC7,
    0x05, 0xC5, 0xC4, 0x04, 0xCC, 0x0C, 0x0D, 0xCD, 0x0F, 0xCF, 0xCE, 0x0E,
    0x0A, 0xCA, 0xCB, 0x0B, 0xC9, 0x09, 0x08, 0xC8, 0xD8, 0x18, 0x19, 0xD9,
    0x1B, 0xDB, 0xDA, 0x1A, 0x1E, 0xDE, 0xDF, 0x1F, 0xDD, 0x1D, 0x1C, 0xDC,
    0x14, 0xD4, 0xD5, 0x15, 0xD7, 0x17, 0x16, 0xD6, 0xD2, 0x12, 0x13, 0xD3,
    0x11, 0xD1, 0xD0, 0x10, 0xF0, 0x30, 0x31, 0xF1, 0x33, 0xF3, 0xF2, 0x32,
    0x36, 0xF6, 0xF7, 0x37, 0xF5, 0x35, 0x34, 0xF4, 0x3C, 0xFC, 0xFD, 0x3D,
    0xFF, 0x3F, 0x3E, 0xFE, 0xFA, 0x3A, 0x3B, 0xFB, 0x39, 0xF9, 0xF8, 0x38,
    0x28, 0xE8, 0xE9, 0x29, 0xEB, 0x2B, 0x2A, 0xEA, 0xEE, 0x2E, 0x2F, 0xEF,
    0x2D, 0xED, 0xEC, 0x2C, 0xE4, 0x24, 0x25, 0xE5, 0x27, 0xE7, 0xE6, 0x26,
    0x22, 0xE2, 0xE3, 0x23, 0xE1, 0x21, 0x20, 0xE0, 0xA0, 0x60, 0x61, 0xA1,
    0x63, 0xA3, 0xA2, 0x62, 0x66, 0xA6, 0xA7, 0x67, 0xA5, 0x65, 0x64, 0xA4,
    0x6C, 0xAC, 0xAD, 0x6D, 0xAF, 0x6F, 0x6E, 0xAE, 0xAA, 0x6A, 0x6B, 0xAB,
    0x69, 0xA9, 0xA8, 0x68, 0x78, 0xB8, 0xB9, 0x79, 0xBB, 0x7B, 0x7A, 0xBA,
    0xBE, 0x7E, 0x7F, 0xBF, 0x7D, 0xBD, 0xBC, 0x7C, 0xB4, 0x74, 0x75, 0xB5,
    0x77, 0xB7, 0xB6, 0x76, 0x72, 0xB2, 0xB3, 0x73, 0xB1, 0x71, 0x70, 0xB0,
    0x50, 0x90, 0x91, 0x51, 0x93, 0x53, 0x52, 0x92, 0x96, 0x56, 0x57, 0x97,
    0x55, 0x95, 0x94, 0x54, 0x9C, 0x5C, 0x5D, 0x9D, 0x5F, 0x9F, 0x9E, 0x5E,
    0x5A, 0x9A, 0x9B, 0x5B, 0x99, 0x59, 0x58, 0x98, 0x88, 0x48, 0x49, 0x89,
    0x4B, 0x8B, 0x8A, 0x4A, 0x4E, 0x8E, 0x8F, 0x4F, 0x8D, 0x4D, 0x4C, 0x8C,
    0x44, 0x84, 0x85, 0x45, 0x87, 0x47, 0x46, 0x86, 0x82, 0x42, 0x43, 0x83,
    0x41, 0x81, 0x80, 0x40
)

def calculate_crc(puchMsg: bytes, usDataLen: int) -> int:
    """Calculates custom standard Modbus CRC-16 using the lookup tables."""
    uchCRCHi: int = 0xFF
    uchCRCLo: int = 0xFF

    for i in range(usDataLen):
        uIndex: int = uchCRCLo ^ puchMsg[i]
        uchCRCLo = uchCRCHi ^ auchCRCHi[uIndex]
        uchCRCHi = auchCRCLo[uIndex]

    return (uchCRCHi << 8 | uchCRCLo)

class CustomFramer(FramerRTU):
    def buildPacket(self, message: Any) -> bytes:
        """Creates a ready to send modbus packet matching PACE 5048 configurations."""
        data: bytes = cast(bytes, message.encode())

        #  Extract unit_id or modern slave_id cleanly for the network packet
        slave_id: int = int(getattr(message, "slave_id", getattr(message, "unit_id", 0x01)))

        # PACE 5048 specific override: forces standard function block formatting layout
        packet: bytes = struct.pack(RTU_FRAME_HEADER,
                                    slave_id,
                                    0x03) + data

        crc: int = calculate_crc(packet, len(packet))
        packet += struct.pack(">H", crc)

        if hasattr(message, "transaction_id"):
            setattr(message, "transaction_id", slave_id)
        return packet

    def checkFrame(self) -> bool:
        """Check if the next frame is available and valid."""
        try:

            pop_header_fn: Callable[[], None] = cast(Callable[[], None], getattr(self, "populateHeader"))
            pop_header_fn()

            header_dict: Dict[str, int] = cast(Dict[str, int], getattr(self, "_header"))
            buffer_bytes: bytes = cast(bytes, getattr(self, "_buffer"))

            frame_size: int = int(header_dict["len"])
            data: bytes = buffer_bytes[:frame_size - 2]
            crc: bytes = buffer_bytes[frame_size - 2:frame_size]

            crc_val: int = (int(crc[0]) << 8) + int(crc[1])

            if calculate_crc(data, len(data)) == crc_val:
                return True
            else:
                _logger.debug("CRC invalid, discarding header!!")
                reset_frame_fn: Callable[[], None] = cast(Callable[[], None], getattr(self, "resetFrame"))
                reset_frame_fn()
                return False
        except (IndexError, KeyError, struct.error, AttributeError):
            return False

class CustomModbusSerialClient(ModbusSerialClient):
    method: str
    socket: Optional[Any]
    _strict: bool
    last_frame_end: Optional[float]
    silent_interval: float
    _t0: float
    inter_char_timeout: float

    def __init__(self, method: str = "rtu", **kwargs: Any) -> None:
        """Initialize a serial client instance using modern super blueprints."""
        self.method = method
        self.socket = None

        # Bind the custom pacing framer class structure
        kwargs["framer"] = CustomFramer

        port: str = str(kwargs.get("port", "/dev/ttyUSB0"))
        baudrate: int = int(kwargs.get("baudrate", 9600))
        bytesize: int = int(kwargs.get("bytesize", 8))
        parity: str = str(kwargs.get("parity", "N"))
        stopbits: int = int(kwargs.get("stopbits", 1))
        timeout: float = float(kwargs.get("timeout", 2.0))

        super().__init__(
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
            **kwargs
        )

        self._strict = bool(kwargs.get("strict", True))
        self.last_frame_end = None
        self.silent_interval = 0.0
        self._t0 = 0.0
        self.inter_char_timeout = 0.0

        if self.method == "rtu":
            if baudrate > 19200:
                self.silent_interval = 1.75 / 1000
            else:
                self._t0 = float((1 + 8 + 2)) / baudrate
                self.inter_char_timeout = 1.5 * self._t0
                self.silent_interval = 3.5 * self._t0
            self.silent_interval = round(self.silent_interval, 6)

class pace:
    transport_type: str = "scraper"
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    client: CustomModbusSerialClient

    def __init__(self, settings: Dict[str, str]) -> None:
        if "port" in settings:
            self.port = settings["port"]

        if "baudrate" in settings:
            self.baudrate = int(settings["baudrate"])

        # Method binary handles the unique custom frame sequence smoothly
        self.client = CustomModbusSerialClient(
            method="binary",
            port=self.port,
            baudrate=int(self.baudrate),
            stopbits=1,
            parity="N",
            bytesize=8,
            timeout=2
        )

    def read_registers(
        self,
        start: int,
        count: int = 1,
        registry_type: Registry_Type = Registry_Type.INPUT,
        **kwargs: Any
    ) -> Optional[Any]:
        # RECONCILED: Map to the standard keyword argument names for modern pymodbus versions
        # Modern pymodbus integration expects 'slave' or 'device_id' based parameters
        if "slave" not in kwargs and "unit" in kwargs:
            kwargs["slave"] = kwargs.pop("unit")

        if registry_type == Registry_Type.INPUT:
            return self.client.read_input_registers(start, count=count, **kwargs)
        elif registry_type == Registry_Type.HOLDING:
            return self.client.read_holding_registers(start, count=count, **kwargs)

        time.sleep(4)
        return None

    def connect(self) -> None:
        self.client.connect()

