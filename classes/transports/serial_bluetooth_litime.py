# Description: Bluetooth Low Energy transport for LiTime (and Redodo, ECO-WORTHY, Starry Sea and other rebranded) LiFePO4 batteries: one request, one little-endian status frame, over the FFE0 service
# File: serial_bluetooth_litime.py
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

# LiTime batteries (and the Redodo, ECO-WORTHY T- and Starry Sea packs that share their BMS) answer one request with
# one status frame; the same frame carries everything:
#
#   request    00  00  04  01  <command>  55  AA  <checksum>                          command 0x13
#   response   00  00  <length>  <data...>  <checksum>                                   (length + 4 bytes in all)
#
# The checksum is the low byte of the sum of every byte before it. The values are little-endian. The protocol map
# (litime_bms) names each value by byte offset into the whole frame. Cells and sensors the pack does not have read
# 0: cells that read 0 V are dropped, and sensors beyond the last one that has read a non-zero value are dropped
# (a real 0 degrees on the last sensor is kept once any non-zero reading has shown that the sensor is there). This
# transport only reads. Advertised names look like L-12100BNNA70, LT-12..., P-12..., R-24..., T-12..., S-... .
import re
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_litime(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ffe0"
    NOTIFY_UUID : str = "ffe1"
    WRITE_UUID : str = "ffe2"

    HEAD : bytes = b"\x00\x00"
    HEAD_SIZE : int = 3
    ''' the two zero bytes and the length byte; the frame is HEAD_SIZE + length + 1 (checksum) bytes '''
    MAX_FRAME : int = 255
    MARKER_END : int = 7
    '''a response always continues 01 <command> 55 AA after the length (bytes 3 to 6); a candidate without it is not a response'''
    COMMAND_STATUS : int = 0x13
    MAX_SENSORS : int = 5
    CELL_ROW : str = r"cell_(\d+)_voltage"
    SENSOR_ROW : str = r"temperature_(\d+)"

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        self._sensors: int = 1
        ''' sensors known to be fitted: raised when a later one reads non-zero, never lowered '''

    @staticmethod
    def calculate_checksum(data : bytes) -> int:
        ''' the low byte of the sum of the bytes '''
        return sum(data) & 0xFF

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        head: bytes = serial_bluetooth_litime.HEAD
        while True:
            position: int = buffer.find(head)
            if position < 0:
                del buffer[:max(0, len(buffer) - (len(head) - 1))]  # a marker may be split across notifications
                return None
            if position > 0:
                del buffer[:position]
            if len(buffer) < serial_bluetooth_litime.HEAD_SIZE:
                return None

            total: int = buffer[2] + serial_bluetooth_litime.HEAD_SIZE + 1
            if total > serial_bluetooth_litime.MAX_FRAME or buffer[2] == 0:
                del buffer[:1]  # not a real frame start: look for the next one
                continue
            if len(buffer) < serial_bluetooth_litime.MARKER_END:
                return None
            if buffer[3] != 1 or bytes(buffer[serial_bluetooth_litime.MARKER_END - 2:serial_bluetooth_litime.MARKER_END]) != b"\x55\xaa":
                del buffer[:1]  # a pair of zero bytes inside other data, not a response: its fixed marker is missing
                continue
            if len(buffer) < total:
                return None

            frame: bytes = bytes(buffer[:total])
            if serial_bluetooth_litime.calculate_checksum(frame[:-1]) != frame[-1]:
                del buffer[:1]
                continue

            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        body: bytes = self.HEAD + bytes([4, 1, command, 0x55, 0xAA])
        return body + bytes([self.calculate_checksum(body)])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.HEAD_SIZE + 1 or not frame.startswith(self.HEAD) or len(frame) != frame[2] + self.HEAD_SIZE + 1:
            self._log.warning(f"LiTime frame has the wrong start or size: {frame.hex()}")
            return None

        calculated: int = self.calculate_checksum(frame[:-1])
        if calculated != frame[-1]:
            self._log.warning(f"LiTime checksum error, expected {calculated:02X}, got {frame[-1]:02X}; frame discarded")
            return None

        return SimpleNamespace(payload=frame)

    def _poll_commands(self) -> list[int]:
        return [self.COMMAND_STATUS]

    def _read_command(self, command : int) -> bytes | None:
        frame: bytes | None = self._exchange_until(command, lambda candidate: len(candidate) > 4)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        frame: bytes = raw_data.get(self.COMMAND_STATUS, b"")
        entries: list[registry_map_entry] = self._registry_map()
        values: dict[str, int] = {
            entry.variable_name: int.from_bytes(frame[entry.register_byte:entry.register_byte + 2], "little", signed=True)
            for entry in entries if entry.register == self.COMMAND_STATUS and re.fullmatch(f"{self.CELL_ROW}|{self.SENSOR_ROW}", entry.variable_name)
        }

        for name, value in values.items():
            sensor: re.Match[str] | None = re.fullmatch(self.SENSOR_ROW, name)
            if sensor is not None and value != 0:
                self._sensors = max(self._sensors, int(sensor.group(1)))

        absent: set[str] = set()
        for name, value in values.items():
            sensor = re.fullmatch(self.SENSOR_ROW, name)
            if (sensor is None and value == 0) or (sensor is not None and int(sensor.group(1)) > min(self._sensors, self.MAX_SENSORS)):
                absent.add(name)

        return self.protocolSettings.process_registery(raw_data, registry_map=self._drop_entries(entries, absent))
