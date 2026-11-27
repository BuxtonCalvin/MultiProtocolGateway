# Description: Bluetooth Low Energy transport for EG4 batteries: Modbus RTU frames carried over the 1000 notify/write service, decoded with the eg4_ll_s register map
# File: serial_bluetooth_eg4.py
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

# The Bluetooth module of an EG4 battery (LL, LL-S and the 12V and 48V LifePower4 packs that carry one) is a
# bridge to the battery's Modbus RTU port; the framing is in serial_bluetooth_modbus. The registers are those of
# the EG4 LL-S map (eg4_ll_s), the map modbus_eg4_ll_s_rtu reads over RS485, so that protocol is used unchanged:
# set protocol_version = eg4_ll_s. The pack answers on unit 1.
#
# Two windows are read, the two the EG4 app uses: registers 0 to 38 (voltage, current, cell voltages,
# temperatures, state of charge, status) every cycle, and registers 105 to 150 (model, firmware and serial
# number) once per connection. Registers outside them (the protection settings) are not read over Bluetooth.
# This transport only reads; writing registers is not implemented.
from defs.common import TransportSettings

from .serial_bluetooth_modbus import serial_bluetooth_modbus


class serial_bluetooth_eg4(serial_bluetooth_modbus):

    transport_type : str = "scraper"
    ''' declared here, not inherited: the web UI scanner reads this file with ast and does not follow inheritance '''

    SERVICE_UUID : str = "1000"
    NOTIFY_UUID : str = "1002"
    WRITE_UUID : str = "1001"

    MIN_ADDRESS : int = 1
    MAX_ADDRESS : int = 247
    ''' the address setting is the Modbus unit id; the pack answers on 1 '''

    COMMAND_STATUS : int = 0
    COMMAND_INFO : int = 105
    BLOCKS : dict[int, int] = {COMMAND_STATUS: 39, COMMAND_INFO: 46}
    STARTUP_BLOCKS : frozenset[int] = frozenset({COMMAND_INFO})

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)
