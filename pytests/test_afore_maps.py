# Description: Afore maps keep one register layout per protocol family and decode the vendor encodings.
# File: test_afore_maps.py
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
Afore publishes two unrelated Modbus layouts.  The AF-SL single-phase hybrids (T6) keep their data in input registers
507-560, 1002-1027, 2000-2014 and 2500-2515.  The BNT string inverters (T4) keep theirs in input registers 0-26 and
holding registers 0-15.  Both send 32-bit values high word first at the lower address, so neither needs an endian
adjustment.

The expected values below come from the T4 PDF, the Afore BNTxxxKTL-2mppt Solarman definition, the Afore AF-SL Solarman
definition and the evcc afore-hybrid template.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pytest

from classes.discovery.startup import startup_entries
from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
)
from pytests.conftest import approx

PROTOCOL_DIR: Path = Path(__file__).resolve().parents[1] / "protocols" / "afore"
HYBRID: str = "afore_af_sl_hybrid"
T4: str = "afore_bnt_t4"


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _decode(
    name: str, registry_type: Registry_Type, registers: dict[int, int]
) -> dict[str, int | float | str]:
    settings: protocol_settings = _load(name)
    return settings.process_registery(
        registers, settings.get_registry_map(registry_type)
    )


# def _entries(name: str, registry_type: Registry_Type) -> dict[str, registry_map_entry]:
#     return {e.variable_name: e for e in _load(name).get_registry_map(registry_type)}


@pytest.mark.parametrize(
    ("name", "registry_type", "suffix"),
    [
        (HYBRID, Registry_Type.INPUT, "input"),
        (T4, Registry_Type.INPUT, "input"),
        (T4, Registry_Type.HOLDING, "holding"),
    ],
)
def test_every_map_loads_and_names_are_unique(
    name: str, registry_type: Registry_Type, suffix: str
) -> None:
    assert _load(name).get_registry_map(registry_type)
    with (PROTOCOL_DIR / f"{name}.{suffix}_registry_map.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        names: list[str] = [
            row["variable_name"]
            for row in csv.DictReader(handle)
            if not row["register"].startswith("#")
        ]
    assert len(names) == len(set(names))
    assert not any(
        n.endswith(("_hi", "_lo", "_high", "_low")) and "grid_connect" not in n
        for n in names
    ), "32-bit values are one row"


def test_hybrid_has_no_holding_map_and_t4_has_both() -> None:
    assert not _load(HYBRID).get_registry_map(Registry_Type.HOLDING)
    assert _load(T4).get_registry_map(Registry_Type.HOLDING)


def test_hybrid_32_bit_values_are_high_word_first() -> None:
    """evcc reads total grid import as a 32-bit value at 1020; the Solarman definition reads only the low word at 1021."""
    info: dict[str, int | float | str] = _decode(
        HYBRID,
        Registry_Type.INPUT,
        {
            1020: 0x0001,
            1021: 0x86A0,
            553: 0x0000,
            554: 3200,
            535: 0xFFFF,
            536: 0xFF9C,
            2013: 0x0000,
            2014: 125,
        },
    )
    assert info["total_grid_import"] == approx(10000.0)
    assert info["pv_power"] == 3200
    assert info["grid_power"] == -100
    assert info["total_battery_discharge"] == approx(12.5)


def test_hybrid_signed_battery_values_and_scaling() -> None:
    info: dict[str, int | float | str] = _decode(
        HYBRID,
        Registry_Type.INPUT,
        {
            2007: 0xFFFF,
            2008: 0xFF38,
            2005: 0xFF9C,
            2002: 87,
            2004: 5120,
            2001: 253,
            507: 2301,
            513: 5001,
            510: 150,
        },
    )
    assert info["battery_power"] == -200
    assert info["battery_current"] == approx(-1.0)
    assert info["battery_soc"] == 87
    assert info["battery_voltage"] == approx(512.0)
    assert info["battery_temperature"] == approx(25.3)
    assert info["grid_voltage"] == approx(230.1)
    assert info["grid_frequency"] == approx(50.01)
    assert info["grid_current"] == approx(1.5)


def test_hybrid_status_codes() -> None:
    info: dict[str, int | float | str] = _decode(
        HYBRID, Registry_Type.INPUT, {2000: 5, 2500: 6}
    )
    assert info["battery_status_desc"] == "Discharging"
    assert info["running_status_desc"] == "Off grid"


