# Description: Bluetooth Low Energy transport for JK (Jikong) BMS, including the JK-PB series: the JK02 300 byte status frames carried over the FFE0 service
# File: serial_bluetooth_jk.py
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

# JK (Jikong) BMS over Bluetooth Low Energy, the "JK02" protocol of the JK-PB, JK-B and JK-BD families. It is not
# the RS485 4E 57 protocol of serial_jk. The BMS has one characteristic (FFE1) that is both written and notified.
#
#   request   AA 55 90 EB  <command>  <length>  <value, padded to 13 bytes>  <crc>        (20 bytes)
#   response  55 AA EB 90  <type>  <counter>  <data...>  <crc>                           (300 bytes)
#
# The crc is the low byte of the sum of every byte before it. Command 0x97 asks for the device information
# (reply type 0x03) and 0x96 for the live status (reply type 0x02). The BMS also sends frames nobody asked for
# (settings, type 0x01) and a 20 byte "ready" message (type 0xC8), and some Bluetooth modules append the text
# "AT" + CR LF to a frame; extract_frame removes it.
#
# The protocol map (jk_bms_ble) names each value by command and byte offset into the whole 300 byte frame, the
# numbering used in the public protocol notes, and every multi-byte value is little-endian. The map is the
# JK02_32S layout, used by firmware 11 and later, which covers the JK-PB series. Older firmware (10.x, the
# 24-cell layout) is detected on connection and refused, because its offsets differ.
#
# Rows for cells and temperature sensors the BMS does not have are dropped: a cell is present when its bit is
# set in the cell mask, a sensor when its bit is set in the sensor mask and it does not report -200.0 C.
# This transport only reads.
import re
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_jk(serial_bluetooth):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ffe0"
    NOTIFY_UUID : str = "ffe1"
    WRITE_UUID : str = "ffe1"

    HEAD_RESPONSE : bytes = b"\x55\xaa\xeb\x90"
    HEAD_COMMAND : bytes = b"\xaa\x55\x90\xeb"
    BLE_MODULE_MESSAGE : bytes = b"AT\r\n"
    ''' text some Bluetooth modules put in front of or behind a frame '''

    FRAME_SIZE : int = 300
    COMMAND_SIZE : int = 20
    TYPE_POSITION : int = 4

    CMD_DEVICE_INFO : int = 0x97
    CMD_CELL_INFO : int = 0x96
    TYPE_DEVICE_INFO : int = 0x03
    TYPE_CELL_INFO : int = 0x02
    TYPE_READY : int = 0xC8
    REPLY_TYPES : dict[int, int] = {CMD_DEVICE_INFO: TYPE_DEVICE_INFO, CMD_CELL_INFO: TYPE_CELL_INFO}

    MIN_SOFTWARE : int = 11
    ''' the first firmware major version with the JK02_32S layout '''
    SOFTWARE_POSITION : int = 30
    SOFTWARE_SIZE : int = 8

    CELL_MASK : int = 70
    SENSOR_MASK : int = 214
    ABSENT_TEMPERATURE : int = -2000
    TEMPERATURE_SENSORS : dict[str, tuple[int, int]] = {
        "mosfet_temperature": (0, 11),
        "battery_temperature_1": (1, 11),
        "battery_temperature_2": (2, 11),
        "mosfet_temperature_2": (3, 11),
        "battery_temperature_3": (4, 14),
        "battery_temperature_4": (5, 14),
    }
    ''' row name -> (bit in the sensor mask, first firmware major version that has the sensor) '''

    RESPONSE_ATTEMPTS : int = 8
    ''' the BMS pushes settings and status frames of its own between the replies '''
    READY_WAIT : float = 1.0
    ''' seconds to wait for the "ready" message after the device information '''

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
        self._software: int = 0
        ''' the firmware major version read from the device information on connection '''

    @staticmethod
    def calculate_checksum(data : bytes) -> int:
        ''' the low byte of the sum of the bytes '''
        return sum(data) & 0xFF

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        module: bytes = serial_bluetooth_jk.BLE_MODULE_MESSAGE
        while True:
            while buffer.startswith(module):
                del buffer[:len(module)]

            positions: list[tuple[int, int]] = []
            for head, size in ((serial_bluetooth_jk.HEAD_RESPONSE, serial_bluetooth_jk.FRAME_SIZE), (serial_bluetooth_jk.HEAD_COMMAND, serial_bluetooth_jk.COMMAND_SIZE)):
                position: int = buffer.find(head)
                if position >= 0:
                    positions.append((position, size))

            if not positions:
                keep: int = len(serial_bluetooth_jk.HEAD_RESPONSE) - 1  # a start marker may be split across notifications
                del buffer[:max(0, len(buffer) - keep)]
                return None

            start, size = min(positions)
            if start > 0:
                del buffer[:start]
                continue
            if len(buffer) < size:
                return None

            frame: bytes = bytes(buffer[:size])
            del buffer[:size]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        body: bytes = self.HEAD_COMMAND + bytes([command, len(info)]) + info + bytes(13 - len(info))
        return body + bytes([self.calculate_checksum(body)])

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) != self.FRAME_SIZE or not frame.startswith(self.HEAD_RESPONSE):
            self._log.debug(f"JK frame is not a status frame: {frame.hex()}")
            return None

        calculated: int = self.calculate_checksum(frame[:-1])
        if calculated != frame[-1]:
            self._log.warning(f"JK checksum error, expected {calculated:02X}, got {frame[-1]:02X}; frame discarded")
            return None

        return SimpleNamespace(type=frame[self.TYPE_POSITION], payload=frame)

    def _poll_commands(self) -> list[int]:
        return [self.CMD_DEVICE_INFO, self.CMD_CELL_INFO]

    def _read_command(self, command : int) -> bytes | None:
        reply_type: int = self.REPLY_TYPES[command]
        frame: bytes | None = self._exchange_until(command, lambda candidate: len(candidate) == self.FRAME_SIZE and candidate[self.TYPE_POSITION] == reply_type)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        return bytes(decoded.payload) if decoded is not None else None

    def _handshake(self) -> bool:
        '''
        The BMS is asked for its device information first, as its own app does: that tells the firmware, which
        decides whether the map fits, and the BMS then announces that it is ready for status requests. The
        device information is kept as the startup payload, so it is not read twice.
        '''
        payload: bytes | None = self._read_command(self.CMD_DEVICE_INFO)
        if payload is None:
            self._log.warning(f"JK BMS {self.port} did not send its device information")
            return False

        text: str = payload[self.SOFTWARE_POSITION:self.SOFTWARE_POSITION + self.SOFTWARE_SIZE].split(b"\x00")[0].decode("ascii", "replace")
        found: re.Match[str] | None = re.match(r"\d+", text)
        self._software = int(found.group()) if found else 0
        if self._software < self.MIN_SOFTWARE:
            self._log.error(f"JK BMS {self.port} reports firmware '{text}'; this transport supports firmware {self.MIN_SOFTWARE} and later (the JK02_32S layout)")
            return False

        self._startup_payloads[self.CMD_DEVICE_INFO] = payload
        self._sync_static_values()
        self._await_ready()
        return True

    def _await_ready(self) -> None:
        ''' consumes the "ready" message if it has not arrived with the device information; best effort, because newer firmware appends it to that frame '''
        client = self.client
        saved: float = client.timeout
        client.timeout = min(self.READY_WAIT, saved)
        try:
            with self._get_port_lock():
                client.read(reset_buffer=False)
        except OSError as err:
            self._log.debug(f"JK BMS {self.port}: no ready message ({err})")
        finally:
            client.timeout = saved

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        status: bytes = raw_data.get(self.CMD_CELL_INFO, b"")
        absent: set[str] = set()
        if len(status) == self.FRAME_SIZE:
            cell_mask: int = int.from_bytes(status[self.CELL_MASK:self.CELL_MASK + 4], "little")
            sensor_mask: int = status[self.SENSOR_MASK]

            entry: registry_map_entry
            for entry in self._registry_map():
                if entry.register != self.CMD_CELL_INFO:
                    continue

                cell: re.Match[str] | None = re.fullmatch(r"cell_(\d+)_(?:voltage|resistance)", entry.variable_name)
                if cell is not None and not cell_mask >> (int(cell.group(1)) - 1) & 1:
                    absent.add(entry.variable_name)

                sensor: tuple[int, int] | None = self.TEMPERATURE_SENSORS.get(entry.variable_name)
                if sensor is not None:
                    value: int = int.from_bytes(status[entry.register_byte:entry.register_byte + 2], "little", signed=True)
                    if not sensor_mask >> sensor[0] & 1 or self._software < sensor[1] or value == self.ABSENT_TEMPERATURE:
                        absent.add(entry.variable_name)

        entries: list[registry_map_entry] = self._entries_within(raw_data, self._drop_entries(self._registry_map(), absent))
        return self.protocolSettings.process_registery(raw_data, registry_map=entries)
