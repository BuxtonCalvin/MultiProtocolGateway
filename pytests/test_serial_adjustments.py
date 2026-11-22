# Description: Tests that serial_base applies the protocol adjustments (Context, Offset, High_Low, endianness) and the startup values the way the Modbus transports do.
# File: test_serial_adjustments.py
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


# pyright: reportPrivateUsage=false

"""Adjustments and startup values through serial_base, with the real protocol decoder (no hardware).

A manufacturer's document often says that a raw value must be scaled, shifted or interpreted through another register.
protocol_settings does that for every transport; these tests show that a polled serial transport feeds it correctly,
in particular that a Context formula can use a row read once at startup, and that the startup values are published
as protocol_settings.static_values, as the Modbus startup routine does.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path
from typing import Any

import pytest
from test_serial_base import CountingSerial, isolated_registries, make

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

_ISOLATION: object = isolated_registries
''' Referenced on purpose: the autouse fixture only applies in a module whose namespace has it (see test_serial_vebus_write.py). '''

Row = tuple[str, str, str, str, str, str]
''' register, variable name, data type, unit, adjustments, read interval '''


def build(tmp_path: Path, rows: list[Row], extra_json: dict[str, object] | None = None) -> protocol_settings:
    header: list[str] = ["register", "variable_name", "documented_name", "unit", "data_type", "values", "read_interval", "writable", "adjustments", "note"]
    with (tmp_path / "probe.holding_registry_map.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for register, name, data_type, unit, adjustments, interval in rows:
            writer.writerow([register, name, name, unit, data_type, "", interval, "R", adjustments, ""])
    (tmp_path / "probe.json").write_text(json.dumps({"transport": "serial_base", **(extra_json or {})}), encoding="utf-8")
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings("probe", settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)


def serial_with(proto: protocol_settings, responses: dict[int, bytes]) -> Any:
    transport = make(CountingSerial)
    transport.protocolSettings = proto
    transport.responses = dict(responses)
    return transport


def u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def ctx(**spec: object) -> str:
    return json.dumps({"Context": spec})


# --------------------------------------------------------------------------- #
# Context formulas fed by startup rows                                          #
# --------------------------------------------------------------------------- #

def test_a_context_formula_can_use_a_row_read_once_at_startup(tmp_path: Path) -> None:
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", "", ctx(formula="x*{scale}"), ""), ("2", "scale", "USHORT", "", "", "startup")])
    transport = serial_with(proto, {1: u16(6), 2: u16(10)})

    assert transport.read_data()["val"] == 60
    assert transport.read_data()["val"] == 60, "still scaled on the cycles that serve the startup row from memory"
    assert transport.sent == [1, 2, 1], "the startup row went to the device once"


def test_a_context_key_may_be_a_startup_row(tmp_path: Path) -> None:
    """A mode register read once picks the formula for a polled value (a sign flip here)."""
    proto: protocol_settings = build(tmp_path, [("1", "power", "USHORT", "", ctx(key="direction", cases={"1": "-x"}, default="x"), ""), ("2", "direction", "USHORT", "", "", "startup")])
    transport = serial_with(proto, {1: u16(500), 2: u16(1)})
    assert transport.read_data()["power"] == -500

    transport.responses[2] = u16(0)
    transport.connect()  # a new connection reads the startup row again
    assert transport.read_data()["power"] == 500


def test_a_scale_factor_row_read_at_startup_drives_the_range_form(tmp_path: Path) -> None:
    """The SunSpec pattern: a scale-factor register is read once, the value is x * 10**sf."""
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", "", ctx(key="sf", range=[-3, 3]), ""), ("2", "sf", "SHORT", "", "", "startup")])
    transport = serial_with(proto, {1: u16(1250), 2: (0xFFFE).to_bytes(2, "big")})  # sf = -2
    assert math.isclose(float(transport.read_data()["val"]), 12.5)


def test_protocol_context_variables_may_use_startup_rows(tmp_path: Path) -> None:
    """A transformer ratio from two startup registers, as a protocol-level named expression."""
    proto: protocol_settings = build(
        tmp_path,
        [("1", "val", "USHORT", "", ctx(formula="x*{ratio}"), ""), ("2", "num", "USHORT", "", "", "startup"), ("3", "den", "USHORT", "", "", "startup")],
        {"context_variables": {"ratio": "{num}/{den}"}},
    )
    transport = serial_with(proto, {1: u16(6), 2: u16(200), 3: u16(5)})
    assert transport.read_data()["val"] == 240


def test_a_startup_row_that_could_not_be_read_leaves_the_context_row_raw_until_it_can(tmp_path: Path) -> None:
    """Same as the Modbus transports: no input, no guess. The next cycle retries the startup row."""
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", "", ctx(formula="x*{scale}"), ""), ("2", "scale", "USHORT", "", "", "startup")])
    transport = serial_with(proto, {1: u16(6)})  # the device does not answer for command 2 yet

    assert transport.read_data()["val"] == 6
    transport.responses[2] = u16(10)
    assert transport.read_data()["val"] == 60


# --------------------------------------------------------------------------- #
# the other adjustments                                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("data_type", "unit", "adjustments", "payload", "expected"),
    [
        ("USHORT", "0.1V", "", u16(125), 12.5),                                                   # unit multiplier
        ("USHORT", "", json.dumps({"Offset": -40}), u16(65), 25),                                 # fixed bias
        ("USHORT", "", json.dumps({"High_Low": "x?(0,65535]->(x-2731)/10"}), u16(3031), 30.0),    # tenths of a kelvin
        ("USHORT", "", json.dumps({"Register_Endian": "little_endian_byte_swap-DCBA"}), b"\x34\x12", 0x1234),
    ],
)
def test_the_per_row_adjustments_apply_to_what_a_serial_device_sends(tmp_path: Path, data_type: str, unit: str, adjustments: str, payload: bytes, expected: float) -> None:
    proto: protocol_settings = build(tmp_path, [("1", "val", data_type, unit, adjustments, "")])
    assert math.isclose(float(serial_with(proto, {1: payload}).read_data()["val"]), expected)


# --------------------------------------------------------------------------- #
# protocol_settings.static_values                                               #
# --------------------------------------------------------------------------- #

def two_startup_rows(tmp_path: Path) -> tuple[protocol_settings, Any]:
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", "", "", ""), ("2", "scale", "USHORT", "", "", "startup"), ("3", "gain", "USHORT", "", ctx(formula="x*{scale}"), "startup")])
    return proto, serial_with(proto, {1: u16(6), 2: u16(10), 3: u16(4)})


def test_startup_values_are_published_as_static_values(tmp_path: Path) -> None:
    proto, transport = two_startup_rows(tmp_path)
    transport.read_data()
    assert proto.static_values == {"scale": 10, "gain": 40}, "decoded together: the formula between two startup rows resolved; polled values are not in it"


def test_static_values_are_replaced_on_reconnect_and_removed_on_release(tmp_path: Path) -> None:
    proto, transport = two_startup_rows(tmp_path)
    transport.read_data()

    transport.connect()
    assert proto.static_values == {}, "a new connection forgets what the old one learned"
    transport.responses[2] = u16(100)
    transport.read_data()
    assert proto.static_values["scale"] == 100 and proto.static_values["gain"] == 400

    transport._release_client()
    assert proto.static_values == {}


def test_an_invalidated_startup_row_is_read_and_published_again(tmp_path: Path) -> None:
    proto, transport = two_startup_rows(tmp_path)
    transport.read_data()
    transport.responses[2] = u16(20)
    transport._invalidate_startup(2)
    assert "scale" not in proto.static_values, "the stale value is gone at once"
    transport.read_data()
    assert proto.static_values["scale"] == 20


def test_other_static_values_are_never_touched(tmp_path: Path) -> None:
    proto, transport = two_startup_rows(tmp_path)
    proto.static_values = {"from_elsewhere": 7}
    transport.read_data()
    transport._release_client()
    assert proto.static_values == {"from_elsewhere": 7}


def test_a_transport_without_startup_rows_leaves_static_values_alone(tmp_path: Path) -> None:
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", "", "", "")])
    transport = serial_with(proto, {1: u16(6)})
    transport.read_data()
    assert proto.static_values == {}


# --------------------------------------------------------------------------- #
# the reverse, for writes                                                       #
# --------------------------------------------------------------------------- #

def entry_of(tmp_path: Path, unit: str, adjustments: str) -> tuple[Any, registry_map_entry]:
    proto: protocol_settings = build(tmp_path, [("1", "val", "USHORT", unit, adjustments, "")])
    transport = serial_with(proto, {})
    return transport, proto.registry_map[Registry_Type.HOLDING][0]


@pytest.mark.parametrize(
    ("unit", "adjustments", "published", "device"),
    [
        ("", "", 42, 42),
        ("0.1A", "", 12.5, 125),                          # decoded = device * 0.1
        ("", json.dumps({"Offset": -40}), 25, 65),        # decoded = device - 40
        ("0.1V", json.dumps({"Offset": 5}), 17.5, 125),   # decoded = device * 0.1 + 5
    ],
)
def test_a_published_value_is_turned_back_into_what_the_device_holds(tmp_path: Path, unit: str, adjustments: str, published: float, device: float) -> None:
    transport, entry = entry_of(tmp_path, unit, adjustments)
    assert math.isclose(transport._to_device_value(entry, published), device)


@pytest.mark.parametrize(
    ("adjustments", "expected"),
    [
        (json.dumps({"High_Low": "x?(0,65535]->(x-2731)/10"}), "High_Low"),
        (ctx(formula="x*{scale}"), "Context"),
    ],
)
def test_a_value_that_cannot_be_reversed_is_refused_not_guessed(tmp_path: Path, adjustments: str, expected: str) -> None:
    transport, entry = entry_of(tmp_path, "", adjustments)
    with pytest.raises(ValueError, match=expected):
        transport._to_device_value(entry, 5)


@pytest.mark.parametrize("value", ["lots", float("nan"), float("inf")])
def test_a_value_that_is_not_a_finite_number_is_refused(tmp_path: Path, value: object) -> None:
    transport, entry = entry_of(tmp_path, "", "")
    with pytest.raises(ValueError, match="number"):
        transport._to_device_value(entry, value)  # type: ignore[arg-type]
