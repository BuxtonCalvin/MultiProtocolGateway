# Description: Bluetooth Low Energy transport for Renogy batteries with the BT-1 / BT-2 Bluetooth module: Modbus RTU frames over the FFD0 and FFF0 services
# File: serial_bluetooth_renogy.py
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

# Renogy Smart Lithium batteries with a BT-1 or BT-2 module (advertising as BT-TH-xxxx) bridge to the battery's
# Modbus RTU port, on unit 0x30; the framing is in serial_bluetooth_modbus. Renogy splits the characteristics
# over two services: requests are written to FFD1 (service FFD0) and answers notify on FFF1 (service FFF0).
#
# Three windows are read every cycle: the pack values (registers 5042 to 5048), the cell and temperature block
# (5000 to 5033) and the status block (5100 to 5107); the serial number, name and firmware (5104 to 5131) once.
# Alarm bits are not decoded. This transport only reads.
import re

from defs.common import TransportSettings

from .serial_bluetooth_modbus import serial_bluetooth_modbus


class serial_bluetooth_renogy(serial_bluetooth_modbus):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "ffd0"
    EXTRA_SERVICE_UUIDS : tuple[str, ...] = ("fff0",)
    NOTIFY_UUID : str = "fff1"
    WRITE_UUID : str = "ffd1"

    UNIT : int = 0x30
    BLOCKS : dict[int, int] = {0x13B2: 7, 0x1388: 34, 0x13EC: 8, 0x13F0: 28}
    STARTUP_BLOCKS : frozenset[int] = frozenset({0x13F0})

    CELL_COUNT_REGISTER : int = 0x1388
    TEMPERATURE_COUNT_REGISTER : int = 0x1399
    MAX_CELLS : int = 16
    MAX_SENSORS : int = 16

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    def _request_unit(self) -> int:
        return self.UNIT

    def _absent_rows(self, registers : dict[int, int]) -> set[str]:
        ''' cells and sensors beyond the counts the battery reports '''
        absent: set[str] = set()
        cells: int = min(registers.get(self.CELL_COUNT_REGISTER, 0), self.MAX_CELLS)
        sensors: int = min(registers.get(self.TEMPERATURE_COUNT_REGISTER, 0), self.MAX_SENSORS)

        for entry in self._registry_map():
            name: str = entry.variable_name
            cell: re.Match[str] | None = re.fullmatch(r"cell_(\d+)_voltage", name)
            sensor: re.Match[str] | None = re.fullmatch(r"temperature_(\d+)", name)
            if cell is not None and int(cell.group(1)) > cells:
                absent.add(name)
            elif sensor is not None and int(sensor.group(1)) > sensors:
                absent.add(name)
        return absent
