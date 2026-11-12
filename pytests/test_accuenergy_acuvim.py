"""Accuenergy Acuvim II (``accuenergy_acuvim``) protocol map checks.

Accuenergy documents every multi-register value (IEEE-754 floats and 32-bit
``Dword`` energies) high word first with big-endian bytes inside each register,
which is the app's default word order (``big_endian-ABCD``).  These tests decode
worked values through the real map with *no* ``Register_Endian`` adjustment, check
that the energy rows follow the meter's Energy Display Mode, and pin the register
addresses where the manufacturer's PDF has typos (the hex address is authoritative).
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import struct
from pathlib import Path
from typing import cast

import pytest

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

PROTOCOL: str = "accuenergy_acuvim"
PROTOCOLS_DIR: Path = Path(__file__).resolve().parent.parent / "protocols"


def _load() -> protocol_settings:
    """Load the Accuenergy protocol with logging silenced."""
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(PROTOCOL)
    finally:
        logging.disable(logging.NOTSET)


def _entries(protocol: protocol_settings, registry_type: Registry_Type = Registry_Type.HOLDING) -> dict[str, registry_map_entry]:
    """Return the registry entries of ``registry_type`` keyed by variable name."""
    return {entry.variable_name: entry for entry in protocol.registry_map[registry_type]}


def _decode(protocol: protocol_settings, raw: dict[str, bytes]) -> dict[str, float | int | str]:
    """Decode ``raw`` (variable name -> whole value, high register first) in one pass."""
    entries: dict[str, registry_map_entry] = _entries(protocol)
    selected: list[registry_map_entry] = [entries[name] for name in raw]
    registry: dict[int, bytes] = {entries[name].register: value for name, value in raw.items()}
    return protocol.process_registery(registry, selected)


def _float(value: float) -> bytes:
    """Return ``value`` as big-endian IEEE-754 single precision (high register first)."""
    return struct.pack(">f", value)


def test_json_defaults() -> None:
    """The descriptor ships the manufacturer's serial default and reads holding registers only."""
    protocol: protocol_settings = _load()
    assert protocol.settings.get("baud") == "19200"
    assert protocol.settings.get("byteorder", "big") == "big"
    assert protocol.settings.get("send_holding_register") == "true"
    assert protocol.settings.get("send_input_register") == "false"


def test_no_endian_overrides() -> None:
    """No row overrides the ABCD default; Accuenergy sends high word first everywhere."""
    protocol: protocol_settings = _load()
    overridden: list[str] = [
        entry.variable_name
        for registry_type in (Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE)
        for entry in protocol.registry_map[registry_type]
        if "Register_Endian" in entry.adjustments
    ]
    assert overridden == []


def test_variable_names_unique() -> None:
    """Variable names are the database/MQTT keys, so they must be unique within a map."""
    protocol: protocol_settings = _load()
    names: list[str] = [entry.variable_name for entry in protocol.registry_map[Registry_Type.HOLDING]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("variable", "address"),
    [
        ("frequency", 0x4000),
        ("voltage_l1", 0x4002),
        ("power_total", 0x4022),
        ("power_factor_total", 0x403A),
        ("power_demand", 0x4042),
        ("energy_import", 0x4048),
        ("apparent_energy", 0x4058),
        ("thd_v1", 0x405A),
        ("current_demand_l1", 0x4600),
        ("energy_import_l1", 0x4620),
        ("phase_angle_v2_v1", 0x42A0),
        ("run_time", 0x1034),
        ("energy_display_mode", 0x1019),
        ("basic_parameter_mode", 0x101D),
        # The manufacturer's PDF prints 425DH for V2's 45th harmonic; 452DH continues the 452xH run.
        ("v2_harmonic_45", 0x452D),
        ("v1_harmonic_2", 0x4062),
        ("v1_harmonic_31", 0x407F),
        ("v1_harmonic_32", 0x4500),
        ("v1_harmonic_63", 0x451F),
        ("i3_harmonic_63", 0x45BF),
        ("k_factor_i3", 0x412A),
    ],
)
def test_register_addresses(variable: str, address: int) -> None:
    """Spot-check wire addresses against the manufacturer's Address(H) column."""
    entry: registry_map_entry = _entries(_load())[variable]
    assert entry.register == address


