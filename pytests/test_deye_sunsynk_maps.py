# Description: Deye / Sunsynk maps keep one register layout per hardware family and decode the vendor encodings.
# File: test_deye_sunsynk_maps.py
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
Deye does not publish one Modbus map for the whole brand.  Single-phase hybrids (SG01/02/03/04/05LP1) keep their
data in registers 0-350, three-phase hybrids (SG04/05LP3 low voltage, SG01/02HP3 high voltage) in 500-730.  The old
``deye_sunsynk`` maps mixed both layouts, split 32-bit counters into ``_hi``/``_lo`` rows that were never combined,
published temperatures without the +1000 offset and carried string-inverter names in the fault tables.

The expected values below come from the Deye protocol documents and the Deye alarm list: addresses, low word first
for 32-bit counters, the (raw - 1000) x 0.1 temperature encoding and the F01-F64 fault numbers.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from classes.discovery.startup import startup_entries
from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from pytests.conftest import approx

PROTOCOL_DIR: Path = Path(__file__).resolve().parents[1] / "protocols"
ALL_PROTOCOLS: list[str] = [
    "deye_sunsynk",
    "deye_sunsynk_hybrid",
    "deye_sunsynk_3ph_lv",
    "deye_sunsynk_3ph_hv",
    "deye_sunsynk_micro_ess",
]


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _decode(name: str, registers: dict[int, int]) -> dict[str, int | float | str]:
    settings: protocol_settings = _load(name)
    return settings.process_registery(registers, settings.get_registry_map(Registry_Type.HOLDING))


def _entries(name: str) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in _load(name).get_registry_map(Registry_Type.HOLDING)}


@pytest.mark.parametrize("name", ALL_PROTOCOLS)
def test_every_map_loads_and_names_are_unique(name: str) -> None:
    assert _load(name).get_registry_map(Registry_Type.HOLDING)
    with (PROTOCOL_DIR / "deye_sunsynk" / f"{name}.holding_registry_map.csv").open(newline="", encoding="utf-8") as handle:
        names: list[str] = [row["variable_name"] for row in csv.DictReader(handle)]
    assert len(names) == len(set(names))
    assert not any(n.endswith(("_hi", "_lo")) for n in names), "32-bit values are one row, not two halves"


def test_basic_map_is_a_subset_of_the_single_phase_map() -> None:
    basic: dict[str, registry_map_entry] = _entries("deye_sunsynk")
    full: dict[str, registry_map_entry] = _entries("deye_sunsynk_hybrid")
    assert set(basic) <= set(full)
    assert {n: basic[n].register for n in basic} == {n: full[n].register for n in basic}


def test_families_use_their_own_register_layout() -> None:
    single: dict[str, registry_map_entry] = _entries("deye_sunsynk_hybrid")
    three: dict[str, registry_map_entry] = _entries("deye_sunsynk_3ph_lv")
    assert (single["pv1_power"].register, single["battery_soc"].register, single["battery_power"].register) == (186, 184, 190)
    assert (three["pv1_power"].register, three["battery_soc"].register, three["battery_power"].register) == (672, 588, 590)
    assert single["overall_state"].register == 59 and three["overall_state"].register == 500


def test_32_bit_counters_are_low_word_first() -> None:
    """V118: total grid import is 78 + 80 (79 is the grid frequency), low word first."""
    info: dict[str, int | float | str] = _decode("deye_sunsynk_hybrid", {78: 0x1234, 79: 5001, 80: 0x0001})
    assert info["total_grid_import"] == approx(0x00011234 * 0.1)
    assert info["grid_frequency"] == approx(50.01)
    three: dict[str, int | float | str] = _decode("deye_sunsynk_3ph_lv", {534: 0x86A0, 535: 0x0001})
    assert three["total_pv_energy"] == approx(10000.0)


def test_rated_power_and_serial_number_are_startup_rows() -> None:
    protocol: protocol_settings = _load("deye_sunsynk_hybrid")
    names: set[str] = {e.variable_name for e in startup_entries(protocol, Registry_Type.HOLDING)}
    assert {"device_type", "serial_number", "rated_power", "control_board_version", "communication_board_version"} <= names
    polled: list[tuple[int, int]] = protocol.calculate_registry_ranges(protocol.get_registry_map(Registry_Type.HOLDING), 400)
    assert not any(start <= 16 < start + count for start, count in polled), "rated power is read once, not polled"
    info: dict[str, int | float | str] = _decode(
        "deye_sunsynk_hybrid",
        {0: 0x0300, 3: 0x3132, 4: 0x3334, 5: 0x3536, 6: 0x3738, 7: 0x3930, 16: 0x3880, 17: 1},
    )
    assert info["serial_number"] == "1234567890"
    assert info["rated_power"] == 8000
    assert info["device_type_desc"] == "Single-phase hybrid (0x0300)"


