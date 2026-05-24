"""Unit tests for classes.protocol_settings."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)


def make_protocol() -> protocol_settings:
    """Create a lightweight protocol_settings instance without filesystem I/O."""
    instance = protocol_settings.__new__(protocol_settings)
    instance.byteorder = "big"
    instance.settings = {}
    instance.codes = {}
    instance._log = logging.getLogger("test.protocol_settings")
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


def test_parse_adjustments_handles_json_shorthand_empty_and_malformed() -> None:
    """Edge cases: adjustment parsing accepts JSON and shorthand while ignoring malformed input."""
    p = make_protocol()
    assert p.parse_adjustments('{"Offset": -50, "Register_Endian": "little"}') == {
        "Offset": -50,
        "Register_Endian": "little",
    }
    assert p.parse_adjustments("Offset:-10") == {"Offset": -10}
    assert p.parse_adjustments("") == {}
    assert p.parse_adjustments("OffsetOnly") == {}


def test_safe_eval_expression_allows_arithmetic_and_rejects_code() -> None:
    """Happy path and error handling: arithmetic works, while calls and names are rejected."""
    p = make_protocol()
    assert p.safe_eval_expression("2 + 3 * (4 - 1)") == 11
    assert p.safe_eval_expression("2 ** 10") == 1024
    with pytest.raises(TypeError):
        p.safe_eval_expression("__import__('os').system('echo bad')")


def test_evaluate_expressions_substitutes_variables_ranges_and_math() -> None:
    """Happy path: dynamic register expressions expand variables, ranges, and arithmetic blocks."""
    p = make_protocol()
    assert p.evaluate_expressions("R[cell]_[1~3]_[2+3]", {"cell": 7}) == [
        "R7_1_5",
        "R7_2_5",
        "R7_3_5",
    ]


def test_process_register_ushort_decodes_scaled_signed_and_string_values() -> None:
    """Happy path: ushort processing decodes numeric scaling, signed values, and text registers."""
    p = make_protocol()
    scaled = make_entry(unit_mod=0.1)
    signed = make_entry(variable_name="current", documented_name="current", data_type=Data_Type.SHORT)
    ascii_entry = make_entry(variable_name="tag", documented_name="tag", data_type=Data_Type.ASCII)

    assert p.process_register_ushort({0: 123}, scaled) == 12.3
    assert p.process_register_ushort({0: 0xFFFE}, signed) == -2
    assert p.process_register_ushort({0: 0x4142}, ascii_entry) == "AB"


def test_process_register_ushort_returns_none_for_partial_multiregister_value() -> None:
    """Error handling: incomplete multi-register values return None instead of fabricating data."""
    p = make_protocol()
    entry = make_entry(data_type=Data_Type.UINT)
    assert p.process_register_ushort({0: 0x1234}, entry) is None


def test_process_registery_creates_description_from_code_mapping() -> None:
    """Happy path: processed enum values get companion description fields from code mappings."""
    p = make_protocol()
    source = make_entry(variable_name="mode", documented_name="mode", has_enum_mapping=True)
    desc = make_entry(
        variable_name="mode_desc",
        documented_name="mode_desc",
        description_source="mode",
        data_type=Data_Type.STRING,
    )
    p.codes = {"mode_codes": {"1": "Online"}}

    assert p.process_registery({0: 1}, [source, desc]) == {"mode": 1, "mode_desc": "Online"}


def test_calculate_registry_ranges_skips_disabled_entries_and_updates_timestamp() -> None:
    """Edge case: disabled/write-only entries are skipped when read ranges are calculated."""
    p = make_protocol()
    p.settings = {"batch_size": "10"}
    readable = make_entry(register=2, read_interval=500)
    disabled = make_entry(register=5, write_mode=WriteMode.READDISABLED)

    assert p.calculate_registry_ranges([readable, disabled], max_register=10, timestamp=1.0) == [(2, 1)]
    assert readable.next_read_timestamp == 1500


def test_validate_registry_entry_rejects_invalid_values() -> None:
    """Error handling: validation rejects non-numeric and out-of-range values without raising."""
    p = make_protocol()
    numeric = make_entry(value_min=1, value_max=10)
    assert p.validate_registry_entry(numeric, 5) == 1
    assert p.validate_registry_entry(numeric, 1000) == 0

