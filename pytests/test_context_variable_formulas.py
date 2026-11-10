"""Context adjustments may name other decoded variables as ``{name}`` and may omit ``key``.

``{"Context": {"formula": "x*{pt1}/{pt2}"}}`` scales a row by a ratio of two other rows, and a
``cases``/``default`` formula may also use ``{name}``.  A variable that was not read in the
same pass (or is not a number) leaves the raw value untouched, like a missing ``key``.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path

import pytest

from classes.protocol_settings import (
    DataAdjustments,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)


def _protocol(tmp_path: Path, rows: list[list[str]], extra_json: dict[str, object] | None = None) -> protocol_settings:
    """Build a one-off holding-register protocol from ``rows`` (register, name, data type, adjustments)."""
    header: list[str] = ["register", "variable_name", "documented_name", "unit", "data_type", "values", "read_interval", "writable", "adjustments", "note"]
    with (tmp_path / "probe.holding_registry_map.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for register, name, data_type, adjustments in rows:
            writer.writerow([register, name, name, "", data_type, "", "", "R", adjustments, ""])
    (tmp_path / "probe.json").write_text(json.dumps({"transport": "modbus_tcp", **(extra_json or {})}), encoding="utf-8")
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings("probe", settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)


def _read(proto: protocol_settings, regs: dict[int, int], names: list[str] | None = None) -> dict[str, float | int | str]:
    entries: list[registry_map_entry] = [e for e in proto.registry_map[Registry_Type.HOLDING] if names is None or e.variable_name in names]
    return proto.process_registery(regs, entries)


_RATIO: str = json.dumps({"Context": {"formula": "x*{num}/{den}"}})


def test_keyless_formula_uses_other_variables(tmp_path: Path) -> None:
    """The documented ``formula`` form needs no ``key``."""
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", _RATIO], ["2", "num", "USHORT", ""], ["3", "den", "USHORT", ""]])
    assert math.isclose(float(_read(proto, {1: 6, 2: 200, 3: 5})["val"]), 240.0)


@pytest.mark.parametrize("missing", ["num", "den"])
def test_raw_value_when_a_variable_was_not_read(tmp_path: Path, missing: str) -> None:
    """A variable absent from the pass leaves the row unscaled instead of guessing."""
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", _RATIO], ["2", "num", "USHORT", ""], ["3", "den", "USHORT", ""]])
    names: list[str] = ["val", *[n for n in ("num", "den") if n != missing]]
    regs: dict[int, int] = {1: 6, 2: 200, 3: 5}
    assert _read(proto, regs, names)["val"] == 6


def test_cases_and_default_may_use_variables(tmp_path: Path) -> None:
    """A key picks the formula, and the chosen formula may still name other variables."""
    adjustment: str = json.dumps({"Context": {"key": "mode", "cases": {"0": "x*{num}"}, "default": "x"}})
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", adjustment], ["2", "mode", "USHORT", ""], ["3", "num", "USHORT", ""]])
    assert _read(proto, {1: 3, 2: 0, 3: 10})["val"] == 30
    assert _read(proto, {1: 3, 2: 1, 3: 10})["val"] == 3


def test_negative_variable_keeps_its_sign(tmp_path: Path) -> None:
    """Values are parenthesised, so ``x - {n}`` with n = -4 is ``x - (-4)``."""
    adjustment: str = json.dumps({"Context": {"formula": "x-{n}"}})
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", adjustment], ["2", "n", "SHORT", ""]])
    assert _read(proto, {1: 10, 2: 0xFFFC})["val"] == 14


def test_zero_divisor_leaves_the_raw_value(tmp_path: Path) -> None:
    """A divide-by-zero in the formula is caught and the raw value is kept."""
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", _RATIO], ["2", "num", "USHORT", ""], ["3", "den", "USHORT", ""]])
    assert _read(proto, {1: 6, 2: 200, 3: 0})["val"] == 6


def test_existing_key_and_cases_form_is_unchanged(tmp_path: Path) -> None:
    """The SunSpec-style ``key``/``cases`` form still works without variables."""
    adjustment: str = json.dumps({"Context": {"key": "sf", "cases": {"-2": "x/100"}}})
    proto: protocol_settings = _protocol(tmp_path, [["1", "val", "USHORT", adjustment], ["2", "sf", "SHORT", ""]])
    assert math.isclose(float(_read(proto, {1: 1250, 2: 0xFFFE})["val"]), 12.5)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"a": 2, "b": 4.5}, "(2.0)*(4.5)"),
        ({"a": 2}, None),                       # b not read
        ({"a": 2, "b": "text"}, None),          # not a number
        ({"a": True, "b": 1}, None),            # booleans are not numbers
    ],
)
def test_substitute_context_variables(context: dict[str, int | float | str], expected: str | None) -> None:
    """The substitution helper returns None unless every named variable is numeric."""
    assert DataAdjustments.substitute_context_variables("{a}*{b}", context) == expected


_SCALE: str = json.dumps({"Context": {"formula": "x*{scale}"}})


def test_protocol_context_variable_is_shared_by_rows(tmp_path: Path) -> None:
    """A ``context_variables`` entry is written once in the protocol JSON and used by many rows."""
    proto: protocol_settings = _protocol(
        tmp_path,
        [["1", "a", "USHORT", _SCALE], ["2", "b", "USHORT", _SCALE], ["3", "num", "USHORT", ""], ["4", "den", "USHORT", ""]],
        {"context_variables": {"scale": "{num}/{den}"}},
    )
    out: dict[str, float | int | str] = _read(proto, {1: 6, 2: 9, 3: 200, 4: 5})
    assert out["a"] == 240
    assert out["b"] == 360
    assert "scale" not in out, "derived values are not published"


def test_context_variables_can_chain_and_select_by_key(tmp_path: Path) -> None:
    """An entry may use earlier entries, and may itself be a key/cases spec with comma-grouped keys."""
    definitions: dict[str, object] = {
        "ratio": {"key": "kind", "cases": {"3, 4,5": "{num}"}, "default": "{num}/{den}"},
        "scale": "{mode}+(1-{mode})*{ratio}",
    }
    rows: list[list[str]] = [["1", "val", "USHORT", _SCALE], ["2", "kind", "USHORT", ""], ["3", "num", "USHORT", ""], ["4", "den", "USHORT", ""], ["5", "mode", "USHORT", ""]]
    proto: protocol_settings = _protocol(tmp_path, rows, {"context_variables": definitions})
    assert _read(proto, {1: 2, 2: 4, 3: 200, 4: 5, 5: 0})["val"] == 400      # grouped key: ratio = num
    assert _read(proto, {1: 2, 2: 1, 3: 200, 4: 5, 5: 0})["val"] == 80       # default: ratio = num/den
    assert _read(proto, {1: 2, 2: 1, 3: 200, 4: 5, 5: 1})["val"] == 2        # mode 1: scale = 1


def test_row_keeps_raw_value_when_a_context_variable_input_is_missing(tmp_path: Path) -> None:
    """If an input of the shared expression was not read in the pass, the row is published raw."""
    proto: protocol_settings = _protocol(
        tmp_path,
        [["1", "a", "USHORT", _SCALE], ["2", "num", "USHORT", ""], ["3", "den", "USHORT", ""]],
        {"context_variables": {"scale": "{num}/{den}"}},
    )
    assert _read(proto, {1: 6, 2: 200}, ["a", "num"])["a"] == 6


def test_real_variable_wins_over_a_context_variable_of_the_same_name(tmp_path: Path) -> None:
    """Decoded variables take precedence, so a derived name cannot shadow a real row."""
    proto: protocol_settings = _protocol(
        tmp_path,
        [["1", "a", "USHORT", _SCALE], ["2", "scale", "USHORT", ""]],
        {"context_variables": {"scale": "100"}},
    )
    assert _read(proto, {1: 6, 2: 2})["a"] == 12


@pytest.mark.parametrize(
    ("value", "expected"),
    [(333, "grouped"), (80, "grouped"), (5, ""), ("333", "grouped"), (333.0, "grouped")],
)
def test_match_context_case_groups_values(value: int | float | str, expected: str) -> None:
    """``"333, 80"`` style keys match any listed value; unlisted values match nothing."""
    assert DataAdjustments.match_context_case({"333, 80": "grouped"}, value) == expected
