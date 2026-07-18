# Description: scraper transport for serial communication with Pylontech batteries, using their specific ASCII Hex protocol with SOI/EOI framing and checksum validation.
# File: serial_pylon.py
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

from __future__ import annotations

# scraper transport for serial communication with Pylontech batteries, using their
# specific ASCII Hex protocol with SOI/EOI framing and checksum validation.
import struct
from enum import Enum
from types import SimpleNamespace
from typing import Any

import serial

from classes.protocol_settings import (
    registry_map_entry,
)
from classes.transports.serial_frame_client import serial_frame_client
from defs.common import (
    TransportSettings,
    find_usb_serial_port,
    get_usb_serial_port_info,
)

from .transport_base import transport_base


class return_codes(Enum):
    NORMAL                  = 0x00
    VERSION_ERROR           = 0x01
    CHECKSUM_ERROR          = 0x02
    LCHECKSUM_ERROR         = 0x03
    INVALID_CID2            = 0x04
    COMMAND_FORMAT_ERROR    = 0X05
    INVALID_DATA            = 0X06
    ADDRESS_ERROR           = 0X90
    COMMUNICATION_ERROR     = 0X91
    UNKNOWN_ERROR           = -1

    @classmethod
    def fromByte(cls, value : bytes):
        try:
            return cls(int(value, 16))  # Attempt to access the Enum member
        except ValueError:
            return return_codes.UNKNOWN_ERROR