def test_frequency_example_is_high_word_first() -> None:
    """The Acuvim II manual's float example: 0x42480000 is 50 Hz."""
    values: dict[str, float | int | str] = _decode(_load(), {"frequency": bytes.fromhex("42480000")})
    assert isinstance(values["frequency"], (int, float)) and math.isclose(float(values["frequency"]), 50.0, abs_tol=1e-6)


@pytest.mark.parametrize(("watts", "kilowatts"), [(1500.0, 1.5), (-2500.0, -2.5)])
def test_power_is_published_in_kw(watts: float, kilowatts: float) -> None:
    """Primary mode power is Rx/1000 kW, signed (negative = export)."""
    values: dict[str, float | int | str] = _decode(_load(), {"power_total": _float(watts)})
    assert isinstance(values["power_total"], (int, float)) and math.isclose(float(values["power_total"]), kilowatts, abs_tol=1e-6)


def test_unbalance_is_percent() -> None:
    """Unbalance is documented as Rx*100 %, with Rx a 0-1 ratio."""
    values: dict[str, float | int | str] = _decode(_load(), {"voltage_unbalance": _float(0.02)})
    assert isinstance(values["voltage_unbalance"], (int, float)) and math.isclose(float(values["voltage_unbalance"]), 2.0, abs_tol=1e-4)


@pytest.mark.parametrize(
    ("mode", "raw", "expected"),
    [
        (0, 1000, 100.0),       # Primary energy display mode: Rx/10
        (1, 100000, 100.0),     # Secondary energy display mode: Rx/1000
    ],
)
def test_energy_follows_energy_display_mode(mode: int, raw: int, expected: float) -> None:
    """Energy is Rx/10 in Primary mode and Rx/1000 in Secondary mode (register 0x1019)."""
    values: dict[str, float | int | str] = _decode(
        _load(),
        {"energy_display_mode": mode.to_bytes(2, "big"), "energy_import": raw.to_bytes(4, "big")},
    )
    assert isinstance(values["energy_import"], (int, float)) and math.isclose(float(values["energy_import"]), expected, abs_tol=1e-6)


def test_net_energy_is_signed() -> None:
    """Ep_net is documented as +/-999999999, so it decodes as a signed 32-bit integer."""
    values: dict[str, float | int | str] = _decode(
        _load(),
        {"energy_display_mode": (0).to_bytes(2, "big"), "energy_net": (-500).to_bytes(4, "big", signed=True)},
    )
    assert isinstance(values["energy_net"], (int, float)) and math.isclose(float(values["energy_net"]), -50.0, abs_tol=1e-6)


def test_run_time_is_hundredths_of_an_hour() -> None:
    """Run time T=Rx/100 hours, 32-bit high word first (0x1034 high, 0x1035 low)."""
    values: dict[str, float | int | str] = _decode(_load(), {"run_time": (1234).to_bytes(4, "big")})
    assert isinstance(values["run_time"], (int, float)) and math.isclose(float(values["run_time"]), 12.34, abs_tol=1e-6)


