"""SunSpec maps hold wire (PDU) addresses, which are the vendor's register numbers minus 1.

SunSpec and Fronius number registers from 1 ("SunS" is register 40001) but the request address is one less
(40000), and the app sends the CSV register unchanged.  Fronius's manual: "To read register 40001, use
address 40000 ... the register address always has 1 number less than the actual register number."
The expected addresses below come from the SunSpec standard layout: common model at 40002 (66 registers),
first model header at 40069, integer inverter model offsets (A +2 ... Tmp_SF +37, St +38, Evt1 +40) and
float model offsets (A +2, W +22 ... TmpCab +40, St +48, Evt1 +50).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

HEADER: int = 40069
INTEGER_MODEL: dict[str, int] = {
    "inverter_did": 0, "inverter_length": 1, "ac_current_total": 2, "ac_current_a": 3, "ac_current_b": 4, "ac_current_c": 5, "ac_current_sf": 6,
    "ac_voltage_ab": 7, "ac_voltage_bc": 8, "ac_voltage_ca": 9, "ac_voltage_an": 10, "ac_voltage_bn": 11, "ac_voltage_cn": 12, "ac_voltage_sf": 13,
    "ac_power": 14, "ac_power_sf": 15, "ac_frequency": 16, "ac_frequency_sf": 17, "ac_apparent_power": 18, "ac_apparent_power_sf": 19,
    "ac_reactive_power": 20, "ac_reactive_power_sf": 21, "ac_power_factor": 22, "ac_power_factor_sf": 23, "ac_energy_total": 24, "ac_energy_sf": 26,
    "dc_current": 27, "dc_current_sf": 28, "dc_voltage": 29, "dc_voltage_sf": 30, "dc_power": 31, "dc_power_sf": 32, "inverter_temperature": 34,
    "temp_other_1": 35, "temp_other_2": 36, "temperature_sf": 37, "operating_state": 38, "vendor_state": 39, "alarm_1": 40, "alarm_2": 42,
    "alarm_3": 44, "alarm_4": 46,
}
FLOAT_MODEL: dict[str, int] = {
    "inverter_did": 0, "sunspec_inverter_did": 0, "inverter_block_length": 1, "ac_current_total": 2, "ac_current_a": 4, "ac_current_b": 6,
    "ac_current_c": 8, "ac_voltage_ab": 10, "ac_voltage_bc": 12, "ac_voltage_ca": 14, "ac_voltage_an": 16, "ac_voltage_bn": 18, "ac_voltage_cn": 20,
    "ac_power": 22, "ac_frequency": 24, "ac_apparent_power": 26, "ac_reactive_power": 28, "ac_power_factor": 30, "ac_energy_total": 32,
    "dc_current": 34, "dc_voltage": 36, "dc_power": 38, "inverter_temperature": 40, "operating_state": 48, "vendor_state": 49, "alarm_1": 50,
    "alarm_2": 52, "event_1": 50,
}
COMMON: dict[str, int] = {
    "sunspec_id": 0, "sunspec_common_did": 2, "manufacturer": 4, "model": 20, "model_string": 20, "options": 36, "version": 44, "serial_number": 52,
    "device_address": 68,
}
MAPS: dict[str, tuple[dict[str, int], int]] = {       # name -> (inverter model offsets, minimum anchors expected)
    "solaredge_sunspec": (INTEGER_MODEL, 45),
    "enphase_iq_gateway_sunspec": (INTEGER_MODEL, 40),
    "fronius_sunspec": (FLOAT_MODEL, 30),
    "apsystems_ecu_sunspec": (FLOAT_MODEL, 14),
}
PROTOCOLS_DIR: Path = Path(__file__).resolve().parent.parent / "protocols"


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _entries(name: str) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in _load(name).registry_map[Registry_Type.HOLDING] if not e.variable_name.endswith("_desc")}


@pytest.mark.parametrize("name", list(MAPS))
def test_rows_sit_at_the_sunspec_wire_addresses(name: str) -> None:
    layout, minimum = MAPS[name]
    entries: dict[str, registry_map_entry] = _entries(name)
    wrong: list[tuple[str, int, int]] = []
    checked: int = 0
    for variable, offset in layout.items():
        if variable in entries:
            checked += 1
            if entries[variable].register != HEADER + offset:
                wrong.append((variable, entries[variable].register, HEADER + offset))
    for variable, offset in COMMON.items():
        if variable in entries:
            checked += 1
            if entries[variable].register != 40000 + offset:
                wrong.append((variable, entries[variable].register, 40000 + offset))
    assert wrong == []
    assert checked >= minimum


def test_fronius_total_ac_power_is_read_at_40091() -> None:
    """Fronius lists it as register 40092; the address on the wire is 40091 (not 91, not 40092)."""
    ac_power: registry_map_entry = _entries("fronius_sunspec")["ac_power"]
    assert ac_power.register == 40091
    assert ac_power.data_type.name == "FLOAT32"


def test_the_sunspec_marker_is_read_at_40000() -> None:
    for name in MAPS:
        assert _entries(name)["sunspec_id"].register == 40000, name


@pytest.mark.parametrize("name", list(MAPS))
def test_the_map_starts_with_a_note_explaining_the_shift(name: str) -> None:
    path: Path = next(PROTOCOLS_DIR.glob(f"*/{name}.holding_registry_map.csv"))
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows: list[list[str]] = list(csv.reader(handle))
    assert rows[1][0].startswith("# ADDRESS SHIFT") and "minus 1" in rows[1][0]


@pytest.mark.parametrize("name", list(MAPS))
def test_no_two_sunspec_rows_overlap(name: str) -> None:
    """Rows in the SunSpec chain (40000-44999) never share a register; a skipped slot used to push the tail onto its neighbors."""
    proto: protocol_settings = _load(name)
    owner: dict[int, str] = {}
    for entry in proto.registry_map[Registry_Type.HOLDING]:
        if entry.variable_name.endswith("_desc") or not 40000 <= entry.register < 45000:
            continue
        for register in range(entry.register, entry.register + max(1, proto.entry_word_count(entry))):
            assert register not in owner or owner[register] == entry.variable_name, (register, owner.get(register), entry.variable_name)
            owner[register] = entry.variable_name


def test_rows_that_were_deliberately_left_alone_are_unchanged() -> None:
    """SolarEdge battery block (62xxx) and Enphase DER rows (704-707) are flagged unverified, not shifted."""
    assert _entries("solaredge_sunspec")["battery_1_soe"].register == 62852
    enphase: dict[str, registry_map_entry] = _entries("enphase_iq_gateway_sunspec")
    assert (enphase["der_control_did"].register, enphase["der_power_limit_pct"].register, enphase["der_power_limit_enabled"].register) == (704, 706, 707)