def test_hybrid_polls_only_documented_windows() -> None:
    protocol: protocol_settings = _load(HYBRID)
    ranges: list[tuple[int, int]] = protocol.calculate_registry_ranges(
        protocol.get_registry_map(Registry_Type.INPUT), 3000, init=True
    )
    assert ranges[0] == (507, 7)
    assert (2000, 15) in ranges and (2500, 16) in ranges
    assert not any(start < 500 for start, _ in ranges)


def test_t4_input_scaling_and_32_bit_values() -> None:
    info: dict[str, int | float | str] = _decode(
        T4,
        Registry_Type.INPUT,
        {
            1: 2301,
            4: 1234,
            8: 560,
            11: 501,
            12: 456,
            14: 0,
            15: 1500,
            16: 0,
            17: 2400,
            18: 0,
            19: 3600,
            20: 1,
            21: 0x86A0,
        },
    )
    assert info["grid_voltage_l1"] == approx(230.1)
    assert info["grid_current_l1"] == approx(12.34), (
        "the working Solarman definition uses 0.01 A, not the PDF's 0.1 A"
    )
    assert info["pv1_current"] == approx(5.6)
    assert info["grid_frequency"] == approx(50.1)
    assert info["module_temperature"] == approx(45.6)
    assert info["day_energy"] == approx(1.5)
    assert info["output_power"] == 2400
    assert info["day_run_time"] == 3600
    assert info["total_energy"] == approx(100.0)


def test_t4_state_and_fault_flags() -> None:
    info: dict[str, int | float | str] = _decode(
        T4, Registry_Type.INPUT, {0: 0x0700, 22: 0x0001, 24: 0x8000, 26: 0x0800}
    )
    assert info["state"] == "Inverter working,Generating on grid,Grid normal"
    assert info["fault_e01"] == "IntFaultE"
    assert info["fault_e03"] == "IntProtectO"
    assert info["fault_e05"] == "EepromErr"


def test_t4_static_registers_are_startup_rows_and_not_polled() -> None:
    protocol: protocol_settings = _load(T4)
    names: set[str] = {
        e.variable_name for e in startup_entries(protocol, Registry_Type.HOLDING)
    }
    assert {
        "dsp_version",
        "hmi_version",
        "serial_number",
        "grid_regulation",
        "communication_address",
        "language",
        "grid_connect_voltage_low",
        "grid_connect_frequency_high",
    } <= names
    assert not {"inverter_time_year", "inverter_time_second"} & names, (
        "the clock is polled, not read once"
    )
    polled: list[tuple[int, int]] = protocol.calculate_registry_ranges(
        protocol.get_registry_map(Registry_Type.HOLDING), 100, init=True
    )
    assert polled == [(7, 3)]


def test_t4_holding_decoding() -> None:
    info: dict[str, int | float | str] = _decode(
        T4,
        Registry_Type.HOLDING,
        {
            0: 0x00C9,
            1: 0x0065,
            2: 0x1234,
            3: 0x5678,
            4: 0x9012,
            5: 0x3450,
            6: 5,
            7: 0x1A0B,
            8: 0x1C0D,
            9: 0x1E1F,
            10: 3,
            11: 1,
            12: 1800,
            13: 2650,
            14: 4700,
            15: 5200,
        },
    )
    assert info["dsp_version"] == 201 and info["hmi_version"] == 101
    assert info["serial_number"] == "1234567890123450"
    assert info["grid_regulation_desc"] == "VDE-0126"
    assert (
        info["inverter_time_year"],
        info["inverter_time_month"],
        info["inverter_time_day"],
    ) == (2026, 11, 28)
    assert (
        info["inverter_time_hour"],
        info["inverter_time_minute"],
        info["inverter_time_second"],
    ) == (13, 30, 31)
    assert info["grid_connect_voltage_low"] == approx(180.0)
    assert info["grid_connect_frequency_high"] == approx(52.0)


def test_grid_regulation_table_has_no_duplicate_codes() -> None:
    codes: dict[str, str] = json.loads(
        (PROTOCOL_DIR / f"{T4}.json").read_text(encoding="utf-8")
    )["grid_regulation_codes"]
    assert codes["30"] == "Malaysia" and codes["29"] == "Austria"
    assert len(codes) == 44
