"""Tests for abb_fimer_vsn300_sunspec and abb_fimer_react2_sunspec, which scale with the Context adjustment.

The VSN300 values are registers captured from a real VSN300 (firmware 2.0.1) in front of a PVI-10.0-OUTD;
the REACT2 layout comes from FIMER's SunSpec PICS (Rev 003).  SunSpec scale factors are read from the
register beside each value (10**SF, SF -10..10) so a changed scale factor is followed automatically.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path
from typing import Any, cast

import pytest

from classes.protocol_settings import (
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)

VSN: str = "abb_fimer_vsn300_sunspec"
REACT2: str = "abb_fimer_react2_sunspec"


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _entries(name: str) -> dict[str, registry_map_entry]:
    return {e.variable_name: e for e in _load(name).registry_map[Registry_Type.HOLDING]}


def _context(entry: registry_map_entry) -> dict[str, Any] | None:
    """The entry's Context adjustment (``{"key": ..., "cases": {...}}``), or None."""
    raw: object = entry.adjustments.get("Context")
    return cast("dict[str, Any]", raw) if isinstance(raw, dict) else None


def _decode(name: str, regs: dict[int, int], variables: list[str]) -> dict[str, float | int | str]:
    proto: protocol_settings = _load(name)
    by_name: dict[str, registry_map_entry] = {e.variable_name: e for e in proto.registry_map[Registry_Type.HOLDING]}
    wanted: list[registry_map_entry] = []
    for variable in variables:
        wanted.extend(e for e in (by_name[variable], by_name.get(f"{variable}_desc")) if e is not None)
        context: dict[str, Any] | None = _context(by_name[variable])
        if context is not None:
            wanted.append(by_name[str(context["key"])])
    return proto.process_registery(regs, wanted)


def _s16(value: int) -> int:
    return value & 0xFFFF


# Registers from the real capture (PVI-10.0-OUTD behind a VSN300, firmware 2.0.1)
CAPTURE: dict[int, int] = {
    72: 376, 76: _s16(-2),                          # A, A_SF
    77: 4288, 80: 2429, 83: _s16(-1),               # PPVphAB, PhVphA, V_SF
    84: 3076, 85: 0, 86: 5002, 87: _s16(-2),        # W, W_SF, Hz, Hz_SF
    94: 1753, 95: 18992, 96: 0,                     # WH (acc32), WH_SF
    101: 3172, 102: 0,                              # DCW, DCW_SF
    103: 2482, 106: 486, 107: _s16(-1),             # TmpCab, TmpOt, Tmp_SF
    108: 4,                                         # St = MPPT
    124: _s16(-2), 125: _s16(-1), 126: 0,           # model 160 DCA_SF, DCV_SF, DCW_SF
    141: 548, 142: 3087, 143: 1685,                 # module 1
    161: 548, 162: 2755, 163: 1487,                 # module 2
    175: 10000, 176: 0,                             # WRtg, WRtg_SF
    237: 100, 239: 60, 240: 60, 241: 0, 255: 0,     # model 123
}


def test_real_vsn300_capture_decodes_to_sensible_values() -> None:
    out = _decode(VSN, CAPTURE, [
        "ac_current", "ac_voltage_an", "ac_voltage_ab", "ac_power", "ac_frequency", "ac_energy", "dc_power", "other_temperature",
        "mppt1_current", "mppt1_voltage", "mppt1_power", "mppt2_voltage", "mppt2_power", "rated_power", "operating_state",
        "power_limit_percent", "power_limit_enable",
    ])
    expected: dict[str, float] = {
        "ac_current": 3.76, "ac_voltage_an": 242.9, "ac_voltage_ab": 428.8, "ac_power": 3076, "ac_frequency": 50.02,
        "ac_energy": 114_903_600, "dc_power": 3172, "other_temperature": 48.6, "mppt1_current": 5.48, "mppt1_voltage": 308.7,
        "mppt1_power": 1685, "mppt2_voltage": 275.5, "mppt2_power": 1487, "rated_power": 10000, "power_limit_percent": 100,
    }
    for name, value in expected.items():
        assert math.isclose(float(out[name]), value, rel_tol=1e-9), name
    assert out["operating_state_desc"] == "MPPT"
    assert out["power_limit_enable"] == 0


