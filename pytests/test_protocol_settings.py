# Description: Unit tests for classes.protocol_settings and classes.data_adjustments.
# File: test_protocol_settings.py
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

"""Unit tests for classes.protocol_settings and classes.data_adjustments."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from classes.protocol_settings import (
    Data_Type,
    DataAdjustments,
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def make_adjustments(
    default_byteorder: str = "big",
) -> DataAdjustments:
    """Create a DataAdjustments instance for focused unit tests."""
    return DataAdjustments(
        log=logging.getLogger("test.data_adjustments"),
        default_byteorder=default_byteorder,  # type: ignore[arg-type]
    )


def make_protocol() -> protocol_settings:
    """Create a lightweight protocol_settings instance without filesystem I/O.

    Bypasses ``__init__`` via ``__new__``, so ``_adjustments`` must be wired
    up manually to reflect what ``__init__`` would normally do.
    """
    instance: protocol_settings = protocol_settings.__new__(protocol_settings)
    instance.byteorder = "big"
    instance.settings = {}
    instance.codes = {}
    instance._log = logging.getLogger("test.protocol_settings")
    instance._adjustments = DataAdjustments(instance._log, instance.byteorder)
    return instance


def make_entry(**overrides: Any) -> registry_map_entry:
    """Create a registry_map_entry with sensible defaults for focused tests."""
    return registry_map_entry(
        registry_type=overrides.get("registry_type", Registry_Type.INPUT),
        register=overrides.get("register", 0),
        register_bit=overrides.get("register_bit", -1),
        register_bit_end=overrides.get("register_bit_end", -1),
        register_byte=overrides.get("register_byte", 0),
        variable_name=overrides.get("variable_name", "voltage"),
        documented_name=overrides.get("documented_name", "voltage"),
        note=overrides.get("note", ""),
        unit=overrides.get("unit", "V"),
        unit_mod=overrides.get("unit_mod", 1.0),
        adjustments=overrides.get("adjustments", {}),
        concatenate=overrides.get("concatenate", False),
        concatenate_registers=overrides.get("concatenate_registers", []),
        values=overrides.get("values", []),
        value_regex=overrides.get("value_regex", ""),
        value_min=overrides.get("value_min", 0),
        value_max=overrides.get("value_max", 65535),
        data_type=overrides.get("data_type", Data_Type.USHORT),
        data_type_size=overrides.get("data_type_size", -1),
        read_command=overrides.get("read_command"),
        read_interval=overrides.get("read_interval", 1000),
        next_read_timestamp=overrides.get("next_read_timestamp", 0.0),
        write_mode=overrides.get("write_mode", WriteMode.READ),
        has_enum_mapping=overrides.get("has_enum_mapping", False),
        description_source=overrides.get("description_source", ""),
    )


# ===========================================================================
# Data_Type and WriteMode enum tests  (unchanged from original)
# ===========================================================================

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("uint16", Data_Type.USHORT), ("  float  ", Data_Type.FLOAT32), ("16bit", Data_Type._16BIT)],
)
def test_data_type_from_string_accepts_aliases_and_bit_names(raw: str, expected: Data_Type) -> None:
    """Happy path: Data_Type.fromString normalizes aliases, whitespace, and numeric-leading names."""
    assert Data_Type.fromString(raw) is expected


def test_data_type_from_string_returns_none_for_empty_or_unknown() -> None:
    """Edge cases: empty and unknown data type strings do not raise and return None."""
    assert Data_Type.fromString("") is None
    assert Data_Type.fromString("not-a-real-type") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("rw", WriteMode.WRITE), ("disabled", WriteMode.READDISABLED), ("", WriteMode.READ)],
)
def test_write_mode_from_string_maps_common_forms(raw: str, expected: WriteMode) -> None:
    """Happy path: WriteMode.fromString maps common user-facing values."""
    assert WriteMode.fromString(raw) is expected


def test_registry_entry_identity_uses_register_position_not_names() -> None:
    """Happy path: registry entries compare equal when they point at the same register location."""
    assert make_entry(variable_name="a") == make_entry(variable_name="b")
    assert make_entry(register=1) != make_entry(register=2)


# ===========================================================================
# DataAdjustments — direct unit tests
# ===========================================================================

class TestDataAdjustmentsInit:
    """DataAdjustments construction and defaults."""

    def test_defaults_to_big_endian(self) -> None:
        """The Modbus default byte order is 'big' when none is specified."""
        adj = DataAdjustments(log=logging.getLogger("test"))
        assert adj.default_byteorder == "big"

    def test_accepts_little_endian_default(self) -> None:
        """A protocol-level 'little' byte order is stored correctly."""
        adj = DataAdjustments(log=logging.getLogger("test"), default_byteorder="little")
        assert adj.default_byteorder == "little"


class TestParseAdjustments:
    """DataAdjustments.parse_adjustments — parsing edge cases."""

    def test_json_object_with_multiple_keys(self) -> None:
        adj: DataAdjustments = make_adjustments()
        result = adj.parse_adjustments('{"Offset": -50, "Register_Endian": "little"}')
        assert result == {"Offset": -50, "Register_Endian": "little"}

    def test_shorthand_with_integer_value(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.parse_adjustments("Offset:-10") == {"Offset": -10}

    def test_shorthand_with_string_value(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.parse_adjustments("Register_Endian:little") == {"Register_Endian": "little"}

    def test_empty_string_returns_empty_dict(self) -> None:
        adj = make_adjustments()
        assert adj.parse_adjustments("") == {}

    def test_none_returns_empty_dict(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.parse_adjustments(None) == {}

    def test_malformed_no_colon_returns_empty_dict(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.parse_adjustments("OffsetOnly") == {}

    def test_invalid_json_object_returns_empty_dict(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.parse_adjustments("{bad json}") == {}


class TestGetEntryByteorder:
    """DataAdjustments.get_entry_byteorder — per-entry override logic."""

    def test_returns_protocol_default_when_no_adjustments(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="big")
        entry: registry_map_entry = make_entry(adjustments={})
        assert adj.get_entry_byteorder(entry) == "big"

    def test_little_endian_protocol_default_propagates(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="little")
        entry: registry_map_entry = make_entry(adjustments={})
        assert adj.get_entry_byteorder(entry) == "little"

    def test_register_endian_little_overrides_big_default(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="big")
        entry = make_entry(adjustments={"Register_Endian": "little"})
        assert adj.get_entry_byteorder(entry) == "little"

    def test_register_endian_le_alias_accepted(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="big")
        entry: registry_map_entry = make_entry(adjustments={"Register_Endian": "le"})
        assert adj.get_entry_byteorder(entry) == "little"

    def test_register_endian_big_overrides_little_default(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="little")
        entry: registry_map_entry = make_entry(adjustments={"Register_Endian": "big"})
        assert adj.get_entry_byteorder(entry) == "big"

    def test_register_endian_be_alias_accepted(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="little")
        entry: registry_map_entry = make_entry(adjustments={"Register_Endian": "be"})
        assert adj.get_entry_byteorder(entry) == "big"

    def test_key_lookup_is_case_insensitive(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="big")
        entry: registry_map_entry = make_entry(adjustments={"register_endian": "little"})
        assert adj.get_entry_byteorder(entry) == "little"

    def test_unsupported_endian_value_falls_back_to_default(self) -> None:
        adj: DataAdjustments = make_adjustments(default_byteorder="big")
        entry: registry_map_entry = make_entry(adjustments={"Register_Endian": "middle"})
        assert adj.get_entry_byteorder(entry) == "big"


class TestApplyAdjustmentsPostDecode:
    """DataAdjustments.apply_adjustments — post_decode stage."""

    def test_unit_mod_scales_value(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(unit_mod=0.1)
        assert adj.apply_adjustments(123, entry, "post_decode") == 12.3

    def test_unit_mod_1_is_identity(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(unit_mod=1.0)
        assert adj.apply_adjustments(42, entry, "post_decode") == 42

    def test_offset_applied_after_unit_mod(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(unit_mod=0.1, adjustments={"Offset": -5})
        # 100 * 0.1 = 10.0, then 10.0 + (-5) = 5.0 → 5
        assert adj.apply_adjustments(100, entry, "post_decode") == 5

    def test_whole_float_collapses_to_int(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry = make_entry(unit_mod=0.5)
        assert adj.apply_adjustments(4, entry, "post_decode") == 2
        assert isinstance(adj.apply_adjustments(4, entry, "post_decode"), int)

    def test_fractional_float_preserved(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(unit_mod=0.1)
        result: int | float | str = adj.apply_adjustments(123, entry, "post_decode")
        assert result == pytest.approx(12.3)

    def test_string_value_returned_unchanged(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(unit_mod=0.1)
        assert adj.apply_adjustments("text", entry, "post_decode") == "text"

    def test_high_low_takes_precedence_over_unit_mod(self) -> None:
        """High_Low is the complete transform; unit_mod must not also be applied."""
        adj: DataAdjustments = make_adjustments()
        # High_Low formula: value in range (0, 10000] → x/10
        entry: registry_map_entry = make_entry(unit_mod=0.001, adjustments={"High_Low": "x?(0,10000)->x/10"})
        result: int | float | str = adj.apply_adjustments(500, entry, "post_decode")
        assert result == 50


class TestApplyAdjustmentsContext:
    """DataAdjustments.apply_adjustments — context stage."""

    def test_context_case_formula_applied(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={
            "Context": {"key": "direction", "cases": {"1": "-x", "0": "x"}}
        })
        result: int | float | str = adj.apply_adjustments(100, entry, "context", context={"direction": 1})
        assert result == -100

    def test_context_default_formula_used_when_no_case_matches(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={
            "Context": {"key": "direction", "cases": {"1": "-x"}, "default": "x"}
        })
        result: int | float | str = adj.apply_adjustments(50, entry, "context", context={"direction": 0})
        assert result == 50

    def test_context_key_missing_returns_value_unchanged(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={
            "Context": {"key": "missing_key", "cases": {"1": "-x"}}
        })
        assert adj.apply_adjustments(99, entry, "context", context={"direction": 1}) == 99

    def test_context_without_adjustments_returns_value_unchanged(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={})
        assert adj.apply_adjustments(7, entry, "context", context={"x": 1}) == 7

    def test_string_value_skipped_in_context_stage(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={"Context": {"key": "dir", "cases": {"1": "-x"}}})
        assert adj.apply_adjustments("text", entry, "context", context={"dir": 1}) == "text"


class TestApplyAdjustmentsByteorder:
    """DataAdjustments.apply_adjustments — byteorder stage."""

    def test_little_endian_adjustment_returns_little(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={"Register_Endian": "little"})
        assert adj.apply_adjustments(0, entry, "byteorder") == "little"

    def test_no_adjustments_returns_value_unchanged(self) -> None:
        adj: DataAdjustments = make_adjustments()
        entry: registry_map_entry = make_entry(adjustments={})
        assert adj.apply_adjustments(0, entry, "byteorder") == 0


class TestSafeEvalExpression:
    """DataAdjustments.safe_eval_expression — arithmetic safety."""

    def test_basic_arithmetic(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.safe_eval_expression("2 + 3 * (4 - 1)") == 11

    def test_exponentiation(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.safe_eval_expression("2 ** 10") == 1024

    def test_floor_division(self) -> None:
        adj: DataAdjustments = make_adjustments()
        assert adj.safe_eval_expression("7//2") == 3

    def test_whole_float_result_collapses_to_int(self) -> None:
        adj: DataAdjustments = make_adjustments()
        result: int | float = adj.safe_eval_expression("6.0/2.0")
        assert result == 3
        assert isinstance(result, int)

    def test_rejects_function_calls(self) -> None:
        adj: DataAdjustments = make_adjustments()
        with pytest.raises(TypeError):
            adj.safe_eval_expression("__import__('os').system('echo bad')")

    def test_rejects_string_constants(self) -> None:
        adj: DataAdjustments = make_adjustments()
        with pytest.raises(ValueError):
            adj.safe_eval_expression("'hello'")


class TestApplyRangeFormula:
    """DataAdjustments.apply_range_formula — High_Low range logic."""

    def test_value_in_range_applies_formula(self) -> None:
        adj: DataAdjustments = make_adjustments()
        result = adj.apply_range_formula(500.0, "x?(0,10000)->x/10")
        assert result == 50.0

    def test_value_outside_all_ranges_returned_unchanged(self) -> None:
        adj: DataAdjustments = make_adjustments()
        result: float = adj.apply_range_formula(-1.0, "x?(0,10000)->x/10")
        assert result == -1.0

    def test_boundary_value_at_lower_is_excluded(self) -> None:
        """Range is (lo, hi] — lower bound is exclusive."""
        adj: DataAdjustments = make_adjustments()
        result: float = adj.apply_range_formula(0.0, "x?(0,10000)->x/10")
        assert result == 0.0  # not matched → returned unchanged

    def test_boundary_value_at_upper_is_included(self) -> None:
        adj: DataAdjustments = make_adjustments()
        result: float = adj.apply_range_formula(10000.0, "x?(0,10000)->x/10")
        assert result == 1000.0


# ===========================================================================
# protocol_settings — higher-level / integration behavior (unchanged)
# ===========================================================================

def test_process_register_ushort_decodes_scaled_signed_and_string_values() -> None:
    """Happy path: ushort processing decodes numeric scaling, signed values, and text registers."""
    p: protocol_settings = make_protocol()
    scaled: registry_map_entry = make_entry(unit_mod=0.1)
    signed: registry_map_entry = make_entry(variable_name="current", documented_name="current", data_type=Data_Type.SHORT)
    ascii_entry: registry_map_entry = make_entry(variable_name="tag", documented_name="tag", data_type=Data_Type.ASCII)

    assert p.process_register_ushort({0: 123}, scaled) == 12.3
    assert p.process_register_ushort({0: 0xFFFE}, signed) == -2
    assert p.process_register_ushort({0: 0x4142}, ascii_entry) == "AB"


def test_process_register_ushort_returns_none_for_partial_multiregister_value() -> None:
    """Error handling: incomplete multi-register values return None instead of fabricating data."""
    p: protocol_settings = make_protocol()
    entry: registry_map_entry = make_entry(data_type=Data_Type.UINT)
    assert p.process_register_ushort({0: 0x1234}, entry) is None


def test_process_registery_creates_description_from_code_mapping() -> None:
    """Happy path: processed enum values get companion description fields from code mappings."""
    p: protocol_settings = make_protocol()
    source: registry_map_entry = make_entry(variable_name="mode", documented_name="mode", has_enum_mapping=True)
    desc: registry_map_entry = make_entry(
        variable_name="mode_desc",
        documented_name="mode_desc",
        description_source="mode",
        data_type=Data_Type.STRING,
    )
    p.codes = {"mode_codes": {"1": "Online"}}

    assert p.process_registery({0: 1}, [source, desc]) == {"mode": 1, "mode_desc": "Online"}


def test_calculate_registry_ranges_skips_disabled_entries_and_updates_timestamp() -> None:
    """Edge case: disabled/write-only entries are skipped when read ranges are calculated."""
    p: protocol_settings = make_protocol()
    p.settings = {"batch_size": "10"}
    readable: registry_map_entry = make_entry(register=2, read_interval=500)
    disabled: registry_map_entry = make_entry(register=5, write_mode=WriteMode.READDISABLED)

    assert p.calculate_registry_ranges([readable, disabled], max_register=10, timestamp=1.0) == [(2, 1)]
    assert readable.next_read_timestamp == 1500


def test_validate_registry_entry_rejects_invalid_values() -> None:
    """Error handling: validation rejects non-numeric and out-of-range values without raising."""
    p: protocol_settings = make_protocol()
    numeric: registry_map_entry = make_entry(value_min=1, value_max=10)
    assert p.validate_registry_entry(numeric, 5) == 1
    assert p.validate_registry_entry(numeric, 1000) == 0


def test_evaluate_expressions_substitutes_variables_ranges_and_math() -> None:
    """Happy path: dynamic register expressions expand variables, ranges, and arithmetic blocks."""
    p: protocol_settings = make_protocol()
    assert p.evaluate_expressions("R[cell]_[1~3]_[2+3]", {"cell": 7}) == [
        "R7_1_5",
        "R7_2_5",
        "R7_3_5",
    ]
