# Description: scraper transport for Jikong (JK) battery management systems over their RS485/TTL serial protocol (JK-B and JK-PB models)
# File: serial_jk.py
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

# The JK "communication protocol between monitoring platform and BMS" (the 4E 57 protocol):
#
#   4E 57 | length:2 | terminal:4 | command:1 | source:1 | type:1 | data... | record:4 | 68 | checksum:4
#
# The length counts every byte after the 4E 57 start marker, itself and the checksum included. The
# checksum is the plain sum of every byte from the start marker through the 68 end marker, held in
# the low two bytes of the final four. The command 0x06 asks for every data identifier at once, and
# the reply is a run of identifier + value items, so one request fills the whole registry map.
#
# Models: JK-B / JK-BD units speak this protocol on their RS485/TTL port at 115200 baud. JK-PB units
# speak it when their UART protocol is set to 000; they can also run Modbus RTU (use modbus_rtu with
# the JK Modbus map) or the Pylon protocol (use serial_pylon). This transport only reads; writing
# parameters is not implemented.
#
# Two values are not stored the way the map presents them, so they are normalized here: the current
# (0x84) has two encodings, selected by the protocol version (0xC0), and temperatures over 100 mean
# negative degrees. Both are rewritten as signed 16-bit numbers before the protocol map decodes them.
import struct
from types import SimpleNamespace

from defs.common import TransportSettings

from .serial_length_transport import serial_length_transport


