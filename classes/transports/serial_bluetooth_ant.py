# Description: Bluetooth Low Energy transport for ANT BMS (ANT-BLE24 / ANT-BLE3x): the 7E A1 ... AA 55 status frame over the FFE0 service, normalized to a fixed layout for the protocol map
# File: serial_bluetooth_ant.py
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

# ANT BMS boards with Bluetooth (advertising as ANT-BLE24... or ANT-BLE3...) use one characteristic, FFE1, for both
# directions, and a frame with a head, a tail and a Modbus CRC-16:
#
#   frame    7E  A1  <type>  <address:2, low byte first>  <length>  <data...>  <crc:2, low byte first>  AA  55
#
# The CRC covers every byte from the A1 to the end of the data. A request has type 0x01 (status, length 0xBE); the
# reply has the same type with 0x10 added. A board that is locked asks for its password first: type 0x23, address
# 0x6A01, the password as the data; the board does not answer it. Set the password setting for such a board.
#
# The status reply has no fixed layout: 32 cell voltages at most start at byte 34, the temperatures follow the
# cells, and the pack values follow the temperatures, so every offset after the cells moves with the cell and
# sensor counts. The map (ant_bms) therefore reads three fixed blocks this transport builds from the reply, as
# serial_pylon does for its variable frames:
#
#   command 0x01    the reply itself: the charge state (byte 7), sensor count (8), cell count (9), cell voltages (34 on)
#   command 0x101   the temperatures: up to 6 sensors, then the MOSFET, then the balancer, 2 bytes each (little-endian)
#   command 0x102   the pack values from the byte after the temperatures on (little-endian)
#
# Cells and sensors beyond the counts the board reports are dropped. This transport only reads; the device
# information request (hardware and software version) is not implemented.
import re
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_ant(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ffe0"
    NOTIFY_UUID : str = "ffe1"
    WRITE_UUID : str = "ffe1"

    HEAD : bytes = b"\x7e\xa1"
    TAIL : bytes = b"\xaa\x55"
    MIN_FRAME : int = 10
    ''' head (2), type, address (2), length, crc (2) and tail (2), with no data '''

    COMMAND_STATUS : int = 0x01
    COMMAND_AUTH : int = 0x23
    REPLY_FLAG : int = 0x10
    STATUS_LENGTH : int = 0xBE
    AUTH_ADDRESS : int = 0x6A01

    BLOCK_TEMPERATURES : int = 0x101
    BLOCK_VALUES : int = 0x102
    SENSOR_COUNT : int = 8
    CELL_COUNT : int = 9
    CELL_FIRST : int = 34
    MAX_CELLS : int = 32
    MAX_SENSORS : int = 6
    SENSOR_SLOTS : int = 8
    ''' six sensors, then the MOSFET, then the balancer '''
    VALUES_MIN : int = 46
    ''' bytes of pack values the map reads, from the first one '''

    def __init__(self, settings : TransportSettings) -> None:
        password: str = settings.get(["password", "secret"], fallback="")
        if not password.isascii() or len(password) > 32:
            msg: str = "ANT password must be ASCII text of at most 32 characters"
            raise ValueError(msg)
        self._password: bytes = password.encode("ascii")
        ''' sent when the connection is made; empty for a board that is not locked. Never logged. '''
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
        head: bytes = serial_bluetooth_ant.HEAD
        while True:
            position: int = buffer.find(head)
            if position < 0:
                keep: int = 1 if buffer.endswith(head[:1]) else 0  # the 7E of a head may be split across notifications
                del buffer[:len(buffer) - keep]
                return None
            if position > 0:
                del buffer[:position]
            if len(buffer) < 6:
                return None

            total: int = buffer[5] + serial_bluetooth_ant.MIN_FRAME
            if len(buffer) < total:
                return None

            frame: bytes = bytes(buffer[:total])
            if not frame.endswith(serial_bluetooth_ant.TAIL) or serial_bluetooth_ant.calculate_crc(frame[1:-4]) != int.from_bytes(frame[-4:-2], "little"):
                del buffer[:1]  # not a real frame start: look for the next head
                continue

            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        if command == self.COMMAND_AUTH:
            address: int = self.AUTH_ADDRESS
            length: int = len(info)
        elif command == self.COMMAND_STATUS:
            address = 0
            length = self.STATUS_LENGTH
        else:
            msg: str = f"ANT command {command:#04x} is not one this transport sends"
            raise ValueError(msg)

        body: bytes = self.HEAD[1:] + bytes([command]) + address.to_bytes(2, "little") + bytes([length]) + info
        return self.HEAD[:1] + body + self.calculate_crc(body).to_bytes(2, "little") + self.TAIL

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.MIN_FRAME or not frame.startswith(self.HEAD) or not frame.endswith(self.TAIL) or len(frame) != frame[5] + self.MIN_FRAME:
            self._log.warning(f"ANT frame has the wrong start, end or size: {frame.hex()}")
            return None

        calculated: int = self.calculate_crc(frame[1:-4])
        received: int = int.from_bytes(frame[-4:-2], "little")
        if calculated != received:
            self._log.warning(f"ANT checksum error, expected {calculated:04X}, got {received:04X}; frame discarded")
            return None

        return SimpleNamespace(type=frame[2], payload=frame)

    def _handshake(self) -> bool:
        ''' sends the password, if one is set; the board does not answer it, so there is nothing to check '''
        if self._password:
            with self._get_port_lock():
                self.send_command(self.COMMAND_AUTH, self._password)
        return True

    def _poll_commands(self) -> list[int]:
        return [self.COMMAND_STATUS]

    def _read_command(self, command : int) -> bytes | None:
        reply: int = command | self.REPLY_FLAG
        frame: bytes | None = self._exchange_until(command, lambda candidate: len(candidate) > 2 and candidate[2] == reply)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        frame: bytes = raw_data.get(self.COMMAND_STATUS, b"")
        if len(frame) <= self.CELL_COUNT:
            return {}

        cells: int = min(frame[self.CELL_COUNT], self.MAX_CELLS)
        sensors: int = min(frame[self.SENSOR_COUNT], self.MAX_SENSORS)
        temperatures_at: int = self.CELL_FIRST + 2 * cells
        values_at: int = temperatures_at + 2 * (sensors + 2)
        if len(frame) < values_at + self.VALUES_MIN:
            self._log.warning(f"ANT frame is too short for {cells} cells and {sensors} sensors: {len(frame)} bytes")
            return {}

        temperatures: bytes = frame[temperatures_at:temperatures_at + 2 * sensors].ljust(2 * self.MAX_SENSORS, b"\x00")
        temperatures += frame[temperatures_at + 2 * sensors:values_at]
        blocks: dict[int, bytes] = {self.COMMAND_STATUS: frame, self.BLOCK_TEMPERATURES: temperatures, self.BLOCK_VALUES: frame[values_at:]}

        absent: set[str] = set()
        entry: registry_map_entry
        for entry in self._registry_map():
            cell: re.Match[str] | None = re.fullmatch(r"cell_(\d+)_voltage", entry.variable_name)
            sensor: re.Match[str] | None = re.fullmatch(r"temperature_(\d+)", entry.variable_name)
            if (cell is not None and int(cell.group(1)) > cells) or (sensor is not None and int(sensor.group(1)) > sensors):
                absent.add(entry.variable_name)

        return self.protocolSettings.process_registery(blocks, registry_map=self._drop_entries(self._registry_map(), absent))