def test_the_two_mppt_inputs_add_up_to_the_dc_power() -> None:
    """A cross-check that needs no reference: 1685 W + 1487 W is the 3172 W the inverter reports."""
    out: dict[str, float | int | str] = _decode(VSN, CAPTURE, ["mppt1_power", "mppt2_power", "dc_power"])
    assert float(out["mppt1_power"]) + float(out["mppt2_power"]) == float(out["dc_power"])


@pytest.mark.parametrize(
    ("scale_factor", "expected"),
    [(-2, 3.76), (-1, 37.6), (0, 376), (1, 3760), (2, 37600), (-3, 0.376)],
)
def test_scale_factor_is_followed_dynamically(scale_factor: int, expected: float) -> None:
    """A different scale factor register gives a different physical value for the same raw reading."""
    regs: dict[int, int] = {**CAPTURE, 76: _s16(scale_factor)}
    assert math.isclose(float(_decode(VSN, regs, ["ac_current"])["ac_current"]), expected, rel_tol=1e-9)


def test_unimplemented_scale_factor_leaves_the_raw_value() -> None:
    """SunSpec marks a missing scale factor 0x8000; the reading is then published unscaled instead of guessed."""
    assert _decode(VSN, {**CAPTURE, 76: 0x8000}, ["ac_current"])["ac_current"] == 376


@pytest.mark.parametrize("name", [VSN, REACT2])
def test_every_context_key_is_a_scale_factor_in_the_same_map(name: str) -> None:
    entries: dict[str, registry_map_entry] = _entries(name)
    scaled: list[registry_map_entry] = [e for e in entries.values() if _context(e) is not None]
    assert scaled
    for entry in scaled:
        context: dict[str, Any] = cast("dict[str, Any]", _context(entry))
        key: str = str(context["key"])
        assert key in entries and entries[key].variable_name.endswith("_sf"), entry.variable_name
        assert context["range"] == [-10, 10]     # the SunSpec scale-factor range, as a Context range instead of 21 cases


@pytest.mark.parametrize("name", [VSN, REACT2])
def test_writable_points_are_raw_not_scaled(name: str) -> None:
    """Context scaling cannot be inverted on a write, so no writable entry carries it."""
    entries: list[registry_map_entry] = [e for e in _entries(name).values() if e.write_mode is WriteMode.WRITE]
    assert entries
    assert not any("Context" in e.adjustments for e in entries)


@pytest.mark.parametrize("name", [VSN, REACT2])
def test_transport_is_modbus_tcp(name: str) -> None:
    assert _load(name).transport == "modbus_tcp"


# --------------------------------------------------------------------------
# VSN300 specifics
# --------------------------------------------------------------------------

def test_vsn300_layout_matches_the_captured_model_chain() -> None:
    entries: dict[str, registry_map_entry] = _entries(VSN)
    assert [entries[v].register for v in ("inverter_did", "mppt_did", "nameplate_did", "control_did")] == [70, 122, 172, 232]
    assert entries["power_limit_enable"].write_mode is WriteMode.WRITE
    assert "WARNING" in entries["power_limit_enable"].note and "exception 7" in entries["power_limit_enable"].note


def test_vsn300_omits_points_the_captured_inverter_reports_as_unimplemented() -> None:
    entries: dict[str, registry_map_entry] = _entries(VSN)
    assert not {"apparent_power", "reactive_power", "power_factor", "dc_current", "dc_voltage"} & entries.keys()


# --------------------------------------------------------------------------
# REACT2 specifics (from the PICS)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("variable", "register"),
    [
        ("common_id", 40002), ("inverter_a", 40072), ("inverter_w", 40084), ("inverter_wh", 40094),
        ("mppt1_dcv", 41124),
        ("meter_w", 41278), ("meter_hz", 41291), ("battery_soc", 41375), ("battery_w", 41376),
        ("battery1_bv", 41444), ("battery2_bv", 41542), ("battery3_bv", 41640),
    ],
)
def test_react2_addresses_come_from_the_pics(variable: str, register: int) -> None:
    assert _entries(REACT2)[variable].register == register


