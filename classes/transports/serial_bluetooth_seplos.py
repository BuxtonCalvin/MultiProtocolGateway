# Description: Bluetooth Low Energy transport for Seplos V2 BMS (BP02 to BP29): the 7E ... 0D telemetry frame over the FF00 service, normalized to a fixed layout for the protocol map
# File: serial_bluetooth_seplos.py
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

# Seplos V2 BMS boards with Bluetooth (advertising as BP02... to BP29...) speak the Seplos serial protocol over a
# notify/write pair on service FF00:
#
#   frame    7E  <version>  <address>  <function>  <return code>  <length:2, high byte first>  <data...>  <crc:2, high byte first>  0D
#
# Requests use version 0x10 and replies 0x14. The CRC is CRC-16/XMODEM of every byte between the 7E and the CRC. The
# function of a request is 0x46 and the command is the next byte; this transport sends command 0x61 (telemetry of one
# pack, data 00). The board ignores the address.
#
# The telemetry reply has no fixed layout: the cell count is at byte 9, the cell voltages follow (2 bytes each, high
# byte first), then the sensor count, the sensor temperatures, and then the pack values, so every offset after the
# cells moves with the counts. The map (seplos_bms_v2) therefore reads three fixed blocks this transport builds:
#
#   command 0x61    the reply itself: the cell count (byte 9) and the cell voltages (10 on)
#   command 0x161   the sensor count, then the temperatures (2 bytes each, tenths of a kelvin)
#   command 0x162   the pack values from the byte after the temperatures on
#
# Cells and sensors beyond the counts the board reports are dropped, and at most 16 cells and 8 sensors are read.
# The last two sensors are the ambient and the MOSFET temperature. MOSFET states, alarms and the protection settings
# are not decoded, nor is the pack count or the model (command 0x51). This transport only reads.
import re
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_seplos(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ff00"
    NOTIFY_UUID : str = "ff01"
    WRITE_UUID : str = "ff02"

    HEAD : int = 0x7E
    TAIL : int = 0x0D
    VERSION_REQUEST : int = 0x10
    VERSION_REPLY : int = 0x14
    FUNCTION : int = 0x46
    MIN_FRAME : int = 10
    ''' head, version, address, function, return code, length (2), crc (2) and tail, with no data '''
    MAX_FRAME : int = 512

    COMMAND_TELEMETRY : int = 0x61
    BLOCK_TEMPERATURES : int = 0x161
    BLOCK_VALUES : int = 0x162
    CELL_COUNT : int = 9
    CELL_FIRST : int = 10
    MAX_CELLS : int = 16
    MAX_SENSORS : int = 8
    VALUES_MIN : int = 17
    ''' bytes of pack values the map reads, from the first one '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    @staticmethod
    def calculate_crc(data : bytes) -> int:
        ''' CRC-16/XMODEM '''
        crc: int = 0
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        return crc

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        while True:
            position: int = buffer.find(bytes([serial_bluetooth_seplos.HEAD]))
            if position < 0:
                buffer.clear()
                return None
            if position > 0:
                del buffer[:position]
            if len(buffer) < 7:
                return None

            total: int = int.from_bytes(buffer[5:7], "big") + serial_bluetooth_seplos.MIN_FRAME
            if total > serial_bluetooth_seplos.MAX_FRAME or buffer[1] != serial_bluetooth_seplos.VERSION_REPLY:
                del buffer[:1]  # not a real frame start: look for the next one
                continue
            if len(buffer) < total:
                return None

            frame: bytes = bytes(buffer[:total])
            if frame[-1] != serial_bluetooth_seplos.TAIL or serial_bluetooth_seplos.calculate_crc(frame[1:-3]) != int.from_bytes(frame[-3:-1], "big"):
                del buffer[:1]
                continue

            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        data: bytes = info or (b"\x00" if command == self.COMMAND_TELEMETRY else b"")
        body: bytes = bytes([self.VERSION_REQUEST, 0x00, self.FUNCTION, command]) + len(data).to_bytes(2, "big") + data
        return bytes([self.HEAD]) + body + self.calculate_crc(body).to_bytes(2, "big") + bytes([self.TAIL])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.MIN_FRAME or frame[0] != self.HEAD or frame[-1] != self.TAIL or len(frame) != int.from_bytes(frame[5:7], "big") + self.MIN_FRAME:
            self._log.warning(f"Seplos frame has the wrong start, end or size: {frame.hex()}")
            return None

        if frame[1] != self.VERSION_REPLY:
            self._log.warning(f"Seplos frame has an unknown version: {frame[1]:#04x}")
            return None

        calculated: int = self.calculate_crc(frame[1:-3])
        received: int = int.from_bytes(frame[-3:-1], "big")
        if calculated != received:
            self._log.warning(f"Seplos checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        if frame[4] != 0:
            self._log.warning(f"Seplos board reported error code {frame[4]:#04x}")
            return None

        return SimpleNamespace(command=frame[3], payload=frame)

    def _poll_commands(self) -> list[int]:
        return [self.COMMAND_TELEMETRY]

    def _read_command(self, command : int) -> bytes | None:
        frame: bytes | None = self._exchange_until(command, lambda candidate: len(candidate) > 3 and candidate[3] == command)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        frame: bytes = raw_data.get(self.COMMAND_TELEMETRY, b"")
        if len(frame) <= self.CELL_COUNT:
            return {}

        cells: int = min(frame[self.CELL_COUNT], self.MAX_CELLS)
        sensors_at: int = self.CELL_FIRST + 2 * frame[self.CELL_COUNT]
        if len(frame) <= sensors_at:
            return {}

        sensors: int = min(frame[sensors_at], self.MAX_SENSORS)
        values_at: int = sensors_at + 1 + 2 * frame[sensors_at]
        if len(frame) < values_at + self.VALUES_MIN:
            self._log.warning(f"Seplos frame is too short for {cells} cells and {sensors} sensors: {len(frame)} bytes")
            return {}

        blocks: dict[int, bytes] = {
            self.COMMAND_TELEMETRY: frame,
            self.BLOCK_TEMPERATURES: frame[sensors_at:sensors_at + 1 + 2 * sensors],
            self.BLOCK_VALUES: frame[values_at:],
        }

        absent: set[str] = set()
        entry: registry_map_entry
        for entry in self._registry_map():
            cell: re.Match[str] | None = re.fullmatch(r"cell_(\d+)_voltage", entry.variable_name)
            sensor: re.Match[str] | None = re.fullmatch(r"temperature_(\d+)", entry.variable_name)
            if (cell is not None and int(cell.group(1)) > cells) or (sensor is not None and int(sensor.group(1)) > sensors):
                absent.add(entry.variable_name)

        return self.protocolSettings.process_registery(blocks, registry_map=self._drop_entries(self._registry_map(), absent))
