# Description: Bluetooth Low Energy transport for PACEEX (PeiCheng) smart BMS: the 9A...9D command frames carried over the FFF0 notify/write service
# File: serial_bluetooth_pace.py
#
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

from __future__ import annotations

# PACEEX smart BMS (PeiCheng Technology, advertising as PC-xxxx) over Bluetooth Low Energy. This is not the
# Modbus protocol of modbus_pace nor the Pylon-style 5048 protocol of pace_5048: it is a binary protocol of its own.
#
#   frame   9A  <type:6>  <length>  <data...>  <crc:2, high byte first>  9D
#
# The CRC is the Modbus CRC-16 of every byte before it. A request's type names the command and the reply
# repeats it. The two middle bytes of the type identify the command, so a command is held here as the
# 16-bit number (type[2] << 8) | type[3], which is also the register number in the protocol map (paceex_bms):
#
#   0x0001  versions           0x0A00  system (whole stack) values    0x0A02  cell voltages and temperatures
#   0x0002  serial number      0x0A01  this battery's pack values     0x0A03  balancing mask
#
# The pack, cell and balance requests carry the data 01 01 (battery 1). The map names each value by command
# and byte offset into the reply's data. This transport only reads.
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_pace(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "fff0"
    NOTIFY_UUID : str = "fff1"
    WRITE_UUID : str = "fff2"

    HEAD : int = 0x9A
    TAIL : int = 0x9D
    MIN_FRAME : int = 11
    ''' head, type (6), length, crc (2) and tail, with no data '''

    CMD_VERSIONS : int = 0x0001
    CMD_SERIAL : int = 0x0002
    CMD_SYSTEM : int = 0x0A00
    CMD_PACK : int = 0x0A01
    CMD_CELLS : int = 0x0A02
    CMD_BALANCE : int = 0x0A03

    BATTERY_ONE : bytes = b"\x01\x01"
    REQUEST_DATA : dict[int, bytes] = {0x0A01: BATTERY_ONE, 0x0A02: BATTERY_ONE, 0x0A03: BATTERY_ONE}
    ''' commands that name the battery they ask about '''

    CELL_COUNT : int = 3
    CELL_FIRST : int = 4
    CELL_STRIDE : int = 4
    ''' in the cell reply: the cell count byte, the first voltage, and the distance between voltages (each is followed by a temperature) '''

    ABSENT_IF_ZERO : frozenset[str] = frozenset({
        "cell_temperature_1", "cell_temperature_2", "cell_temperature_3", "cell_temperature_4",
        "mosfet_temperature", "ambient_temperature",
    })
    ''' temperature sensors the pack does not have report a raw 0 '''

    PRUNE_SHORT_PAYLOADS : bool = True

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    @staticmethod
    def calculate_crc(data : bytes) -> int:
        ''' the Modbus CRC-16 '''
        crc: int = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return crc

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        while True:
            position: int = buffer.find(bytes([serial_bluetooth_pace.HEAD]))
            if position < 0:
                buffer.clear()
                return None
            if position > 0:
                del buffer[:position]
            if len(buffer) < 8:
                return None

            total: int = buffer[7] + serial_bluetooth_pace.MIN_FRAME
            if len(buffer) < total:
                return None

            frame: bytes = bytes(buffer[:total])
            if frame[-1] != serial_bluetooth_pace.TAIL or serial_bluetooth_pace.calculate_crc(frame[:-3]) != int.from_bytes(frame[-3:-1], "big"):
                del buffer[:1]  # not a real frame start: look for the next marker
                continue

            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        data: bytes = info or self.REQUEST_DATA.get(command, b"")
        body: bytes = bytes([self.HEAD, 0x00, 0x00, command >> 8, command & 0xFF, 0x00, 0x00, len(data)]) + data
        return body + self.calculate_crc(body).to_bytes(2, "big") + bytes([self.TAIL])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.MIN_FRAME or frame[0] != self.HEAD or frame[-1] != self.TAIL:
            self._log.warning(f"PACE frame has the wrong start, end or size: {frame.hex()}")
            return None

        if len(frame) != frame[7] + self.MIN_FRAME:
            self._log.warning(f"PACE frame length byte says {frame[7]} data bytes but the frame is {len(frame)} bytes: {frame.hex()}")
            return None

        calculated: int = self.calculate_crc(frame[:-3])
        received: int = int.from_bytes(frame[-3:-1], "big")
        if calculated != received:
            self._log.warning(f"PACE checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        return SimpleNamespace(command=(frame[3] << 8) | frame[4], payload=frame[8:-3])

    def _poll_commands(self) -> list[int]:
        return [self.CMD_VERSIONS, self.CMD_SERIAL, self.CMD_SYSTEM, self.CMD_PACK, self.CMD_CELLS, self.CMD_BALANCE]

    def _read_command(self, command : int) -> bytes | None:
        # the reply repeats the request's type, which is how it is told from a frame the pack sent on its own
        wanted: bytes = self.build_frame(command)[1:7]
        frame: bytes | None = self._exchange_until(command, lambda candidate: candidate[1:7] == wanted)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        cells: bytes = raw_data.get(self.CMD_CELLS, b"")
        cell_count: int = cells[self.CELL_COUNT] if len(cells) > self.CELL_COUNT else 0

        absent: set[str] = set()
        entry: registry_map_entry
        for entry in self._registry_map():
            payload: bytes | None = raw_data.get(entry.register)
            if payload is None:
                continue

            if entry.register == self.CMD_CELLS and entry.variable_name.startswith("cell_") and entry.variable_name.endswith("_voltage"):
                if entry.register_byte >= self.CELL_FIRST + cell_count * self.CELL_STRIDE:
                    absent.add(entry.variable_name)
            elif entry.variable_name in self.ABSENT_IF_ZERO and int.from_bytes(payload[entry.register_byte:entry.register_byte + 2], "big") == 0:
                absent.add(entry.variable_name)

        entries: list[registry_map_entry] = self._entries_within(raw_data, self._drop_entries(self._registry_map(), absent))
        return self.protocolSettings.process_registery(raw_data, registry_map=entries)