class serial_jk(serial_length_transport):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    START : bytes = b"\x4e\x57"
    LENGTH_OFFSET : int = 2
    LENGTH_SIZE : int = 2
    LENGTH_ADJUST : int = 0
    ''' the length already counts everything after the start marker, so a frame is the start marker plus the length '''
    MAX_FRAME : int = 1024

    DEFAULT_BAUDRATE : int = 115200
    MAX_ADDRESS : int = 0xFFFFFF
    ''' the address is the terminal number, of which the protocol uses the low 24 bits '''

    END : int = 0x68
    READ_ALL : int = 0x06
    ''' command word: read every data identifier at once '''
    FRAME_SOURCE : int = 0x02
    ''' the value the esphome-jk-bms request sends (GPS); the protocol also defines 0 BMS, 1 Bluetooth, 3 PC '''
    TYPE_RESPONSE : int = 1
    TYPE_ACTIVE_UPLOAD : int = 2

    FIXED_SIZE : int = 18
    ''' bytes of a frame that are not data: length, terminal, command, source, type, record, end marker and checksum '''

    CELL_VOLTAGES : int = 0x79
    CURRENT : int = 0x84
    PROTOCOL_VERSION : int = 0xC0
    TEMPERATURES : tuple[int, ...] = (0x80, 0x81, 0x82)

    ITEM_SIZES : dict[int, int] = {
        0x80: 2, 0x81: 2, 0x82: 2, 0x83: 2, 0x84: 2, 0x85: 1, 0x86: 1, 0x87: 2, 0x89: 4, 0x8A: 2, 0x8B: 2, 0x8C: 2,
        0x8E: 2, 0x8F: 2, 0x90: 2, 0x91: 2, 0x92: 2, 0x93: 2, 0x94: 2, 0x95: 2, 0x96: 2, 0x97: 2, 0x98: 2, 0x99: 2,
        0x9A: 2, 0x9B: 2, 0x9C: 2, 0x9D: 1, 0x9E: 2, 0x9F: 2, 0xA0: 2, 0xA1: 2, 0xA2: 2, 0xA3: 2, 0xA4: 2, 0xA5: 2,
        0xA6: 2, 0xA7: 2, 0xA8: 2, 0xA9: 1, 0xAA: 4, 0xAB: 1, 0xAC: 1, 0xAD: 2, 0xAE: 1, 0xAF: 1, 0xB0: 2, 0xB1: 1,
        0xB2: 10, 0xB3: 1, 0xB4: 8, 0xB5: 4, 0xB6: 4, 0xB7: 15, 0xB8: 1, 0xB9: 4, 0xBA: 24, 0xC0: 1,
    }
    ''' the size of each data identifier's value, from the protocol's data table (0x79 carries its own length byte) '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    # ------------------------------------------------------------------ #
    # frames                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def calculate_checksum(data : bytes) -> int:
        ''' the plain 16-bit sum of the bytes '''
        return sum(data) & 0xFFFF

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        if command == self.READ_ALL and not info:
            info = b"\x00"  # reading everything uses identifier 0x00

        length: int = self.FIXED_SIZE + len(info)
        body: bytes = (self.START + length.to_bytes(2, "big") + self.address.to_bytes(4, "big")
                       + bytes([command, self.FRAME_SOURCE, 0x00]) + info + (0).to_bytes(4, "big") + bytes([self.END]))
        return body + (0).to_bytes(2, "big") + self.calculate_checksum(body).to_bytes(2, "big")

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)
        minimum: int = len(self.START) + self.FIXED_SIZE

        if len(frame) < minimum or frame[:2] != self.START:
            self._log.warning(f"JK frame has the wrong start or is too short: {frame.hex()}")
            return None

        length: int = int.from_bytes(frame[2:4], "big")
        if len(frame) != len(self.START) + length:
            self._log.warning(f"JK frame length says {length} bytes after the start marker but {len(frame) - len(self.START)} arrived")
            return None

        if frame[-5] != self.END:
            self._log.warning(f"JK frame does not end with {self.END:#04x} before its checksum: {frame.hex()}")
            return None

        calculated: int = self.calculate_checksum(frame[:-4])
        received: int = int.from_bytes(frame[-2:], "big")
        if calculated != received:
            self._log.warning(f"JK checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        transport_type: int = frame[10]
        if transport_type not in (self.TYPE_RESPONSE, self.TYPE_ACTIVE_UPLOAD):
            self._log.debug(f"JK frame is type {transport_type}, not a response or an upload; ignored")
            return None

        return SimpleNamespace(command_word=frame[8], source=frame[9], type=transport_type, payload=frame[11:-9])

    # ------------------------------------------------------------------ #
    # polling                                                              #
    # ------------------------------------------------------------------ #

    def _poll_commands(self) -> list[int]:
        ''' one request returns every identifier, so there is a single command whatever the map holds '''
        return [self.READ_ALL]

    def _parse_items(self, payload : bytes) -> dict[int, bytes]:
        '''
        Splits a read-all reply into its identifier -> value items. An identifier that is not in the
        protocol's table has an unknown size, so parsing stops there and keeps what it has.
        '''
        items: dict[int, bytes] = {}
        position: int = 0
        while position < len(payload):
            identifier: int = payload[position]
            if identifier == self.CELL_VOLTAGES:
                if position + 1 >= len(payload):
                    break
                size: int = payload[position + 1]
                start: int = position + 2
            elif identifier in self.ITEM_SIZES:
                size = self.ITEM_SIZES[identifier]
                start = position + 1
            else:
                self._log.debug(f"JK data identifier {identifier:#04x} is not in the protocol table; stopped reading items")
                break

            if start + size > len(payload):
                self._log.debug(f"JK item {identifier:#04x} is cut off; stopped reading items")
                break
            items[identifier] = payload[start:start + size]
            position = start + size
        return items

    @staticmethod
    def _temperature(raw : int) -> int:
        ''' 0-100 are degrees C; above 100 the excess is negative degrees (101 is -1 C) '''
        return raw if raw <= 100 else -(raw - 100)

    def _normalize(self, items : dict[int, bytes]) -> dict[int, bytes]:
        '''
        Rewrites the current and the temperatures as signed 16-bit numbers, the forms the protocol
        map decodes. The current is made positive when charging, to match the other BMS transports.
        '''
        normalized: dict[int, bytes] = dict(items)

        for identifier in self.TEMPERATURES:
            if identifier in normalized:
                normalized[identifier] = struct.pack(">h", self._temperature(int.from_bytes(normalized[identifier], "big")))

        if self.CURRENT in normalized:
            raw: int = int.from_bytes(normalized[self.CURRENT], "big")
            version: int = items.get(self.PROTOCOL_VERSION, b"\x00")[0]
            if version >= 1:
                # 10 mA units, the top bit set for charging
                current: int = (raw & 0x7FFF) if raw & 0x8000 else -(raw & 0x7FFF)
            else:
                # 10 mA units offset by 10000, discharge above it
                current = 10000 - raw
            normalized[self.CURRENT] = struct.pack(">h", max(-32768, min(32767, current)))

        return normalized

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        payload: bytes | None = raw_data.get(self.READ_ALL)
        if payload is None:
            return {}

        items: dict[int, bytes] = self._normalize(self._parse_items(payload))
        return self.protocolSettings.process_registery(items, registry_map=self._entries_within(items, self._registry_map()))
