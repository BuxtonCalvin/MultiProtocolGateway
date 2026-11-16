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
from typing import Any, Callable

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

    # Commands are the register numbers of the protocol map: CID1 << 8 | CID2.
    CMD_ANALOG : int = 0x4642
    CMD_ALARM : int = 0x4644
    CMD_SYSTEM_PARAMETERS : int = 0x4647
    CMD_MANUFACTURER : int = 0x4651
    CMD_MANAGEMENT : int = 0x4692
    CMD_SERIAL_NUMBER : int = 0x4693
    CMD_SOFTWARE_VERSION : int = 0x4696

    PRUNE_SHORT_PAYLOADS : bool = True
    ''' a battery with fewer cells or temperature sensors than the map has rows for simply has no such variables '''

    COMMANDS_WITH_COMMAND_INFO : frozenset[int] = frozenset({0x4642, 0x4644, 0x4692, 0x4693, 0x4696})
    ''' commands whose request INFO is one byte naming the battery (V3.3 3.3, 3.5, 3.6, 3.7, 3.10): the
    battery's ADR for a single battery, or 0xFF for the system totals (analog and alarm only) '''

    # Analog and alarm responses are variable length: the number of cells and temperature points changes
    # every offset after them. They are split into fixed-offset blocks held under these pseudo commands,
    # which are never sent: they are served from the response to the real command (see _split_analog).
    BLOCK_ANALOG_CELLS : int = 0x46F1
    BLOCK_ANALOG_TEMPERATURES : int = 0x46F2
    BLOCK_ALARM_CELLS : int = 0x46F3
    BLOCK_ALARM_TEMPERATURES : int = 0x46F4
    BLOCK_PARENT : dict[int, int] = {0x46F1: 0x4642, 0x46F2: 0x4642, 0x46F3: 0x4644, 0x46F4: 0x4644}
    ''' pseudo command -> the real command whose response it is cut from '''

    command_info : int
    ''' the INFO byte sent with commands in COMMANDS_WITH_COMMAND_INFO; the address unless set otherwise '''
    _blocks : dict[int, bytes]
    ''' fixed-offset blocks cut from the last analog / alarm response, waiting to be read '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        self.adr = struct.pack("B", self.address)
        self._blocks = {}

        raw_info: Any = settings.get(["command_info"], fallback=None)
        self.command_info = self.address if raw_info in (None, "") else int(str(raw_info), 0) if str(raw_info).lower().startswith("0x") else int(str(raw_info), 16)
        ''' accepts hex without a prefix ("ff", "05") or with one ("0xFF") '''

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

    def _request_info(self, command : int) -> bytes:
        ''' the INFO sent with a command: the battery number for the commands that take one, else nothing '''
        if command in self.COMMANDS_WITH_COMMAND_INFO:
            return struct.pack("B", self.command_info)
        return b""

    def _query(self, command : int) -> SimpleNamespace | None:
        ''' one validated command/response round trip; None if no (valid) response '''
        frame: bytes | None = self._exchange(command, self._request_info(command))
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

    @staticmethod
    def _split_analog(info : bytes) -> dict[int, bytes] | None:
        '''
        Cuts an analog response (V3.3 3.3) into fixed-offset blocks, or None if it is malformed.

        Wire layout: flag, command value, M, M cell voltages, N, N temperatures, current, module voltage,
        remaining capacity 1, user defined count, total capacity 1, cycles and, when the count is above 2
        (batteries over 65 Ah), remaining capacity 2 and total capacity 2 as three bytes each.

        Summary block (command 0x4642), every offset fixed whatever the battery:
          0 flag, 1 command value, 2 M, 3 N, 4-5 current, 6-7 module voltage, 8-11 remaining capacity (mAh),
          12-15 total capacity (mAh), 16-17 cycles, 18-19 state of charge in 0.1% (derived, not sent)
        Cell voltages (0x46F1) and temperatures (0x46F2) are 2 bytes each in their own blocks.
        '''
        if len(info) < 3:
            return None
        cells: int = info[2]
        position: int = 3 + 2 * cells
        if len(info) < position + 1:
            return None
        temperatures: int = info[position]
        scalars: int = position + 1 + 2 * temperatures
        if len(info) < scalars + 11:
            return None

        cell_block: bytes = info[3:3 + 2 * cells]
        temperature_block: bytes = info[position + 1:scalars]
        current: bytes = info[scalars:scalars + 2]
        voltage: bytes = info[scalars + 2:scalars + 4]
        remaining: int = int.from_bytes(info[scalars + 4:scalars + 6], "big")
        user_defined: int = info[scalars + 6]
        total: int = int.from_bytes(info[scalars + 7:scalars + 9], "big")
        cycles: bytes = info[scalars + 9:scalars + 11]

        if user_defined > 2 and len(info) >= scalars + 17:
            # batteries over 65 Ah report 0xFFFF in the 2-byte fields and the real values in these
            remaining = int.from_bytes(info[scalars + 11:scalars + 14], "big")
            total = int.from_bytes(info[scalars + 14:scalars + 17], "big")

        state_of_charge: int = min(1000, max(0, round(remaining * 1000 / total))) if 0 < total < 0xFFFFFF else 0
        summary: bytes = (
            bytes([info[0], info[1], cells, temperatures]) + current + voltage
            + remaining.to_bytes(4, "big") + total.to_bytes(4, "big") + cycles + state_of_charge.to_bytes(2, "big")
        )
        return {
            serial_pylon.CMD_ANALOG: summary,
            serial_pylon.BLOCK_ANALOG_CELLS: cell_block,
            serial_pylon.BLOCK_ANALOG_TEMPERATURES: temperature_block,
        }

    @staticmethod
    def _split_alarm(info : bytes) -> dict[int, bytes] | None:
        '''
        Cuts an alarm response (V3.3 3.5) into fixed-offset blocks, or None if it is malformed.

        Wire layout: flag, command value, M, M cell states, N, N temperature states, charge current state,
        module voltage state, discharge current state, status 1 to 5 (one byte each).

        Summary block (command 0x4644): 0 flag, 1 command value, 2 M, 3 N, 4 charge current state,
        5 module voltage state, 6 discharge current state, 7-11 status 1 to 5.
        Cell states (0x46F3) and temperature states (0x46F4) are one byte each in their own blocks.
        '''
        if len(info) < 3:
            return None
        cells: int = info[2]
        position: int = 3 + cells
        if len(info) < position + 1:
            return None
        temperatures: int = info[position]
        scalars: int = position + 1 + temperatures
        if len(info) < scalars + 8:
            return None

        summary: bytes = bytes([info[0], info[1], cells, temperatures]) + info[scalars:scalars + 8]
        return {
            serial_pylon.CMD_ALARM: summary,
            serial_pylon.BLOCK_ALARM_CELLS: info[3:3 + cells],
            serial_pylon.BLOCK_ALARM_TEMPERATURES: info[position + 1:scalars],
        }

    def _fetch_command(self, command : int) -> bytes | None:
        ''' sends a real command and returns its payload; analog and alarm responses are split into blocks '''
        payload: bytes | None = self._info_bytes(self._query(command))
        if payload is None or command not in (self.CMD_ANALOG, self.CMD_ALARM):
            return payload

        splitter: Callable[..., dict[int, bytes] | None] = self._split_analog if command == self.CMD_ANALOG else self._split_alarm
        blocks: dict[int, bytes] | None = splitter(payload)
        if blocks is None:
            self._log.warning(f"Serial Pylon response to 0x{command:04X} is malformed ({len(payload)} bytes); discarded")
            return None

        for block_command in [c for c, parent in self.BLOCK_PARENT.items() if parent == command]:
            self._blocks[block_command] = blocks[block_command]
        return blocks[command]

    def _read_command(self, command : int) -> bytes | None:
        parent: int | None = self.BLOCK_PARENT.get(command)
        if parent is None:
            return self._fetch_command(command)

        # a pseudo command: read it from the response to its real command, asking for that once
        # if the map polls it first (the real command is normally polled before its blocks)
        if command not in self._blocks:
            self._fetch_command(parent)
        return self._blocks.pop(command, None)

    def read_variable(self, variable_name: str, entry: "registry_map_entry | None" = None, attribute: str = "info") -> dict[str, int | float | str] | Any | None:
        ## clean for convenience
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        if self.protocolSettings is not None:
            registry_map: list[registry_map_entry] = self._registry_map()

            if entry is None:
                for e in registry_map:
                    if e.variable_name == variable_name:
                        entry = e
                        break

            if entry:
                decoded: SimpleNamespace | None = self._query(entry.register)  # sends the INFO the command needs
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
        lenid: int = len(info) * 2  # LENID is the number of ASCII characters of INFO: two per byte

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