@pytest.mark.parametrize(("name", "register"), [("deye_sunsynk_hybrid", 90), ("deye_sunsynk_3ph_lv", 540)])
def test_temperatures_remove_the_1000_offset(name: str, register: int) -> None:
    variable: str = "dc_transformer_temperature"
    assert _decode(name, {register: 1250})[variable] == approx(25.0)
    assert _decode(name, {register: 1000})[variable] == approx(0.0)


def test_signed_power_and_current_registers() -> None:
    info: dict[str, int | float | str] = _decode("deye_sunsynk_hybrid", {190: 65436, 191: 65386, 169: 65436})
    assert info["battery_power"] == -100
    assert info["battery_current"] == approx(-1.5)
    assert info["grid_power"] == -100


def test_bit_fields_do_not_leak_other_bits() -> None:
    info: dict[str, int | float | str] = _decode("deye_sunsynk_hybrid", {232: 0x0101, 243: 0x0003, 248: 0x0000})
    assert info["grid_charge_enabled"] == 1
    assert info["priority_load"] == 1
    assert info["use_timer"] == 0


def test_fault_flags_use_the_hybrid_alarm_list() -> None:
    """F13 = reg 103 bit 12, F58 (BMS communication) = reg 106 bit 9, F62 (DRMs0 stop) = reg 106 bit 13."""
    info: dict[str, int | float | str] = _decode("deye_sunsynk_hybrid", {103: 1 << 12, 106: (1 << 9) | (1 << 13)})
    assert info["fault_1"] == "Grid_Mode_changed - F13"
    assert info["fault_4"] == "BMS_Communication_Fault - F58,DRMs0_Stop - F62"
    three: dict[str, int | float | str] = _decode("deye_sunsynk_3ph_hv", {558: 1 << 14})
    assert three["fault_4"] == "Arc_Fault - F63"


def test_high_voltage_map_scales_pv_and_battery_power_by_ten() -> None:
    low: dict[str, registry_map_entry] = _entries("deye_sunsynk_3ph_lv")
    high: dict[str, registry_map_entry] = _entries("deye_sunsynk_3ph_hv")
    assert low["pv1_power"].unit_mod == 1.0 and high["pv1_power"].unit_mod == 10.0
    assert low["battery_voltage"].unit_mod == approx(0.01) and high["battery_voltage"].unit_mod == approx(0.1)
    assert "pv8_power" in high and "pv8_power" not in low


def test_inverter_enable_registers_are_read_only() -> None:
    """Documentation disagrees on the 0/1 meaning and a wrong write can stop the inverter."""
    for name in ("deye_sunsynk_hybrid", "deye_sunsynk_3ph_lv", "deye_sunsynk_3ph_hv", "deye_sunsynk_micro_ess"):
        entry: registry_map_entry = _entries(name)["inverter_enabled"]
        assert entry.write_mode.name == "READ"


def test_micro_hybrid_ess_uses_its_own_register_block() -> None:
    """The micro hybrid / ESS family keeps live data in 1000-1130 and has no PV or fault registers."""
    entries: dict[str, registry_map_entry] = _entries("deye_sunsynk_micro_ess")
    assert (entries["battery_soc"].register, entries["battery_voltage"].register, entries["battery_power"].register) == (1069, 1067, 1071)
    assert not any(name.startswith(("pv", "fault")) for name in entries)
    info: dict[str, int | float | str] = _decode(
        "deye_sunsynk_micro_ess",
        {17: 30000, 1028: 0xFF9C, 1029: 0xFFFF, 1067: 5320, 1071: 65436, 1091: 0x1234, 1092: 1},
    )
    assert info["rated_power"] == 3000
    assert info["inverter_power"] == approx(-10.0), "signed 32-bit, low word first"
    assert info["battery_voltage"] == approx(53.2)
    assert info["battery_power"] == -100
    assert info["total_grid_import"] == approx(0x00011234 * 0.1)


def test_split_phase_units_use_the_single_phase_map() -> None:
    """SG01LP1-US is the single-phase platform with two 120 V legs, so L1/L2 rows live in the single-phase map."""
    entries: dict[str, registry_map_entry] = _entries("deye_sunsynk_hybrid")
    assert (entries["grid_current_l1"].register, entries["grid_current_l2"].register) == (160, 161)
    assert (entries["load_l1_power"].register, entries["load_l2_power"].register) == (176, 177)
