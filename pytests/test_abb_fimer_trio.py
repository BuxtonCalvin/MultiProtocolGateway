"""Tests for the ABB/FIMER TRIO native Modbus RTU protocols.

The expectations come from ABB's own register-map documents (TRIO-50/60 Gt03.0, TRIO-20/27.6 GT05,
TRIO-5.8/8.5 GB00).  ABB's tables use 1-based Modbus Data Addresses; the wire (PDU) address is one
less and the app sends the CSV value unchanged, so CSV registers are table address minus 1.  The
50/60 document defines its floats as word swapped (low register first) and gives worked examples
that are reproduced here.
"""

from __future__ import annotations

import logging
import math

import pytest

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)

ALL: list[str] = ["abb_fimer_trio_50_60", "abb_fimer_trio_tm_50_60", "abb_fimer_trio_20_27", "abb_fimer_trio_5_8_8_5"]


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _by_name(proto: protocol_settings, registry_type: Registry_Type) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in proto.registry_map[registry_type]}


def _decode(name: str, registry_type: Registry_Type, regs: dict[int, int]) -> dict[str, float | int | str]:
    proto: protocol_settings = _load(name)
    return proto.process_registery(regs, proto.registry_map[registry_type])


@pytest.mark.parametrize("name", ALL)
def test_declared_as_modbus_rtu(name: str) -> None:
    assert _load(name).transport == "modbus_rtu"


@pytest.mark.parametrize(
    ("name", "variable", "register"),
    [
        ("abb_fimer_trio_50_60", "global_state", 1049),       # ABB table 1050 (PDU 0x0419)
        ("abb_fimer_trio_50_60", "total_energy", 1071),       # table 1072 (PDU 0x042F)
        ("abb_fimer_trio_50_60", "output_power", 1093),       # table 1094 (PDU 0x0445)
        ("abb_fimer_trio_50_60", "serial_number", 1008),      # table 1009 (PDU 0x03F0)
        ("abb_fimer_trio_20_27", "global_state", 123),        # table 124
        ("abb_fimer_trio_20_27", "grid_power", 147),          # table 148
        ("abb_fimer_trio_5_8_8_5", "total_energy", 133),      # table 134
    ],
)
def test_input_register_addresses_are_table_minus_one(name: str, variable: str, register: int) -> None:
    proto: protocol_settings = _load(name)
    # A range (such as a serial number) has one entry per register under one name: compare its first register.
    first: int = min(e.register for e in proto.registry_map[Registry_Type.INPUT] if e.variable_name == variable)
    assert first == register


@pytest.mark.parametrize("name", ["abb_fimer_trio_50_60", "abb_fimer_trio_20_27", "abb_fimer_trio_5_8_8_5"])
def test_heartbeat_is_a_holding_register(name: str) -> None:
    assert _by_name(_load(name), Registry_Type.HOLDING)["heartbeat"].register == 299


# --------------------------------------------------------------------------
# ABB's worked examples (TRIO-50/60 document)
# --------------------------------------------------------------------------

def test_worked_examples_50_60() -> None:
    out = _decode("abb_fimer_trio_50_60", Registry_Type.INPUT, {
        1049: 0x0006,                        # global state 6 = Run
        1071: 0x0002, 1072: 0x55A8,          # total energy 0x000255A8 = 153000 (U32, high register first)
        1093: 0x4000, 1094: 0x469C,          # output power 20000 W: word-swapped float
        1089: 0x28F6, 1090: 0x41CC,          # the document's float 25.52
    })
    assert out["global_state"] == 6 and out["global_state_desc"] == "Connected to grid"
    assert out["total_energy"] == 153000
    assert out["output_power"] == 20000
    assert math.isclose(float(out["grid_voltage"]), 25.52, abs_tol=1e-4)


def test_ascii_strings_have_one_character_per_register() -> None:
    """ABB sends '123789' as six registers 0x0031 0x0032 ... (character in the low byte)."""
    out = _decode("abb_fimer_trio_50_60", Registry_Type.INPUT, {
        1008: 0x31, 1009: 0x32, 1010: 0x33, 1011: 0x37, 1012: 0x38, 1013: 0x39,                 # serial number
        1002: 0x2D, 1003: 0x33, 1004: 0x4E, 1005: 0x36, 1006: 0x33, 1007: 0x2D,                 # fixed part number "-3N63-"
        3649: 0x47, 3650: 0x74, 3651: 0x33, 3652: 0x30,                                         # "Gt30"
    })
    assert out["serial_number"] == "123789"
    assert out["part_number"] == "-3N63-"
    assert out["register_map_version"] == "Gt30"