def test_harmonics_and_optional_io_are_read_disabled() -> None:
    """Per-order harmonics, Max/Min, TOU and I/O-module rows are not polled until enabled."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    disabled: list[str] = [
        name
        for name in entries
        if "_harmonic_" in name or name.startswith(("max_", "min_", "tou_", "di", "ai", "ao"))
    ]
    assert disabled
    for name in disabled:
        if name.endswith("_desc"):
            continue
        assert entries[name].write_mode.name == "READDISABLED", name


def _scaled(raw: dict[str, bytes], **settings: int) -> dict[str, float | int | str]:
    """Decode ``raw`` together with the 16-bit setting rows named in ``settings`` (e.g. ``pt1=4160``)."""
    protocol: protocol_settings = _load()
    values: dict[str, bytes] = dict(raw)
    for name, number in settings.items():
        width: int = 4 if name == "pt1" else 2
        values[name] = number.to_bytes(width, "big")
    return _decode(protocol, values)


def test_primary_mode_values_are_not_rescaled() -> None:
    """Basic Parameter Mode 1 (Primary): voltage, current and power are published as read."""
    out: dict[str, float | int | str] = _scaled(
        {"voltage_l1": _float(230.0), "current_l1": _float(12.5), "power_total": _float(3000.0)},
        basic_parameter_mode=1, pt1=4160, pt2=120, ct1=200, ct2=5,
    )
    assert math.isclose(float(out["voltage_l1"]), 230.0, abs_tol=1e-3)
    assert math.isclose(float(out["current_l1"]), 12.5, abs_tol=1e-6)
    assert math.isclose(float(out["power_total"]), 3.0, abs_tol=1e-6)


def test_secondary_mode_applies_pt_and_ct_ratios() -> None:
    """Basic Parameter Mode 0 (Secondary): U=Rx*(PT1/PT2), I=Rx*(CT1/CT2), P=Rx*(PT1/PT2)*(CT1/CT2)/1000 kW."""
    out: dict[str, float | int | str] = _scaled(
        {"voltage_l1": _float(120.0), "current_l1": _float(2.5), "power_total": _float(1000.0)},
        basic_parameter_mode=0, pt1=4160, pt2=120, ct1=200, ct2=5,
    )
    assert math.isclose(float(out["voltage_l1"]), 4160.0, rel_tol=1e-6)
    assert math.isclose(float(out["current_l1"]), 100.0, rel_tol=1e-6)
    assert math.isclose(float(out["power_total"]), 1.0 * (4160 / 120) * (200 / 5), rel_tol=1e-6)


@pytest.mark.parametrize("ct2", [333, 80, 100, 200])
def test_millivolt_and_milliamp_inputs_take_ct2_as_one(ct2: int) -> None:
    """For 333mV and mA CT inputs the manufacturer takes CT2 as 1, so the current ratio is just CT1."""
    out: dict[str, float | int | str] = _scaled({"current_l1": _float(0.5)}, basic_parameter_mode=0, ct1=1000, ct2=ct2)
    assert math.isclose(float(out["current_l1"]), 500.0, rel_tol=1e-6)


def test_values_stay_raw_when_the_ratio_rows_were_not_read() -> None:
    """Without the mode/ratio rows in the pass the raw (Primary-mode) value is published."""
    out: dict[str, float | int | str] = _scaled({"voltage_l1": _float(120.0)})
    assert math.isclose(float(out["voltage_l1"]), 120.0, abs_tol=1e-3)


def test_scaling_rows_are_read_every_cycle() -> None:
    """Rows other rows scale from use the default interval (as ``frequency`` does), or most passes would publish raw values."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("basic_parameter_mode", "energy_display_mode", "pt1", "pt2", "ct1", "ct2"):
        assert entries[name].read_interval == entries["frequency"].read_interval, name
        assert entries[name].write_mode.name != "READDISABLED", name


def test_context_variables_work_in_the_alphabetical_order_the_json_editor_writes(tmp_path: Path) -> None:
    """The web JSON editor lists nested keys alphabetically, which puts power_scale before voltage_scale."""
    raw: dict[str, bytes] = {"voltage_l1": _float(120.0), "current_l1": _float(2.5), "power_total": _float(1000.0)}
    settings: dict[str, int] = {"basic_parameter_mode": 0, "pt1": 4160, "pt2": 120, "ct1": 200, "ct2": 5}
    shipped: dict[str, float | int | str] = _scaled(dict(raw), **settings)

    # A copy of the protocol whose JSON has its context_variables saved in alphabetical order.
    for source in (PROTOCOLS_DIR / "accuenergy").glob(f"{PROTOCOL}.*"):
        shutil.copy(source, tmp_path / source.name)
    json_path: Path = tmp_path / f"{PROTOCOL}.json"
    protocol_json: dict[str, object] = json.loads(json_path.read_text(encoding="utf-8"))
    definitions: dict[str, object] = cast("dict[str, object]", protocol_json["context_variables"])
    assert list(definitions) != sorted(definitions), "the shipped file is deliberately not alphabetical"
    protocol_json["context_variables"] = dict(sorted(definitions.items()))
    json_path.write_text(json.dumps(protocol_json), encoding="utf-8")

    logging.disable(logging.CRITICAL)
    try:
        reordered_protocol: protocol_settings = protocol_settings(PROTOCOL, settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)
    saved_definitions: dict[str, object] = cast("dict[str, object]", reordered_protocol.codes["context_variables"])
    assert list(saved_definitions) == sorted(definitions)

    values: dict[str, bytes] = dict(raw)
    for name, number in settings.items():
        values[name] = number.to_bytes(4 if name == "pt1" else 2, "big")
    reordered: dict[str, float | int | str] = _decode(reordered_protocol, values)
    for name in raw:
        assert math.isclose(float(reordered[name]), float(shipped[name]), rel_tol=1e-9), name
    assert math.isclose(float(reordered["power_total"]), 1.0 * (4160 / 120) * (200 / 5), rel_tol=1e-6)

