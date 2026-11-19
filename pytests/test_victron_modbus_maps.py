# Description: Victron Modbus maps follow the CCGX Modbus-TCP register list, including its scale factors.
# File: test_victron_modbus_maps.py
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

"""
Victron's "CCGX Modbus-TCP register list" gives each register a scale factor and says to **divide** the register by it
(battery voltage, scale 100: raw 5123 is 51.23 V; consumed Ah, scale -10: raw 100 is -10.0 Ah).  ``victron_gx_v3.3`` had
copied the factor into the unit as a multiplier (230.1 V published as 23010), and five other maps used register numbers
that belong to different quantities in the vendor list.  ``victron_bmv_battery_monitor`` loaded nothing because its
map file was not named ``<protocol>.holding_registry_map.csv``.

The expected values below come from the vendor list: addresses, types and scale factors.
"""

from __future__ import annotations

import pytest

from classes.protocol_settings import Registry_Type, protocol_settings
from pytests.conftest import approx


def _decode(protocol: str, registers: dict[int, int]) -> dict[str, int | float | str]:
    settings = protocol_settings(protocol)
    return settings.process_registery(registers, settings.get_registry_map(Registry_Type.HOLDING))


@pytest.mark.parametrize(
    "protocol",
    ["victron_bmv_battery_monitor", "victron_smartsolar_mppt", "victron_multiplus_quattro", "victron_phoenix_inverter", "victron_venus_gx_system", "victron_gx_v3.3"],
)
def test_every_modbus_map_loads_registers(protocol: str) -> None:
    assert protocol_settings(protocol).get_registry_map(Registry_Type.HOLDING)


def test_bmv_registers_and_scales() -> None:
    """battery service: 259 voltage /100, 261 current /10 (signed), 265 consumed Ah /-10, 266 SOC /10."""
    info: dict[str, int | float | str] = _decode("victron_bmv_battery_monitor", {259: 5123, 261: 65413, 265: 100, 266: 855})

    assert info["battery_voltage"] == approx(51.23)
    assert info["current"] == approx(-12.3)
    assert info["consumed_amphours"] == approx(-10.0)
    assert info["state_of_charge"] == approx(85.5)


def test_smartsolar_registers_and_scales() -> None:
    """solarcharger service: 771 battery voltage /100, 772 battery current /10, 776 PV voltage /100, 777 PV current /10."""
    info: dict[str, int | float | str] = _decode("victron_smartsolar_mppt", {771: 5240, 772: 253, 776: 12345, 777: 61})

    assert info["battery_voltage"] == approx(52.4)
    assert info["battery_current"] == approx(25.3)
    assert info["pv_voltage"] == approx(123.45)
    assert info["pv_current"] == approx(6.1)


def test_vebus_registers_and_scales() -> None:
    """vebus service: 3 AC input voltage /10, 9 input frequency /100."""
    info: dict[str, int | float | str] = _decode("victron_multiplus_quattro", {3: 2301, 9: 5001})

    assert info["input_voltage_phase_1"] == approx(230.1)
    assert info["input_frequency_1"] == approx(50.01)


def test_system_registers_and_scales() -> None:
    """system service: 840 battery voltage /10, 841 battery current /10, 842 power, 843 SOC."""
    info: dict[str, int | float | str] = _decode("victron_venus_gx_system", {840: 512, 841: 65286, 842: 1234, 843: 87})

    assert info["battery_voltage_system"] == approx(51.2)
    assert info["battery_current_system"] == approx(-25.0)
    assert info["battery_power_system"] == 1234
    assert info["battery_state_of_charge_system"] == 87


def test_gx_v3_3_divides_by_the_scale_factor() -> None:
    """Raw 2301 with scale 10 is 230.1 V; it used to publish as 23010."""
    info: dict[str, int | float | str] = _decode("victron_gx_v3.3", {3: 2301, 9: 5001, 12: 1500, 840: 512, 841: 65286, 845: 500, 846: 600})

    assert info["vebus/ac/activein/l1/v"] == approx(230.1)
    assert info["vebus/ac/activein/l1/f"] == approx(50.01)
    assert info["vebus/ac/activein/l1/p"] == 15000  # scale 0.1: the register holds the power divided by 10
    assert info["system/dc/battery/voltage"] == approx(51.2)
    assert info["system/dc/battery/current"] == approx(-25.0)
    assert info["system/dc/battery/consumedamphours"] == approx(-50.0)
    assert info["system/dc/battery/timetogo"] == 60000  # scale 0.01 s: the register holds the time divided by 100


def test_gx_v3_3_battery_state_enum() -> None:
    info: dict[str, int | float | str] = _decode("victron_gx_v3.3", {844: 1})

    assert info["system/dc/battery/state"] == 1
    assert info["system/dc/battery/state_desc"] == "charging"


def test_gx_v3_3_output_power_phase_1_has_a_definition() -> None:
    info: dict[str, int | float | str] = _decode("victron_gx_v3.3", {23: 300})

    assert info["vebus/ac/out/l1/p"] == 3000