def test_state_codes_and_flags() -> None:
    out = _decode("abb_fimer_trio_50_60", Registry_Type.INPUT, {1050: 38, 1053: 0b11, 1018: 78, 1021: 40, 1019: 101})
    assert out["alarm_state_desc"] == "Riso low (E025)"
    assert out["derating_state"] == "Power curtailment from user setpoint,Grid over-frequency derating"
    assert out["inverter_type_desc"] == "Photovoltaic"
    assert out["model_type_desc"] == "TRIO-50-TL-OUTD-EU"
    assert out["grid_type_desc"] == "VDE 4105"


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------

def test_tl_has_one_dc_input_and_tm_has_three() -> None:
    tl: dict[str, registry_map_entry] = _by_name(_load("abb_fimer_trio_50_60"), Registry_Type.INPUT)
    tm: dict[str, registry_map_entry] = _by_name(_load("abb_fimer_trio_tm_50_60"), Registry_Type.INPUT)
    assert not any(name.startswith(("dc2_", "dc3_")) for name in tl)
    assert {"dc2_power", "dc2_voltage", "dc2_current", "dc3_power", "dc3_voltage", "dc3_current"} <= tm.keys()
    assert (tm["dc2_power"].register, tm["dc3_current"].register) == (1113, 1133)
    assert set(tl) <= set(tm)


def test_20_27_has_fuse_board_string_currents_and_floats_for_energy() -> None:
    entries: dict[str, registry_map_entry] = _by_name(_load("abb_fimer_trio_20_27"), Registry_Type.INPUT)
    assert entries["fuse_current_1a"].register == 348 and entries["fuse_current_2e"].register == 366
    assert entries["daily_energy"].data_type is Data_Type.FLOAT32       # the GT05 document lists floats


def test_5_8_8_5_has_u32_energy_and_pmu_board() -> None:
    entries: dict[str, registry_map_entry] = _by_name(_load("abb_fimer_trio_5_8_8_5"), Registry_Type.INPUT)
    assert entries["total_energy"].data_type is Data_Type.UINT           # GB00 changed energy from float to U32
    assert "pmu_board_analog1" in entries and "fuse_current_1a" not in entries


# --------------------------------------------------------------------------
# Word order: floats are word swapped, 32-bit integers are not
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ALL)
def test_every_float_is_word_swapped_and_nothing_else_is(name: str) -> None:
    proto: protocol_settings = _load(name)
    for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):
        for entry in proto.registry_map[registry_type]:
            swapped: bool = entry.adjustments.get("Register_Endian") == "little_endian-CDAB"
            assert swapped == (entry.data_type is Data_Type.FLOAT32), entry.variable_name


# --------------------------------------------------------------------------
# ABB's read rules
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("registry_type", [Registry_Type.INPUT, Registry_Type.HOLDING])
def test_reads_start_and_end_on_mapped_registers(name: str, registry_type: Registry_Type) -> None:
    """The inverter answers a request that starts or ends on a reserved register with exception 2."""
    proto: protocol_settings = _load(name)
    mapped: set[int] = set()
    for entry in proto.registry_map[registry_type]:
        mapped.update(range(entry.register, entry.register + max(1, proto.entry_word_count(entry))))
    for start, count in proto.registry_map_ranges[registry_type]:
        assert start in mapped and start + count - 1 in mapped, (start, count)


@pytest.mark.parametrize("name", ALL)
def test_no_32bit_value_is_split_by_a_read(name: str) -> None:
    """Both registers of every float/U32 must fall inside one read range."""
    proto: protocol_settings = _load(name)
    for entry in proto.registry_map[Registry_Type.INPUT]:
        if entry.data_type in (Data_Type.FLOAT32, Data_Type.UINT):
            last: int = entry.register + 1
            assert any(s <= entry.register and s + c - 1 >= last for s, c in proto.registry_map_ranges[Registry_Type.INPUT]), entry.variable_name


@pytest.mark.parametrize("name", ALL)
def test_nothing_is_writable(name: str) -> None:
    proto: protocol_settings = _load(name)
    for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):
        assert all(e.write_mode in (WriteMode.READ, WriteMode.READDISABLED) for e in proto.registry_map[registry_type])
