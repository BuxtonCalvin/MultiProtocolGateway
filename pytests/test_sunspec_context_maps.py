"""SolarEdge and Fronius SunSpec maps publish scaled values through the Context adjustment."""

from __future__ import annotations

import logging
import math
from typing import Any, cast

import pytest

from classes.protocol_settings import (
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _entries(name: str) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in _load(name).registry_map[Registry_Type.HOLDING]}


def _context_key(entry: registry_map_entry) -> str | None:
    raw: object = entry.adjustments.get("Context")
    return str(cast("dict[str, Any]", raw)["key"]) if isinstance(raw, dict) else None


def _s16(value: int) -> int:
    return value & 0xFFFF


@pytest.mark.parametrize("name", ["solaredge_sunspec", "fronius_sunspec"])
def test_every_context_key_is_a_scale_factor_row(name: str) -> None:
    entries: dict[str, registry_map_entry] = _entries(name)
    keys: list[str] = [k for k in (_context_key(e) for e in entries.values()) if k]
    assert keys and all(k in entries and k.endswith("_sf") for k in keys)


@pytest.mark.parametrize("name", ["solaredge_sunspec", "fronius_sunspec"])
def test_writable_rows_are_never_scaled(name: str) -> None:
    for entry in _entries(name).values():
        if entry.write_mode is WriteMode.WRITE:
            assert _context_key(entry) is None, entry.variable_name


def test_solaredge_has_no_unit_hint_left_to_apply_by_hand() -> None:
    proto: protocol_settings = _load("solaredge_sunspec")
    assert all("×SF" not in e.unit for e in proto.registry_map[Registry_Type.HOLDING])
    assert sum(1 for e in proto.registry_map[Registry_Type.HOLDING] if _context_key(e)) == 23


def test_solaredge_values_are_published_in_real_units() -> None:
    proto: protocol_settings = _load("solaredge_sunspec")
    e: dict[str, registry_map_entry] = _entries("solaredge_sunspec")
    regs: dict[int, int] = {
        e["ac_current_total"].register: 1250, e["ac_current_sf"].register: _s16(-2),
        e["ac_voltage_an"].register: 2301, e["ac_voltage_sf"].register: _s16(-1),
        e["ac_power"].register: _s16(-1500), e["ac_power_sf"].register: 0,
        e["ac_frequency"].register: 5002, e["ac_frequency_sf"].register: _s16(-2),
        e["inverter_temperature"].register: 412, e["temperature_sf"].register: _s16(-1),
        e["rated_power"].register: 5000, e["rated_power_sf"].register: 1,
    }
    out = proto.process_registery(regs, proto.registry_map[Registry_Type.HOLDING])
    assert math.isclose(float(out["ac_current_total"]), 12.5)
    assert math.isclose(float(out["ac_voltage_an"]), 230.1)
    assert out["ac_power"] == -1500
    assert math.isclose(float(out["ac_frequency"]), 50.02)
    assert math.isclose(float(out["inverter_temperature"]), 41.2)
    assert out["rated_power"] == 50000


def test_fronius_nameplate_ratings_are_scaled() -> None:
    proto: protocol_settings = _load("fronius_sunspec")
    e: dict[str, registry_map_entry] = _entries("fronius_sunspec")
    regs: dict[int, int] = {e["rated_power"].register: 5000, e["rated_power_sf"].register: _s16(-1),
                            e["rated_va"].register: 6000, e["rated_va_sf"].register: 0}
    out = proto.process_registery(regs, proto.registry_map[Registry_Type.HOLDING])
    assert math.isclose(float(out["rated_power"]), 500.0) and out["rated_va"] == 6000
    assert _context_key(e["active_power_limit_pct"]) is None        # writable: stays raw with its fixed 0.01 multiplier
