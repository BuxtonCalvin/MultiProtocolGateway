"""Validation and preview of ``adjustments`` cells (the protocol editor's adjustments popup).

``DataAdjustments.validate_adjustments`` is the single source of truth for what the editor accepts:
the four adjustments MPG acts on are checked strictly (errors), anything else is allowed but flagged
(warnings).  ``preview_adjustments`` runs a cell through the real adjustment code on a sample value.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path

import pytest

from classes.protocol_settings import AdjustmentValidation, DataAdjustments

PROTOCOLS_DIR: Path = Path(__file__).resolve().parent.parent / "protocols"


def _adjustments() -> DataAdjustments:
    return DataAdjustments(logging.getLogger("test_adjustments_validation"))


def _check(text: str, known: tuple[str, ...] = (), context_vars: tuple[str, ...] = ()) -> AdjustmentValidation:
    return _adjustments().validate_adjustments(text, known, context_vars)


# ---------------------------------------------------------------- structure
def test_empty_cell_is_valid_and_stays_empty() -> None:
    assert _check("") == AdjustmentValidation(True, [], [], "")  # noqa: FBT003
    assert _check("   ").normalized == ""


@pytest.mark.parametrize("text", ["{not json", '{"Offset": 1,}', '["Offset"]', "42", "Offset"])
def test_malformed_cells_are_errors(text: str) -> None:
    result: AdjustmentValidation = _check(text)
    assert not result.ok and result.errors


def test_duplicate_keys_are_an_error() -> None:
    result: AdjustmentValidation = _check('{"Offset": 1, "Offset": 2}')
    assert not result.ok and "duplicate key 'Offset'" in result.errors[0]


def test_keys_differing_only_in_case_are_an_error() -> None:
    result: AdjustmentValidation = _check('{"offset": 1, "OFFSET": 2}')
    assert not result.ok and any("twice" in e for e in result.errors)


def test_recognized_keys_are_normalized_to_canonical_case() -> None:
    result: AdjustmentValidation = _check('{"register_endian": "little", "offset": "5"}')
    assert result.ok
    assert json.loads(result.normalized) == {"Register_Endian": "little", "Offset": 5}


def test_shorthand_is_accepted_and_normalized() -> None:
    result: AdjustmentValidation = _check("Offset:-40")
    assert result.ok and result.normalized == '{"Offset": -40}'


def test_unknown_key_is_allowed_with_a_warning() -> None:
    result: AdjustmentValidation = _check('{"Clamp": [0, 100]}')
    assert result.ok and any("not an adjustment MPG uses" in w for w in result.warnings)
    assert json.loads(result.normalized) == {"Clamp": [0, 100]}


# ---------------------------------------------------------------- Register_Endian / Offset
@pytest.mark.parametrize("value", ["big_endian-ABCD", "little_endian-CDAB", "little", "cdab", "  BE "])
def test_register_endian_accepts_names_and_aliases(value: str) -> None:
    assert _check(json.dumps({"Register_Endian": value})).ok


@pytest.mark.parametrize("value", ["middle_endian", "", 3, None])
def test_register_endian_rejects_unknown_values(value: object) -> None:
    assert not _check(json.dumps({"Register_Endian": value})).ok


@pytest.mark.parametrize("value", [5, -40, 0.5, "12", "-3.5"])
def test_offset_accepts_numbers(value: object) -> None:
    assert _check(json.dumps({"Offset": value})).ok


@pytest.mark.parametrize("value", ["abc", True, None, [1], "nan"])
def test_offset_rejects_non_numbers(value: object) -> None:
    assert not _check(json.dumps({"Offset": value})).ok


# ---------------------------------------------------------------- High_Low
def test_high_low_valid_blocks() -> None:
    assert _check('{"High_Low": "x?(0,32768]->x x?(32768,65535)->x-65536"}').ok


def test_high_low_errors() -> None:
    assert not _check('{"High_Low": "nonsense"}').ok
    assert not _check('{"High_Low": "x?(10,5]->x"}').ok                      # lower >= upper
    assert not _check('{"High_Low": "x?(0,10]->x garbage"}').ok               # text outside blocks
    assert not _check('{"High_Low": "x?(0,10]->2x"}').ok                      # implicit multiplication
    assert not _check('{"High_Low": "x?(0,10]->x*{scale}"}').ok               # names not allowed
    assert not _check('{"High_Low": 5}').ok


def test_high_low_overlap_and_offset_warnings() -> None:
    result: AdjustmentValidation = _check('{"High_Low": "x?(0,10]->x x?(5,20]->x*2", "Offset": 3}')
    assert result.ok
    assert any("overlap" in w for w in result.warnings)
    assert any("Offset is ignored" in w for w in result.warnings)


# ---------------------------------------------------------------- Context
def test_context_formula_with_variables() -> None:
    result: AdjustmentValidation = _check('{"Context": {"formula": "x*{pt1}/{pt2}"}}', known=("pt1", "pt2"))
    assert result.ok and not result.warnings


def test_context_key_cases_range_and_default() -> None:
    assert _check('{"Context": {"key": "mode", "cases": {"0": "x", "1": "-x"}, "default": "x"}}').ok
    assert _check('{"Context": {"key": "sf", "range": [-10, 10]}}').ok
    assert _check('{"Context": {"key": "sf", "range": "-3~3"}}').ok
    assert _check('{"Context": {"key": "ct2", "cases": {"333,80,100": "x*{ct1}"}, "default": "x*{ct1}/{ct2}"}}').ok


@pytest.mark.parametrize(
    "spec",
    [
        {"formula": "x*(2"},                                    # unbalanced bracket
        {"formula": "x**"},                                     # dangling operator
        {"formula": "sqrt(x)"},                                 # functions are not supported
        {"formula": "x*{pt1"},                                  # unmatched brace
        {"formula": ""},
        {"key": "mode"},                                        # key with nothing to choose
        {"key": "mode", "cases": {}},
        {"key": "mode", "cases": {"0": "x*"}},
        {"key": "sf", "range": [-100, 100]},                    # beyond +/-50
        {"key": "bad name", "cases": {"0": "x"}},
        {"cases": {"0": "x"}},                                  # no formula and no key
        {},
    ],
)
def test_context_errors(spec: dict[str, object]) -> None:
    assert not _check(json.dumps({"Context": spec})).ok


def test_context_must_be_an_object() -> None:
    assert not _check('{"Context": "x*2"}').ok


def test_context_warnings() -> None:
    unused: AdjustmentValidation = _check('{"Context": {"formula": "x", "colour": "red"}}')
    assert unused.ok and any("colour" in w for w in unused.warnings)
    ignored: AdjustmentValidation = _check('{"Context": {"key": "mode", "formula": "x", "default": "x"}}')
    assert ignored.ok and any("ignored when key is set" in w for w in ignored.warnings)
    constant: AdjustmentValidation = _check('{"Context": {"formula": "100"}}')
    assert constant.ok and any("does not use x" in w for w in constant.warnings)


def test_unknown_variable_and_key_warnings_need_a_known_list() -> None:
    spec = '{"Context": {"key": "mode", "cases": {"0": "x*{typo}"}}}'
    assert not _check(spec).warnings                                  # nothing to compare against
    result: AdjustmentValidation = _check(spec, known=("mode", "pt1"))
    assert any("{typo}" in w for w in result.warnings)
    assert not any("mode" in w and "not a variable" in w for w in result.warnings)


def test_context_variables_count_as_known() -> None:
    result: AdjustmentValidation = _check('{"Context": {"formula": "x*{voltage_scale}"}}', known=("pt1",), context_vars=("voltage_scale",))
    assert result.ok and not result.warnings


def test_template_placeholder_in_key_is_accepted() -> None:
    """SunSpec template rows use ``{i}`` in key names; the map loader expands it."""
    result: AdjustmentValidation = _check('{"Context": {"key": "meter_{i}_ac_voltage_sf", "range": [-10, 10]}}', known=("pt1",))
    assert result.ok and not result.warnings


# ---------------------------------------------------------------- existing maps keep validating
def test_every_adjustment_in_the_repo_validates() -> None:
    """Whatever ships in a protocol CSV must be acceptable to the editor, or editing that row would be refused."""
    adjustments: DataAdjustments = _adjustments()
    failures: list[str] = []
    checked: int = 0
    for path in sorted(PROTOCOLS_DIR.rglob("*.csv")):
        with path.open(newline="", encoding="latin-1") as handle:
            for row in csv.DictReader(handle):
                cell: str = (row.get("adjustments") or "").strip()
                if not cell:
                    continue
                checked += 1
                result: AdjustmentValidation = adjustments.validate_adjustments(cell)
                if not result.ok:
                    failures.append(f"{path.name}:{row.get('variable_name')}: {result.errors}")
    assert checked > 0
    assert failures == []


# ---------------------------------------------------------------- preview
def test_preview_runs_the_real_code_path() -> None:
    out: dict[str, object] = _adjustments().preview_adjustments(
        '{"Context": {"formula": "x*{pt1}/{pt2}"}}', 120.0, variables={"pt1": 4160, "pt2": 120},
    )
    assert out["ok"] is True
    assert out["post_decode"] == 120
    final: object = out["final"]
    assert isinstance(final, (int, float)) and math.isclose(float(final), 4160.0)
    assert "4160.0" in str(out["formula"]), "the formula is shown with the values substituted"
    assert out["waiting_for"] == []


def test_preview_applies_unit_multiplier_offset_and_high_low() -> None:
    adj: DataAdjustments = _adjustments()
    assert adj.preview_adjustments('{"Offset": -40}', 100.0, unit_mod=0.1)["post_decode"] == -30
    twos: dict[str, object] = adj.preview_adjustments('{"High_Low": "x?(0,32768]->x x?(32768,65535]->x-65536"}', 65535.0)
    assert twos["post_decode"] == -1


def test_preview_reports_missing_context_inputs_and_keeps_the_raw_value() -> None:
    out: dict[str, object] = _adjustments().preview_adjustments('{"Context": {"formula": "x*{pt1}/{pt2}"}}', 120.0, variables={"pt1": 4160})
    assert out["waiting_for"] == ["pt2"]
    assert out["final"] == 120


def test_preview_uses_protocol_context_variables() -> None:
    out: dict[str, object] = _adjustments().preview_adjustments(
        '{"Context": {"formula": "x*{scale}"}}', 10.0,
        variables={"num": 6, "den": 3}, context_variable_defs={"scale": "{num}/{den}"},
    )
    assert out["final"] == 20


def test_preview_refuses_an_invalid_cell() -> None:
    out: dict[str, object] = _adjustments().preview_adjustments('{"Offset": "abc"}', 1.0)
    assert out["ok"] is False and out["errors"] and out["final"] is None