class serial_pylon(transport_base):

    transport_type = "scraper"
    ''' for a lack of a better name'''

    addresses : list[int] = []

    client : serial_frame_client

    #this format is pretty common; i need a name for it.
    SOI : bytes = b"\x7e" # aka b"~"
    ver : bytes = b"\x00"
    ''' version has to be fetched first '''
    adr : bytes
    CID1 : bytes
    CID2 : bytes
    LENGTH : bytes
    ''' 2 bytes - include LENID & LCHKSUM'''
    INFO : bytes
    CHKSUM : bytes
    EOI : bytes = b"\x0d" # aka b"\r"

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        '''address is required to be specified '''
        self.port = settings.get("port", fallback="/dev/ttyUSB0")
        self.baudrate = settings.getint("baudrate", fallback=9600)
        if not self.port:
            raise ValueError("Port is not set")

        resolved_port: str | None = find_usb_serial_port(self.port)
        if not resolved_port:
            raise ValueError("Port is not valid / not found")
        self.port: str = resolved_port

        self._log.info("Serial Port : " + self.port + " = "+get_usb_serial_port_info(self.port)) #print for config convenience

        self.baudrate: int = settings.getint("baudrate", 9600)

        address : int = settings.getint("address", 0)
        self.addresses = [address]

        self.adr = struct.pack("B", address)
        #todo, multi address support later

        self.client = serial_frame_client(self.port,
                                          self.baudrate,
                                          self.SOI,
                                          self.EOI,
                                          bytesize=8,
                                          parity=serial.PARITY_NONE,
                                          stopbits=1,
                                          exclusive=True)


        pass

    def connect(self) -> None:
        self.client.connect()

        if self.ver == b"\x00":
            # Get the version.
            # Note: If attribute is NOT "info", read_variable returns the 'raw' attribute value.
            version_result: dict[str, int | float | str] | Any | None = self.read_variable("version", attribute="ver")

            if version_result:
                # Ensure version_result is treated as bytes
                # If read_variable returns a dict, then: version_result.get("version")
                # But since attribute="ver", it likely returns the raw bytes directly.
                if isinstance(version_result, bytes):
                    self.ver = version_result
                else:
                    # Fallback: try to convert to bytes if it's a string/int
                    self.ver = str(version_result).encode("utf-8")

                self.connected = True
                self._log.info(f"pylon protocol version is {self.ver!r}")

                # Get the battery name (this returns a dict)
                name_dict: dict[str, int | float | str] | Any | None = self.read_variable("battery_name")
                self._log.info(f"Battery Name: {name_dict}")

    def read_data(self) -> dict[str, int | float | str]:
        # Initialize 'info' outside the IF to satisfy the return type
        info: dict[str, int | float | str] = {}

        if self.protocolSettings is not None:
            registry_map: list[registry_map_entry] = self.protocolSettings.get_registry_map()

            # We'll use a temporary dict to collect all raw register data first
            all_raw_data: dict[int, bytes] = {}

            for entry in registry_map:
                # Note: Using 'all_raw_data' to check if we already polled this register
                if entry.register not in all_raw_data:
                    command: int = entry.register
                    self.send_command(command)
                    frame: list[bytes] | bytes | None = self.client.read()

                    if frame:
                        # 1. Standardize 'frame' into a single 'bytes' object
                        if isinstance(frame, list):
                            frame = b"".join(frame)

                        # 'frame' is guaranteed to be type 'bytes'
                        raw_attr: Any | None = getattr(self.decode_frame(frame), "info", None)

                        if raw_attr:
                            # Decode hex string bytes to literal bytes
                            raw_bytes: bytes = bytes.fromhex(raw_attr.decode("utf8"))
                            all_raw_data[entry.register] = raw_bytes


            # Process everything once, or update 'info' cumulatively
            # If process_registery can take the whole map at once, do it here:
            if all_raw_data:
                processed: dict[str, int | float | str] = self.protocolSettings.process_registery(all_raw_data, registry_map=registry_map)
                info.update(processed)

        # logs if NO data was gathered across any registers
        if not info:
            self._log.info("Data is Empty; Serial Pylon Transport busy?")

        # Always returns a dict (even if empty), satisfying the type checker
        return info

    def read_variable(self, variable_name: str, entry: "registry_map_entry | None" = None, attribute: str = "info") -> dict[str, int | float | str] | Any | None:
        ## clean for convenience
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        if self.protocolSettings is not None:
            registry_map: list[registry_map_entry] = self.protocolSettings.get_registry_map()

            if entry is None:
                for e in registry_map:
                    if e.variable_name == variable_name:
                        entry = e
                        break

            if entry:
                command: int = entry.register
                self.send_command(command)
                frame: list[bytes] | bytes | None = self.client.read()

                if frame:
                    # Standardize 'frame' into 'bytes' to fix the type error
                    if isinstance(frame, list):
                        frame = b"".join(frame)

                    # Extract the attribute (e.g., "info") safely
                    raw: Any | None = getattr(self.decode_frame(frame), attribute, None)

                    if raw and attribute == "info":
                        # Decode from hex string bytes to literal bytes
                        raw_bytes = bytes.fromhex(raw.decode("utf8"))
                        # Process into the final dictionary format
                        return self.protocolSettings.process_registery({entry.register: raw_bytes}, registry_map=registry_map)
                    # Return 'raw' if it's a different attribute (like a status code)
                    return raw

        return None

    def calculate_checksum(self, data: bytes) -> int:

        """
        calculates the sum of the ASCII character values rather than raw binary data
            Sum the ASCII values of all characters in the frame (excluding SOI, EOI, and the CHKSUM itself).
            Take the sum modulo 65536 (16-bit sum).
            Perform a bitwise NOT (invert all bits) of the result.
            Add 1 to the inverted result. """

        # Calculate the sum of all characters in ASCII value
        ascii_sum: int = sum(data)

        # Take modulus 65536
        remainder: int = ascii_sum % 65536

        # Bitwise invert the remainder and add 1

        checksum: int = (~remainder + 1) & 0xFFFF

        return checksum
    # returning object not bytes
    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace:
        raw_frame = bytes(raw_frame)

        frame_data: bytes = raw_frame[0:-4]
        frame_checksum: bytes = raw_frame[-4:]

        # Calculate checksum
        calc_checksum: bytes = struct.pack(">H", self.calculate_checksum(frame_data)).hex().upper().encode()

        if calc_checksum != frame_checksum:
            self._log.warning(f"Serial Pylon checksum error, got {calc_checksum!r}, expected {frame_checksum!r}")

        # 2. Use SimpleNamespace instead of Object()
        data = SimpleNamespace()
        data.ver = frame_data[0:2]
        data.adr = frame_data[2:4]
        data.cid1 = frame_data[4:6]
        data.cid2 = frame_data[6:8]
        data.infolength = frame_data[8:12]
        data.info = frame_data[12:]

        # Process return code
        # Ensure return_codes.fromByte is handled correctly
        returnCode = return_codes.fromByte(data.cid2)
        if returnCode != return_codes.NORMAL:
            self._log.warning(f"Serial Pylon Error code {returnCode}")

        # 3. Return the object containing all the parsed fields
        return data


    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        ''' builds frame without soi and eoi; that is left for frame client'''

        info_length = 0
        lenid = len(info)

        if lenid != 0:
            # Pylontech specific LENGT calculation logic
            lenid_sum: int = (lenid & 0xF) + ((lenid >> 4) & 0xF) + ((lenid >> 8) & 0xF)
            lenid_modulo: int = lenid_sum % 16
            lenid_invert_plus_one: int = 0b1111 - lenid_modulo + 1
            info_length: int = (lenid_invert_plus_one << 12) + lenid

        # Ensure ver and adr are bytes before calling .hex()
        self.ver = b"\x20"

        # Build the frame as a string first (ASCII Hex protocol)
        frame_str: str = self.ver.hex().upper()
        frame_str += self.adr.hex().upper()
        frame_str += struct.pack(">H", command).hex().upper()
        frame_str += f"{info_length:04X}" # Cleaner way to get 4-char hex
        frame_str += info.hex().upper()

        # Convert to bytes for checksum calculation
        frame_bytes: bytes = frame_str.encode("ascii")

        # Calculate and append checksum
        frame_chksum: int = self.calculate_checksum(frame_bytes)
        # Checksum is also sent as ASCII Hex (4 bytes)
        checksum_hex: bytes = struct.pack(">H", frame_chksum).hex().upper().encode("ascii")

        final_frame: bytes = frame_bytes + checksum_hex

        return final_frame


    def send_command(self, cmd: int, info: bytes = b"") -> None:
        data: bytes = self.build_frame(cmd, info)
        self.client.write(data)

