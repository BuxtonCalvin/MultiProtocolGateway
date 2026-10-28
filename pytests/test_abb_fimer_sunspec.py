"""Tests for the ABB/Power-One (FIMER) native SunSpec map.

Expectations come from the vendor workbook 'ABB SunSpec Inverter Modbus Map': absolute 0-based
addresses, the fixed SunSpec scale factors (A_SF=-2, V_SF=-2, W_SF=0, Hz_SF=-2, WH_SF=0,
Tmp_SF=-1; model 160 DCA_SF=DCV_SF=-1), IEEE float32 in vendor model 64061, and the alarm
table that maps each alarm code to a bit.
"""

from __future__ import annotations

import logging
import math
import struct

import pytest

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)

NAME: str = "abb_fimer_sunspec"


@pytest.fixture(scope="module")
def proto() -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(NAME)
    finally:
        logging.disable(logging.NOTSET)


@pytest.fixture(scope="module")
def entries(proto: protocol_settings) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in proto.registry_map[Registry_Type.HOLDING]}


def _decode(proto: protocol_settings, variable: str, raw: bytes) -> float | int | str | None:
    entry: registry_map_entry = next(e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == variable)
    values: dict[str, float | int | str] = proto.process_registery({entry.register: raw}, [entry])
    return values.get(variable)


def test_declared_as_modbus_rtu(proto: protocol_settings) -> None:
    assert proto.transport == "modbus_rtu"


@pytest.mark.parametrize(
    ("variable", "address"),
    [
        ("sunspec_id", 0), ("sunspec_did", 2), ("manufacturer", 4), ("model", 20), ("options", 36),
        ("firmware_version", 44), ("serial_number", 52), ("device_address", 68),
        ("inverter_did", 70),            # common model is 66 registers long, so model 103 starts at 70
        ("ac_current", 72), ("ac_voltage_an", 80), ("ac_power", 84), ("ac_frequency", 86),
        ("ac_energy", 94), ("dc_power", 101), ("operating_state", 108), ("events", 110),
        ("mppt_did", 122), ("mppt1_current", 141), ("mppt1_voltage", 142), ("mppt2_current", 161), ("mppt2_voltage", 162),
        ("vendor_did", 172), ("device_presence", 187), ("inverter_state", 189), ("system_time", 192),
        ("alarm1", 194), ("day_energy", 210), ("ac_voltage", 222), ("dc1_power", 230), ("dc2_current", 240),
        ("isolation_resistance_1", 246), ("power_factor", 252),
    ],
)
def test_register_addresses_match_the_vendor_workbook(entries: dict[str, registry_map_entry], variable: str, address: int) -> None:
    assert entries[variable].register == address


@pytest.mark.parametrize(
    ("variable", "unit_mod"),
    [("ac_current", 0.01), ("ac_voltage_an", 0.01), ("ac_power", 1.0), ("ac_frequency", 0.01), ("ac_energy", 1.0),
     ("dc_power", 1.0), ("cabinet_temperature", 0.1), ("mppt1_current", 0.1), ("mppt2_voltage", 0.1)],
)
def test_fixed_scale_factors(entries: dict[str, registry_map_entry], variable: str, unit_mod: float) -> None:
    """ABB fixes the scale factors, so each point carries the matching constant multiplier."""
    assert math.isclose(entries[variable].unit_mod, unit_mod)


@pytest.mark.parametrize("variable", ["ac_power", "dc_power", "cabinet_temperature", "other_temperature"])
def test_signed_points_are_signed(entries: dict[str, registry_map_entry], variable: str) -> None:
    assert entries[variable].data_type is Data_Type.SHORT


def test_vendor_model_measurements_are_float32(entries: dict[str, registry_map_entry]) -> None:
    for name in ("day_energy", "ac_voltage", "dc1_current", "temperature", "power_factor"):
        assert entries[name].data_type is Data_Type.FLOAT32


@pytest.mark.parametrize(
    "omitted",
    [   # points ABB marks as unimplemented (constant 0x8000 / 0xFFFF)
        "ac_voltage_ab", "ac_voltage_bc", "ac_voltage_ca", "apparent_power", "reactive_power", "dc_current", "dc_voltage",
        "heat_sink_temperature", "transformer_temperature", "mppt1_power", "mppt1_energy",
    ],
)
def test_unimplemented_points_are_not_mapped(entries: dict[str, registry_map_entry], omitted: str) -> None:
    assert omitted not in entries


