# Description: Bluetooth Low Energy transport for Daly smart BMS: Modbus RTU frames over the FFF0 service, in both protocol variants Daly ships (D2 and X81)
# File: serial_bluetooth_daly.py
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

# Daly smart BMS boards with Bluetooth (advertising as DL-xxxx) put a Modbus RTU bridge on service FFF0; the
# framing is in serial_bluetooth_modbus. Daly ships two protocol variants, each with its own protocol file:
#
#   daly_bms_d2    boards that talk to unit 0xD2 and answer from it; up to 32 cells and 8 sensors
#   daly_bms_x81   newer boards that are asked on unit 0x81 and answer from unit 0x51; up to 48 cells and 8 sensors
#
# Set protocol_version to the one of your board. If you are not sure, try daly_bms_d2 first: a board of the other
# kind does not answer it, and the log says the board did not answer.
#
# D2 reads registers 0 to 61 (cells, sensors, voltage, current, state of charge, counters, MOSFETs) and the MOSFET
# temperature at 62 to 70 every cycle, and the versions at 169 to 200 once. X81 reads 0 to 63 and 65 to 126 every
# cycle and the versions and serial number at 376 to 449 once. Current is offset by 30000 (so that discharge is
# negative); temperatures are offset by 40 degrees. This transport only reads.
import re

from defs.common import TransportSettings

from .serial_bluetooth_modbus import serial_bluetooth_modbus


class serial_bluetooth_daly(serial_bluetooth_modbus):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "fff0"
    NOTIFY_UUID : str = "fff1"
    WRITE_UUID : str = "fff2"

    VARIANT_X81 : str = "daly_bms_x81"
    ''' the protocol that selects the newer variant; any other (daly_bms_d2) is the older '''

    D2_BLOCKS : dict[int, int] = {0x0000: 62, 0x003E: 9, 0x00A9: 32}
    D2_STARTUP : frozenset[int] = frozenset({0x00A9})
    X81_BLOCKS : dict[int, int] = {0x0000: 64, 0x0041: 62, 0x0178: 74}
    X81_STARTUP : frozenset[int] = frozenset({0x0178})
    ''' the windows of each variant: first register -> count '''

    UNIT_D2 : int = 0xD2
    UNIT_X81_REQUEST : int = 0x81
    UNIT_X81_RESPONSE : int = 0x51

    CELL_COUNT_REGISTER : dict[bool, int] = {False: 49, True: 60}
    TEMPERATURE_COUNT_REGISTER : dict[bool, int] = {False: 50, True: 61}
    MOSFET_TEMPERATURE_REGISTER : int = 66
    ''' where the cell count, the sensor count and the MOSFET temperature are, for (D2, X81) '''
    MAX_SENSORS : int = 8

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

    @property
    def _x81(self) -> bool:
        return self.protocolSettings is not None and self.protocolSettings.protocol == self.VARIANT_X81

    def _blocks(self) -> dict[int, int]:
        return self.X81_BLOCKS if self._x81 else self.D2_BLOCKS

    def _startup_blocks(self) -> frozenset[int]:
        return self.X81_STARTUP if self._x81 else self.D2_STARTUP

    def _request_unit(self) -> int:
        return self.UNIT_X81_REQUEST if self._x81 else self.UNIT_D2

    def _response_unit(self) -> int:
        return self.UNIT_X81_RESPONSE if self._x81 else self.UNIT_D2

    def _absent_rows(self, registers : dict[int, int]) -> set[str]:
        ''' cells and sensors beyond the counts the board reports, and a MOSFET sensor that is not fitted '''
        absent: set[str] = set()
        cells: int = registers.get(self.CELL_COUNT_REGISTER[self._x81], 0)
        sensors: int = min(registers.get(self.TEMPERATURE_COUNT_REGISTER[self._x81], 0), self.MAX_SENSORS)

        for entry in self._registry_map():
            name: str = entry.variable_name
            cell: re.Match[str] | None = re.fullmatch(r"cell_(\d+)_voltage", name)
            sensor: re.Match[str] | None = re.fullmatch(r"temperature_(\d+)", name)
            if cell is not None and int(cell.group(1)) > cells:
                absent.add(name)
            elif sensor is not None and int(sensor.group(1)) > sensors:
                absent.add(name)
            elif name == "mosfet_temperature" and registers.get(entry.register, 0) in (0, 0xFFFF):
                absent.add(name)
        return absent