def test_react2_decodes_scaled_hybrid_values() -> None:
    entries: dict[str, registry_map_entry] = _entries(REACT2)
    regs: dict[int, int] = {
        entries["inverter_a"].register: 53, entries["inverter_a_sf"].register: _s16(-1),
        entries["inverter_w"].register: 1500, entries["inverter_w_sf"].register: 1,
        entries["battery_soc"].register: 87, entries["battery_soc_sf"].register: 0,
        entries["battery_w"].register: _s16(-2300), entries["battery_w_sf"].register: 0,
        entries["battery1_bv"].register: 512, entries["battery_v_sf"].register: _s16(-1),
        entries["meter_w"].register: _s16(-800), entries["meter_w_sf"].register: 1,
    }
    out: dict[str, float | int | str] = _decode(REACT2, regs, ["inverter_a", "inverter_w", "battery_soc", "battery_w", "battery1_bv", "meter_w"])
    assert math.isclose(float(out["inverter_a"]), 5.3) and out["inverter_w"] == 15000
    assert out["battery_soc"] == 87 and out["battery_w"] == -2300
    assert math.isclose(float(out["battery1_bv"]), 51.2) and out["meter_w"] == -8000


def test_react2_has_three_battery_units_with_lifetime_energy() -> None:
    entries: dict[str, registry_map_entry] = _entries(REACT2)
    for unit in (1, 2, 3):
        assert entries[f"battery{unit}_soh"].register == 41452 + 98 * (unit - 1)
    assert entries["battery1_whch"].data_type.name == "UINT64"


def test_react2_omits_points_the_pics_marks_unimplemented() -> None:
    entries: dict[str, registry_map_entry] = _entries(REACT2)
    assert "inverter_aphb" not in entries and "inverter_ppvphab" not in entries     # single-phase


def test_react2_json_codes_are_valid_json() -> None:
    proto: protocol_settings = _load(REACT2)
    assert proto.codes and all(isinstance(v, dict) for k, v in proto.codes.items() if k.endswith("_codes"))
    json.dumps({k: v for k, v in proto.codes.items() if k.endswith("_codes")})


# --------------------------------------------------------------------------
# Behaviors documented in protocols.md
# --------------------------------------------------------------------------

def _tiny_protocol(tmp_path: Path, rows: list[list[str]]) -> protocol_settings:
    header: list[str] = ["register", "variable_name", "documented_name", "unit", "data_type", "values", "read_interval", "writable", "adjustments", "note"]
    with (tmp_path / "probe.holding_registry_map.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    (tmp_path / "probe.json").write_text(json.dumps({"transport": "modbus_tcp"}), encoding="utf-8")
    return protocol_settings("probe", settings_dir=str(tmp_path))


_CASES: str = json.dumps({"Context": {"key": "sf", "cases": {"-2": "x/100", "0": "x"}}})


@pytest.mark.parametrize("sf_first", [True, False])
def test_row_order_does_not_matter(tmp_path: Path, sf_first: bool) -> None:
    """The scale-factor row may come before or after the row it scales."""
    value_row: list[str] = ["1", "val", "val", "", "USHORT", "", "", "R", _CASES, ""]
    sf_row: list[str] = ["2", "sf", "sf", "", "SHORT", "", "", "R", "", ""]
    proto: protocol_settings = _tiny_protocol(tmp_path, [sf_row, value_row] if sf_first else [value_row, sf_row])
    out: dict[str, int | float | str] = proto.process_registery({1: 1250, 2: _s16(-2)}, proto.registry_map[Registry_Type.HOLDING])
    assert math.isclose(float(out["val"]), 12.5)


def test_value_is_raw_when_the_key_register_was_not_read(tmp_path: Path) -> None:
    proto: protocol_settings = _tiny_protocol(tmp_path, [["1", "val", "val", "", "USHORT", "", "", "R", _CASES, ""], ["2", "sf", "sf", "", "SHORT", "", "", "R", "", ""]])
    only_value: list[registry_map_entry] = [e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == "val"]
    assert proto.process_registery({1: 1250}, only_value)["val"] == 1250


def test_default_formula_applies_when_no_case_matches(tmp_path: Path) -> None:
    with_default: str = json.dumps({"Context": {"key": "sf", "cases": {"-2": "x/100"}, "default": "x*2"}})
    proto: protocol_settings = _tiny_protocol(tmp_path, [["1", "val", "val", "", "USHORT", "", "", "R", with_default, ""], ["2", "sf", "sf", "", "SHORT", "", "", "R", "", ""]])
    out: dict[str, int | float | str] = proto.process_registery({1: 10, 2: 7}, proto.registry_map[Registry_Type.HOLDING])
    assert out["val"] == 20
