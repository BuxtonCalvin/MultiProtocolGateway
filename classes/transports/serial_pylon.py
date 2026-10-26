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
#
# SOI/EOI framing and its client live in serial_frame_transport; port handling, the shared
# serial client, locking, scheduling and polling live in serial_base. This module only
# implements the Pylon wire format (frame layout, checksum, version negotiation) on top of them.
import struct
from enum import Enum
from types import SimpleNamespace
from typing import Any

from classes.protocol_settings import (
    registry_map_entry,
)
from defs.common import (
    TransportSettings,
)

from .serial_frame_transport import serial_frame_transport


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

class serial_pylon(serial_frame_transport):

    transport_type = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    # Frame layout (ASCII hex): VER(2) ADR(2) CID1(2) CID2(2) LENGTH(4) INFO(n) CHKSUM(4)
    # LENGTH holds LENID plus its own checksum nibble; SOI and EOI are added by the frame client.
    SOI : bytes = b"\x7e" # aka b"~"
    EOI : bytes = b"\x0d" # aka b"\r"

    LISTEN_ONLY : bool = False
    ''' Pylon is command/response: the battery only answers when asked '''
    OPEN_ON_INIT : bool = True
    ''' open the port at start-up, so a missing adapter stops the gateway early (the original behavior) '''
    EXCLUSIVE_PORT : bool = True
    ''' nothing else on the machine may open the port while the gateway polls the bus '''

    DEFAULT_VER : bytes = b"\x20"
    ''' protocol version used until the battery reports its own (v2.0) '''
    ver : bytes = b"\x00"
    ''' negotiated version as a single raw byte; zero means not fetched yet '''
    adr : bytes
    ''' the battery address as a single raw byte '''

    MIN_FRAME_LENGTH : int = 16
    ''' ASCII chars: VER(2) ADR(2) CID1(2) CID2(2) LENGTH(4) CHKSUM(4), i.e. a frame with no INFO '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        self.adr = struct.pack("B", self.address)

        self.ignore_checksum_errors: bool = settings.getboolean("ignore_checksum_errors", fallback=False)
        ''' accept frames that fail the checksum (old behavior); off by default '''

    @staticmethod
    def _parse_version(value : Any) -> bytes | None:
        ''' converts the ASCII hex VER field of a response (b"20") to the single raw byte 0x20.
        returns None if it is not a usable version. '''
        if not isinstance(value, (bytes, bytearray)) or len(value) != 2:
            return None
        try:
            parsed: bytes = bytes.fromhex(bytes(value).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed if parsed != b"\x00" else None

    def _handshake(self) -> bool:
        ''' negotiates the protocol version and logs the battery name; True when the battery answered '''
        if self.ver != b"\x00":
            return True  # already negotiated on an earlier connect

        # Get the version. Requests use DEFAULT_VER until this succeeds.
        # Note: If attribute is NOT "info", read_variable returns the 'raw' attribute value.
        version_result: dict[str, int | float | str] | Any | None = self.read_variable("version", attribute="ver")
        if not version_result:
            return False

        detected: bytes | None = self._parse_version(version_result)
        if detected is None:
            self._log.warning(f"Unusable pylon protocol version {version_result!r}; using default {self.DEFAULT_VER!r}")
        else:
            self.ver = detected
            self._log.info(f"pylon protocol version is {self.ver!r}")

        # Get the battery name (this returns a dict)
        name_dict: dict[str, int | float | str] | Any | None = self.read_variable("battery_name")
        self._log.info(f"Battery Name: {name_dict}")
        return True

    def _query(self, command : int) -> SimpleNamespace | None:
        ''' one validated command/response round trip; None if no (valid) response '''
        frame: bytes | None = self._exchange(command)
        return self.decode_frame(frame) if frame else None

    def _info_bytes(self, decoded : SimpleNamespace | None) -> bytes | None:
        ''' INFO field of a decoded frame as raw bytes (it is ASCII hex on the wire) '''
        raw_attr: Any | None = getattr(decoded, "info", None)
        if not raw_attr:
            return None
        try:
            return bytes.fromhex(raw_attr.decode("utf8"))
        except (ValueError, UnicodeDecodeError):
            self._log.warning(f"Serial Pylon INFO is not valid hex: {raw_attr!r}")
            return None

    def _read_command(self, command : int) -> bytes | None:
        return self._info_bytes(self._query(command))

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
                decoded: SimpleNamespace | None = self._query(entry.register)
                if decoded is None:
                    return None

                if attribute == "info":
                    raw_bytes: bytes | None = self._info_bytes(decoded)
                    if raw_bytes is not None:
                        # Process into the final dictionary format
                        return self.protocolSettings.process_registery({entry.register: raw_bytes}, registry_map=registry_map)
                    return None

                # Return the raw attribute if it's a different one (like ver or a status code)
                return getattr(decoded, attribute, None) or None

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
    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
        ''' splits a response (SOI/EOI already stripped) into its fields.
        returns None for a truncated frame or one that fails its checksum, unless
        ignore_checksum_errors is set. '''
        raw_frame = bytes(raw_frame)

        if len(raw_frame) < self.MIN_FRAME_LENGTH:
            self._log.warning(f"Serial Pylon frame too short ({len(raw_frame)} bytes): {raw_frame!r}")
            return None

        frame_data: bytes = raw_frame[0:-4]
        frame_checksum: bytes = raw_frame[-4:].upper()

        # Calculate checksum
        calc_checksum: bytes = struct.pack(">H", self.calculate_checksum(frame_data)).hex().upper().encode()

        if calc_checksum != frame_checksum:
            self._log.warning(f"Serial Pylon checksum error, expected {calc_checksum!r}, got {frame_checksum!r}"
                              + (" (ignored)" if self.ignore_checksum_errors else "; frame discarded"))
            if not self.ignore_checksum_errors:
                return None

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
            info_length = (lenid_invert_plus_one << 12) + lenid

        # Use the version negotiated in connect(); until then fall back to the default.
        ver: bytes = self.ver if self.ver != b"\x00" else self.DEFAULT_VER

        # Build the frame as a string first (ASCII Hex protocol)
        frame_str: str = ver.hex().upper()
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
