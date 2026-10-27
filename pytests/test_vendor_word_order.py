"""The Eastron, ABB and a-TroniX/Wattsonic maps need no endian adjustments.

Each vendor documents big-endian data with the high register first, which is the
app's default word order (``big_endian-ABCD``).  These tests decode the vendors' own
worked examples through the real maps with *no* ``Register_Endian`` adjustment, and
check that no entry or JSON setting overrides the default.  If the default ever
changes, or someone adds an override to one of these maps, they fail.
"""

from __future__ import annotations

import logging
import math

import pytest

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

PROTOCOLS: list[tuple[str, Registry_Type]] = [
    ("eastron_eastron_sdm120m", Registry_Type.INPUT),
    ("abb_b21", Registry_Type.HOLDING),
    ("a_tronix", Registry_Type.HOLDING),
]


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _decode(name: str, registry_type: Registry_Type, variable: str, raw: bytes) -> float | int | str | None:
    """Decode ``raw`` (the whole value, high register first) as the entry ``variable`` would be read."""
    protocol: protocol_settings = _load(name)
    entries: list[registry_map_entry] = protocol.registry_map[registry_type]
    entry: registry_map_entry = next(e for e in entries if e.variable_name == variable)
    values: dict[str, float | int | str] = protocol.process_registery({entry.register: raw}, [entry])
    return values.get(variable)


@pytest.mark.parametrize(("name", "registry_type"), PROTOCOLS, ids=[p[0] for p in PROTOCOLS])
def test_no_endian_overrides(name: str, registry_type: Registry_Type) -> None:
    """Neither the JSON (``byteorder``) nor any CSV row (``Register_Endian``) overrides the ABCD default."""
    protocol: protocol_settings = _load(name)
    assert protocol.settings.get("byteorder", "big") == "big"
    overridden: list[str] = [
        entry.variable_name
        for entry in protocol.registry_map[registry_type]
        if "Register_Endian" in entry.adjustments
    ]
    assert overridden == []


def test_eastron_float32_is_high_register_first() -> None:
    """Eastron's manual: reading Volts 1 returns bytes 43 66 33 34, which it states is 230.2 V."""
    volts = _decode("eastron_eastron_sdm120m", Registry_Type.INPUT, "voltage", bytes.fromhex("43663334"))
    assert isinstance(volts, float) and math.isclose(volts, 230.2, abs_tol=1e-3)


@pytest.mark.parametrize(
    ("variable", "raw_hex", "expected"),
    [
        ("current_l1", "00000209", 5.21),          # ABB manual: 0x00000209 = 521 -> 5.21 A
        ("voltage_l1", "000008F3", 229.1),         # ABB manual: 0x000008F3 = 2291 -> 229.1 V
        ("active_power", "0001C14D", 1150.21),     # ABB manual: 0x0001C14D = 115021 -> 1150.21 W
        ("active_power", "FFFE3EB3", -1150.21),    # same magnitude, negative (export), two's complement
    ],
)
def test_abb_32bit_values_are_big_endian(variable: str, raw_hex: str, expected: float) -> None:
    """ABB documents most-significant-byte-first; the default decode reproduces its worked examples."""
    value = _decode("abb_b21", Registry_Type.HOLDING, variable, bytes.fromhex(raw_hex))
    assert isinstance(value, (int, float)) and math.isclose(float(value), expected, abs_tol=1e-6)


def test_abb_64bit_energy_is_big_endian() -> None:
    """ABB manual example 0x0000000000112233 = 1122867 raw, scaled by the map's resolution."""
    protocol: protocol_settings = _load("abb_b21")
    entry: registry_map_entry = next(e for e in protocol.registry_map[Registry_Type.HOLDING] if e.variable_name == "active_energy_import")
    value = _decode("abb_b21", Registry_Type.HOLDING, "active_energy_import", bytes.fromhex("0000000000112233"))
    assert isinstance(value, (int, float)) and math.isclose(float(value), 1122867 * entry.unit_mod, rel_tol=1e-9)


@pytest.mark.parametrize(
    ("variable", "raw_hex", "expected"),
    [
        ("battery_power", "FFFFFA24", -1500),          # I32 -1500 W: high register 0xFFFF first
        ("meter_power_total", "000008FC", 2300),        # I32 2300 W
        ("total_pv_generation", "0001E240", 12345.6),   # U32 123456 x 0.1 kWh
        ("battery_voltage", "0200", 51.2),              # U16 512 x 0.1 V
    ],
)
def test_a_tronix_32bit_values_are_high_register_first(variable: str, raw_hex: str, expected: float) -> None:
    """Solinteg/Wattsonic: U32/I32 span two registers, higher-order register first (big-endian)."""
    value = _decode("a_tronix", Registry_Type.HOLDING, variable, bytes.fromhex(raw_hex))
    assert isinstance(value, (int, float)) and math.isclose(float(value), expected, abs_tol=1e-6)
