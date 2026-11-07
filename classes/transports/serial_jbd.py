# Description: scraper transport for JBD (Xiaoxiang / Overkill Solar) battery management systems over their UART, RS232 or RS485 serial protocol
# File: serial_jbd.py
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

# JBD frames are binary and carry their own length:
#
#   request    DD  A5  <command>  <length>  <data...>  <checksum:2>  77
#   response   DD  <command>  <status>  <length>  <data...>  <checksum:2>  77
#
# The checksum is 0x10000 minus the sum of every byte from the third byte (the command in a
# request, the status in a response) through the data, sent high byte first. Because the data is
# binary, 0x77 can appear inside it (a 2.935 V cell is the two bytes 0B 77), so a frame is read by
# its length, never by scanning for the end byte; serial_length_transport does that.
#
# The commands this transport polls come from the protocol's registry map: 0x03 (pack status),
# 0x04 (cell voltages) and 0x05 (hardware version). JBD has no addressing, so there is one pack
# per port. Writing to the pack (MOSFET control, parameters) is not implemented.
from types import SimpleNamespace

from defs.common import TransportSettings

from .serial_length_transport import serial_length_transport


class serial_jbd(serial_length_transport):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    START : bytes = b"\xdd"
    END : int = 0x77
    LENGTH_OFFSET : int = 3
    LENGTH_SIZE : int = 1
    LENGTH_ADJUST : int = 4
    ''' a frame is DD, command, status and length (4 bytes), then the data, then 2 checksum bytes and the end marker (3 bytes) '''
    MAX_FRAME : int = 128
    ''' the largest read reply is 71 bytes (32 cell voltages); a stray start byte with a bigger length is noise '''

    MAX_ADDRESS : int = 0
    ''' the protocol has no address field '''
    DEFAULT_BAUDRATE : int = 9600

    READ : int = 0xA5
    ''' the second byte of a read request '''
    STATUS_OK : int = 0x00
    ''' a response whose status is anything else (0x80 is "error") carries no data '''

    MIN_FRAME : int = 7
    ''' DD, command, status, length, 2 checksum bytes and the end marker, with no data '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    @staticmethod
    def calculate_checksum(data : bytes) -> int:
        ''' 0x10000 minus the sum of the bytes, as the 16-bit value the frame carries '''
        return (0x10000 - sum(data)) & 0xFFFF

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        body: bytes = bytes([command, len(info)]) + info
        return bytes([self.START[0], self.READ]) + body + self.calculate_checksum(body).to_bytes(2, "big") + bytes([self.END])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.MIN_FRAME or frame[0] != self.START[0] or frame[-1] != self.END:
            self._log.warning(f"JBD frame has the wrong start, end or size: {frame.hex()}")
            return None

        length: int = frame[3]
        if len(frame) != length + self.MIN_FRAME:
            self._log.warning(f"JBD frame length byte says {length} data bytes but the frame is {len(frame)} bytes: {frame.hex()}")
            return None

        calculated: int = self.calculate_checksum(frame[2:4 + length])
        received: int = int.from_bytes(frame[4 + length:6 + length], "big")
        if calculated != received:
            self._log.warning(f"JBD checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        status: int = frame[2]
        if status != self.STATUS_OK:
            self._log.warning(f"JBD command {frame[1]:#04x} was refused with status {status:#04x}")
            return None

        return SimpleNamespace(command=frame[1], status=status, payload=frame[4:4 + length])