def test_decodes_scaled_ac_values(proto: protocol_settings) -> None:
    def word(value: int, signed: bool = False) -> bytes:
        return value.to_bytes(2, "big", signed=signed)

    assert math.isclose(float(_decode(proto, "ac_current", word(1250)) or 0), 12.5, abs_tol=1e-9)
    assert math.isclose(float(_decode(proto, "ac_voltage_an", word(23010)) or 0), 230.10, abs_tol=1e-9)
    assert _decode(proto, "ac_power", word(-1500, signed=True)) == -1500
    assert math.isclose(float(_decode(proto, "ac_frequency", word(4998)) or 0), 49.98, abs_tol=1e-9)
    assert math.isclose(float(_decode(proto, "cabinet_temperature", word(-125, signed=True)) or 0), -12.5, abs_tol=1e-9)
    assert math.isclose(float(_decode(proto, "mppt1_voltage", word(3921)) or 0), 392.1, abs_tol=1e-9)
    assert _decode(proto, "ac_energy", (12_345_678).to_bytes(4, "big")) == 12_345_678


def test_decodes_float32_big_endian(proto: protocol_settings) -> None:
    """Vendor model 64061 floats are high-register-first IEEE 754 (the app default word order)."""
    value = _decode(proto, "ac_voltage", struct.pack(">f", 230.1))
    assert isinstance(value, float) and math.isclose(value, 230.1, abs_tol=1e-3)


def test_strings_use_all_their_registers(proto: protocol_settings) -> None:
    """16-register manufacturer string and 8-register options string are rebuilt in full."""
    maker: bytes = "Power-One (ABB)".encode().ljust(32, b"\0")
    entry: registry_map_entry = next(e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == "manufacturer")
    words: dict[int, int] = {entry.register + i: int.from_bytes(maker[2 * i:2 * i + 2], "big") for i in range(16)}
    assert proto.process_registery(words, [entry]) == {"manufacturer": "Power-One (ABB)"}


@pytest.mark.parametrize(
    ("variable", "word", "expected"),
    [
        ("operating_state", 4, "MPPT"),
        ("operating_state", 8, "Standby"),
        ("inverter_state", 0, "Stand By"),
        ("inverter_state", 2, "Run"),
        ("global_state", 6, "Run"),
    ],
)
def test_state_enumerations(proto: protocol_settings, variable: str, word: int, expected: str) -> None:
    """The numeric state and its generated ``*_desc`` companion resolve through the JSON code tables."""
    pair: list[registry_map_entry] = [
        e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name in (variable, f"{variable}_desc")
    ]
    start: int = next(e.register for e in pair if e.variable_name == variable)
    result = proto.process_registery({start: word.to_bytes(2, "big")}, pair)
    assert result[variable] == word
    assert result[f"{variable}_desc"] == expected


@pytest.mark.parametrize(
    ("variable", "bit", "text"),
    [
        ("alarm1", 13, "Grid Fail"),     # alarm code W003
        ("alarm2", 2, "Grid UV"),        # W005, 33rd alarm: word 2, bit 2
        ("alarm3", 11, "Power reduction start"),  # W021
        ("events", 0, "Ground Fault"),   # SunSpec Evt1 bit 0
        ("events", 7, "Over Temperature"),
        ("device_presence", 1, "Comm Board present"),
    ],
)
def test_flag_bits_are_named(proto: protocol_settings, variable: str, bit: int, text: str) -> None:
    entry: registry_map_entry = next(e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == variable)
    size: int = proto.entry_word_count(entry)
    raw: bytes = (1 << bit).to_bytes(size * 2, "big")
    result = proto.process_registery({entry.register: raw}, [entry])
    assert text in str(result[variable])


def test_alarm_words_cover_the_workbook_table(entries: dict[str, registry_map_entry]) -> None:
    """Alarm1-3 are the three 32-bit words the vendor table uses; Alarm4-8 are unused and absent."""
    assert [entries[f"alarm{i}"].register for i in (1, 2, 3)] == [194, 196, 198]
    assert "alarm4" not in entries


def test_no_write_or_trio_only_registers(entries: dict[str, registry_map_entry]) -> None:
    """The control block and the TRIO-only blocks (64062, 403, 64063; 298 and up) are not in this map."""
    assert all(e.register < 254 for e in entries.values())
    assert all(e.write_mode is WriteMode.READ for name, e in entries.items() if not name.endswith("_desc"))
