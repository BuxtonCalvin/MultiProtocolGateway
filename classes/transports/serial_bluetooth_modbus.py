# Description: Modbus RTU over Bluetooth Low Energy: the framing, polling windows and register decoding shared by the batteries whose Bluetooth module is a bridge to a Modbus port (EG4, Daly, Renogy)
# File: serial_bluetooth_modbus.py
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

# Several battery makers put a Bluetooth module in front of the Modbus RTU port of the BMS. The gateway writes
# ordinary "read registers" frames and the answers notify back:
#
#   request    <unit> <function> <first register:2> <count:2> <crc:2, low byte first>
#   response   <unit> <function> <byte count> <registers, 2 bytes each, high byte first> <crc:2, low byte first>
#
# This class is that framing. A manufacturer's transport names the windows of registers to read (BLOCKS), which of
# them are read once per connection (startup), the unit the request is sent to and the unit the answer comes from
# (a few makers use different ones), and the UUIDs. The values are decoded with a HOLDING protocol map whose
# register numbers are the Modbus register addresses, exactly as for a wired Modbus transport, so a pack's map can
# be shared between the wired and the Bluetooth transport when the registers are the same (EG4 does).
#
# A transport only reads. A pack with fewer cells than the map has rows for reports 0 V for them; override
# _absent_rows() for a protocol that says how many cells (or sensors) it has.
import re
from types import SimpleNamespace

from classes.protocol_settings import registry_map_entry

from .serial_bluetooth import serial_bluetooth


class serial_bluetooth_modbus(serial_bluetooth):

    transport_type : str = "base class"
    ''' abstract; a manufacturer's subclass declares its own transport_type = "scraper" '''

    FUNCTION_READ : int = 0x03
    FUNCTION_INPUT : int = 0x04
    FUNCTION_ERROR_FLAG : int = 0x80
    ''' the function code of a reply is the request's with the top bit set when the device refuses it '''
    EXCEPTION_FRAME : int = 5
    ''' unit, function, exception code and the crc '''

    BLOCKS : dict[int, int] = {}
    ''' the commands, which are the first register of a window, and how many registers each reads; a subclass sets them '''
    STARTUP_BLOCKS : frozenset[int] = frozenset()
    ''' the windows that never change while connected (model, versions, serial number), read once per connection '''

    CELL_ROW : str = r"cell_\d+_voltage"
    ''' the map rows of the cell voltages '''

    def _blocks(self) -> dict[int, int]:
        ''' the windows this device is read through; a subclass whose windows depend on the model returns the right set '''
        return self.BLOCKS

    def _startup_blocks(self) -> frozenset[int]:
        return self.STARTUP_BLOCKS

    def _request_unit(self) -> int:
        ''' the unit the requests are sent to: the address setting unless the manufacturer fixes it '''
        return self.address

    def _response_unit(self) -> int:
        ''' the unit the answers come from, which is the request's unless the manufacturer says otherwise '''
        return self._request_unit()

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
            if len(buffer) < 3:
                return None

            function: int = buffer[1] & ~serial_bluetooth_modbus.FUNCTION_ERROR_FLAG
            if buffer[0] == 0 or function not in (serial_bluetooth_modbus.FUNCTION_READ, serial_bluetooth_modbus.FUNCTION_INPUT):
                del buffer[:1]  # not a frame start: look for the next one
                continue

            refused: bool = bool(buffer[1] & serial_bluetooth_modbus.FUNCTION_ERROR_FLAG)
            total: int = serial_bluetooth_modbus.EXCEPTION_FRAME if refused else buffer[2] + 5
            if len(buffer) < total:
                return None

            frame: bytes = bytes(buffer[:total])
            if serial_bluetooth_modbus.calculate_crc(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                del buffer[:1]
                continue

            del buffer[:total]
            return frame

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        ''' a read of the window that starts at register `command`; info, when given, is the 2-byte count '''
        count: bytes = info or self._blocks()[command].to_bytes(2, "big")
        body: bytes = bytes([self._request_unit(), self.FUNCTION_READ]) + command.to_bytes(2, "big") + count
        return body + self.calculate_crc(body).to_bytes(2, "little")

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        frame: bytes = bytes(raw_frame)

        if len(frame) < self.EXCEPTION_FRAME or frame[0] != self._response_unit():
            self._log.warning(f"Modbus frame is too short or is for another unit: {frame.hex()}")
            return None

        if self.calculate_crc(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            self._log.warning(f"Modbus checksum error; frame discarded: {frame.hex()}")
            return None

        if frame[1] & self.FUNCTION_ERROR_FLAG:
            self._log.warning(f"Device refused the read with exception {frame[2]:#04x}")
            return None

        if frame[1] != self.FUNCTION_READ or frame[2] != len(frame) - 5:
            self._log.warning(f"Modbus frame is not a complete read reply: {frame.hex()}")
            return None

        return SimpleNamespace(function=frame[1], payload=frame[3:-2])

    def _poll_commands(self) -> list[int]:
        return list(self._blocks())

    def _startup_commands(self) -> set[int]:
        return set(self._startup_blocks())

    def _read_command(self, command : int) -> bytes | None:
        unit: int = self._response_unit()
        frame: bytes | None = self._exchange_until(command, lambda candidate: candidate[0] == unit)
        if frame is None:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        if decoded is None:
            return None

        payload: bytes = bytes(decoded.payload)
        expected: int = self._blocks()[command]
        if len(payload) != 2 * expected:
            self._log.warning(f"Read of {expected} registers from {command} returned {len(payload) // 2}")
            return None
        return payload

    def _absent_rows(self, registers : dict[int, int]) -> set[str]:
        ''' the rows of cells and sensors the device does not have; by default the cell voltages that read 0 V '''
        absent: set[str] = set()
        entry: registry_map_entry
        for entry in self._registry_map():
            if registers.get(entry.register) == 0 and re.fullmatch(self.CELL_ROW, entry.variable_name):
                absent.add(entry.variable_name)
        return absent

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        if self.protocolSettings is None:
            return {}

        registers: dict[int, int] = {}
        for first, payload in raw_data.items():
            for index in range(len(payload) // 2):
                registers[first + index] = int.from_bytes(payload[2 * index:2 * index + 2], "big")
        if not registers:
            return {}

        entries: list[registry_map_entry] = self._drop_entries(self._registry_map(), self._absent_rows(registers))
        return self.protocolSettings.process_registery(registers, registry_map=entries)
