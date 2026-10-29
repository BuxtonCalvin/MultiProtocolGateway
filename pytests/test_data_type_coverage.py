"""Tests for the INT64 data type and a guard that every fixed-width numeric
``Data_Type`` is wired into every place that enumerates types.

When a numeric type is added to ``Data_Type`` it must also appear in: the width
table (``getSize``), the word-count tables, the decoders, the write encoder, the
MQTT/Home Assistant numeric set, the TimescaleDB type map and the protocol
documentation.  ``test_every_numeric_type_is_wired_everywhere`` fails if any of
those is missed, so the next new type cannot silently fall back to a default.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from classes.transports.modbus_base import modbus_base
from classes.transports.mqtt import is_numeric_entry, number_bounds
from classes.transports.timescaledb import timescaledb
from classes.transports.transport_base import TransportWriteMode

INT64_MIN: int = -(1 << 63)
INT64_MAX: int = (1 << 63) - 1

# Every fixed-width numeric type.  Add new numeric types here.
NUMERIC_TYPES: list[Data_Type] = [
    Data_Type.BYTE, Data_Type.USHORT, Data_Type.SHORT, Data_Type.UINT, Data_Type.INT,
    Data_Type.UINT64, Data_Type.INT64, Data_Type.FLOAT32, Data_Type.FLOAT64, Data_Type.ACC32,
]
MULTI_REGISTER_WORDS: dict[Data_Type, int] = {
    Data_Type.UINT: 2, Data_Type.INT: 2, Data_Type.ACC32: 2, Data_Type.FLOAT32: 2,
    Data_Type.UINT64: 4, Data_Type.INT64: 4, Data_Type.FLOAT64: 4,
}
PROTOCOLS_DOC: Path = Path(__file__).resolve().parent.parent / "documentation" / "usage" / "protocols.md"


@pytest.fixture(scope="module")
def abb() -> protocol_settings:
    """The ABB B21 protocol, whose map contains INT64 net-energy registers."""
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings("abb_b21")
    finally:
        logging.disable(logging.NOTSET)


def _net_entry(abb: protocol_settings, endian: str | None = None) -> registry_map_entry:
    entry: registry_map_entry = next(e for e in abb.registry_map[Registry_Type.HOLDING] if e.variable_name == "active_energy_net")
    if endian:
        entry = dataclasses.replace(entry, adjustments={"Register_Endian": endian})
    return entry


def _probe_entry(abb: protocol_settings, **overrides: Any) -> registry_map_entry:
    """A real, fully-populated entry (ABB net energy) with ``overrides`` applied."""
    return dataclasses.replace(_net_entry(abb), **overrides)


def _dt(name: str) -> Data_Type:
    """Look a member up by name; members such as ``_4SBIT`` are underscore-prefixed (reported as private when accessed as attributes)."""
    return Data_Type[name]


def _wire_words(value: int, order: str) -> list[bytes]:
    """Encode ``value`` as four 16-bit wire words using the given ABCD/BADC/CDAB/DCBA order."""
    words: list[bytes] = [value.to_bytes(8, "big", signed=True)[i:i + 2] for i in range(0, 8, 2)]
    if order in ("badc", "dcba"):
        words = [w[::-1] for w in words]
    if order in ("cdab", "dcba"):
        words.reverse()
    return words


# --------------------------------------------------------------------------
# Enum, aliases, widths
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INT64", Data_Type.INT64), ("int64", Data_Type.INT64), ("  S64 ", Data_Type.INT64),
        ("UINT64", Data_Type.UINT64), ("u64", Data_Type.UINT64),
        ("FLOAT64", Data_Type.FLOAT64), ("double", Data_Type.FLOAT64),
        ("INT32", Data_Type.INT), ("s32", Data_Type.INT),
        ("UINT32", Data_Type.UINT), ("u32", Data_Type.UINT),
    ],
)
def test_wide_type_names_and_aliases(raw: str, expected: Data_Type) -> None:
    """The 32/64-bit names and aliases documented in protocols.md all resolve."""
    assert Data_Type.fromString(raw) is expected


def test_int64_enum_value_is_unique() -> None:
    """INT64 gets its own value and does not alias another member."""
    assert Data_Type.INT64.value == 13
    assert len({m.value for m in Data_Type}) == len(list(Data_Type.__members__.values())) - 4  # 4 documented BIT aliases


def test_int64_size_and_word_count(abb: protocol_settings) -> None:
    """INT64 is 64 bits wide and occupies four registers."""
    assert Data_Type.getSize(Data_Type.INT64) == 64
    assert abb.entry_word_count(_net_entry(abb)) == 4


def test_int64_default_range_is_signed(abb: protocol_settings) -> None:
    """An INT64 entry with no explicit values range gets the full signed 64-bit range."""
    entry: registry_map_entry = _net_entry(abb)
    assert (entry.value_min, entry.value_max) == (INT64_MIN, INT64_MAX)


# --------------------------------------------------------------------------
# Decoding — both decode paths, all four word orders, boundary values
# --------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["abcd", "badc", "cdab", "dcba"])
@pytest.mark.parametrize("value", [0, 1, -1, -250000, 1_122_867, INT64_MIN, INT64_MAX])
def test_int64_decodes_in_every_word_order(abb: protocol_settings, order: str, value: int) -> None:
    """process_register_bytes and process_register_ushort agree and are sign-correct."""
    entry: registry_map_entry = _net_entry(abb, order)
    words: list[bytes] = _wire_words(value, order)
    as_bytes: dict[int, bytes | tuple[bytes, float]] = {entry.register: b"".join(words)}
    as_ints: dict[int, int] = {entry.register + i: int.from_bytes(w, "big") for i, w in enumerate(words)}

    raw_bytes_path = abb.process_register_bytes(as_bytes, dataclasses.replace(entry, unit_mod=1.0))
    raw_ushort_path = abb.process_register_ushort(as_ints, dataclasses.replace(entry, unit_mod=1.0))
    assert raw_bytes_path == value
    assert raw_ushort_path == value


def test_int64_partial_value_returns_none(abb: protocol_settings) -> None:
    """A truncated 64-bit read yields None rather than a fabricated number."""
    entry: registry_map_entry = _net_entry(abb)
    assert abb.process_register_ushort({entry.register: 0xFFFF, entry.register + 1: 0xFFFF}, entry) is None


def test_abb_net_equals_import_minus_export(abb: protocol_settings) -> None:
    """End to end through process_registery: signed net comes out negative when export > import."""
    m: list[registry_map_entry] = abb.registry_map[Registry_Type.HOLDING]
    regs: dict[int, bytes | tuple[bytes, float]] = {
        0x5000: (1_000_000).to_bytes(8, "big"),
        0x5004: (1_250_000).to_bytes(8, "big"),
        0x5008: (-250_000).to_bytes(8, "big", signed=True),
    }
    out = abb.process_registery(regs, m)
    assert out["active_energy_import"] == 10000
    assert out["active_energy_export"] == 12500
    assert out["active_energy_net"] == -2500


# --------------------------------------------------------------------------
# Write path
# --------------------------------------------------------------------------

def _writer(abb: protocol_settings, current: int, register: int | None = None) -> tuple[Any, MagicMock]:
    """A stand-in transport whose real encode helpers are bound; returns (self, write_registers)."""
    fake: Any = MagicMock()
    fake._log = MagicMock()
    fake._protocol = abb
    fake.write_mode = TransportWriteMode.UNSAFE
    fake.transport_name = "test"
    for name in ("_entry_word_count", "_entry_byte_order", "_register_words_to_bytes", "_bytes_to_register_words"):
        setattr(fake, name, types.MethodType(getattr(modbus_base, name), fake))
    base: int = register if register is not None else _net_entry(abb).register
    words: list[bytes] = _wire_words(current, "abcd")

    def entry_byte_order(entry: registry_map_entry) -> object:
        return abb.get_entry_byteorder(entry)

    def read_modbus_registers(**_kwargs: object) -> dict[int, int]:
        return {base + i: int.from_bytes(w, "big") for i, w in enumerate(words)}

    fake._entry_byte_order = entry_byte_order
    fake.read_modbus_registers = read_modbus_registers
    return fake, fake.write_registers


@pytest.mark.parametrize("value", [INT64_MIN, -1, 0, 5, INT64_MAX])
def test_int64_write_encodes_signed_big_endian(abb: protocol_settings, value: int) -> None:
    """write_variable packs INT64 as four signed big-endian words."""
    fake, write_registers = _writer(abb, current=0)
    entry: registry_map_entry = dataclasses.replace(_net_entry(abb), unit_mod=1.0, write_mode=_net_entry(abb).write_mode)
    modbus_base.write_variable(fake, entry, value)
    assert write_registers.call_count == 1
    sent: list[int] = write_registers.call_args.args[1]
    assert len(sent) == 4
    assert int.from_bytes(b"".join(w.to_bytes(2, "big") for w in sent), "big", signed=True) == value


@pytest.mark.parametrize("value", [INT64_MIN - 1, INT64_MAX + 1])
def test_int64_write_rejects_out_of_range(abb: protocol_settings, value: int) -> None:
    """Out-of-range values are refused and nothing reaches the wire."""
    fake, write_registers = _writer(abb, current=0)
    entry: registry_map_entry = dataclasses.replace(_net_entry(abb), unit_mod=1.0)
    modbus_base.write_variable(fake, entry, value)
    write_registers.assert_not_called()
    assert fake._log.error.called


# --------------------------------------------------------------------------
# Consumers of the type list
# --------------------------------------------------------------------------

def test_int64_is_numeric_for_home_assistant(abb: protocol_settings) -> None:
    """MQTT/HA treats INT64 as a plain number (unit + state_class allowed)."""
    assert is_numeric_entry(_net_entry(abb), has_code_map=False)


def test_int64_maps_to_bigint_in_timescaledb() -> None:
    """INT64 -> BIGINT (signed 64-bit) while UINT64 stays NUMERIC (does not fit BIGINT)."""
    assert timescaledb.timescale_type_map["INT64"] == "BIGINT"
    assert timescaledb.timescale_type_map["UINT64"] == "NUMERIC"


@pytest.mark.parametrize("data_type", NUMERIC_TYPES, ids=lambda t: t.name)
def test_every_numeric_type_is_wired_everywhere(abb: protocol_settings, data_type: Data_Type) -> None:
    """Guard: a numeric Data_Type missing from any enumeration fails here."""
    assert Data_Type.getSize(data_type) > 0, "missing from Data_Type.getSize width table"
    assert is_numeric_entry(_probe_entry(abb, data_type=data_type), has_code_map=False), "missing from mqtt._HA_NUMERIC_DATA_TYPES"
    assert data_type.name in timescaledb.timescale_type_map, "missing from timescale_type_map"
    assert f"`{data_type.name}`" in PROTOCOLS_DOC.read_text(encoding="utf-8"), "missing from documentation/usage/protocols.md"


@pytest.mark.parametrize(("data_type", "words"), list(MULTI_REGISTER_WORDS.items()), ids=lambda v: getattr(v, "name", str(v)))
def test_multi_register_types_have_word_counts(abb: protocol_settings, data_type: Data_Type, words: int) -> None:
    """Every multi-register type reports the right word count and decodes zero bytes without error."""
    entry: registry_map_entry = _probe_entry(abb, data_type=data_type, register_list=[], concatenate=False)
    assert abb.entry_word_count(entry) == words
    zero: dict[int, int] = {entry.register + i: 0 for i in range(words)}
    assert abb.process_register_ushort(zero, dataclasses.replace(entry, unit_mod=1.0)) == 0


# --------------------------------------------------------------------------
# Default value ranges (signed types must be able to go negative)
# --------------------------------------------------------------------------

UINT64_MAX: int = (1 << 64) - 1
FLOAT32_MAX: int = int(3.4028234663852886e38)
FLOAT64_MAX: int = int(sys.float_info.max)


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (Data_Type.BYTE, (0, 255)),
        (Data_Type.USHORT, (0, 65535)),
        (Data_Type.SHORT, (-32768, 32767)),
        (Data_Type.UINT, (0, 0xFFFFFFFF)),
        (Data_Type.ACC32, (0, 0xFFFFFFFF)),
        (Data_Type.INT, (-(1 << 31), (1 << 31) - 1)),
        (Data_Type.UINT64, (0, UINT64_MAX)),
        (Data_Type.INT64, (INT64_MIN, INT64_MAX)),
        (Data_Type.FLOAT32, (-FLOAT32_MAX, FLOAT32_MAX)),
        (Data_Type.FLOAT64, (-FLOAT64_MAX, FLOAT64_MAX)),
        (_dt("_2SBIT"), (-2, 1)),
        (_dt("_4SBIT"), (-8, 7)),
        (_dt("_16SBIT"), (-32768, 32767)),
        (_dt("_2SMBIT"), (-1, 1)),
        (_dt("_4SMBIT"), (-7, 7)),
        (_dt("_16SMBIT"), (-32767, 32767)),
        # Unsigned bit-fields: 0 .. 2**n - 1
        (_dt("_1BIT"), (0, 1)),
        (_dt("_4BIT"), (0, 15)),
        (_dt("_8BIT"), (0, 255)),
        (_dt("_16BIT"), (0, 65535)),
        # Unchanged historical default for types with no numeric range
        (Data_Type.ASCII, (0, 65535)),
        (_dt("_16BIT_FLAGS"), (0, 65535)),
    ],
    ids=lambda v: v.name if isinstance(v, Data_Type) else "",
)
def test_default_range_per_type(data_type: Data_Type, expected: tuple[int, int]) -> None:
    """Every type's default (min, max) is its true range; signed types include negatives."""
    assert Data_Type.getDefaultRange(data_type) == expected


@pytest.mark.parametrize("data_type", [t for t in Data_Type if 300 < t.value < 400], ids=lambda t: t.name)
def test_signed_bit_ranges_match_width(data_type: Data_Type) -> None:
    """Two's-complement N-bit range is exactly -2**(N-1) .. 2**(N-1)-1."""
    bits: int = Data_Type.getSize(data_type)
    assert Data_Type.getDefaultRange(data_type) == (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)


def _entries(name: str, registry_type: Registry_Type) -> dict[str, registry_map_entry]:
    logging.disable(logging.CRITICAL)
    try:
        return {e.variable_name: e for e in protocol_settings(name).registry_map[registry_type]}
    finally:
        logging.disable(logging.NOTSET)


def test_loader_applies_signed_defaults_from_real_maps() -> None:
    """CSV rows with no `values` range get the signed defaults (a_tronix SHORT/INT, eastron FLOAT32)."""
    a = _entries("a_tronix", Registry_Type.HOLDING)
    assert (a["battery_current"].data_type, a["battery_current"].value_min, a["battery_current"].value_max) == (Data_Type.SHORT, -32768, 32767)
    assert (a["battery_power"].data_type, a["battery_power"].value_min, a["battery_power"].value_max) == (Data_Type.INT, -(1 << 31), (1 << 31) - 1)
    e = _entries("eastron_eastron_sdm120m", Registry_Type.INPUT)
    assert (e["active_power"].data_type, e["active_power"].value_min, e["active_power"].value_max) == (Data_Type.FLOAT32, -FLOAT32_MAX, FLOAT32_MAX)


def test_explicit_values_range_still_overrides_default() -> None:
    """A `values` range in the CSV wins over the type default (a_tronix off-grid frequency 4500-6500)."""
    freq: registry_map_entry = _entries("a_tronix", Registry_Type.HOLDING)["offgrid_frequency_setting"]
    assert (freq.value_min, freq.value_max) == (4500, 6500)


@pytest.mark.parametrize(
    ("variable", "value", "ok"),
    [
        ("battery_current", -5, True), ("battery_current", -32768, True), ("battery_current", 32767, True),
        ("battery_current", -32769, False), ("battery_current", 32768, False),
        ("battery_power", -2_000_000_000, True), ("battery_power", -(1 << 31) - 1, False),
    ],
)
def test_signed_values_validate_against_signed_defaults(variable: str, value: int, ok: bool) -> None:
    """validate_registry_entry accepts negatives inside the signed range and rejects outside it."""
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings("a_tronix")
        entry: registry_map_entry = next(e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == variable)
        assert bool(proto.validate_registry_entry(entry, value)) is ok
    finally:
        logging.disable(logging.NOTSET)


def test_mqtt_number_bounds_allow_negative_for_signed_types() -> None:
    """Home Assistant number entities for signed registers get a negative minimum."""
    entry: registry_map_entry = _entries("a_tronix", Registry_Type.HOLDING)["battery_current"]
    low, high, _step = number_bounds(dataclasses.replace(entry, unit_mod=0.1))
    assert math.isclose(low, -3276.8) and math.isclose(high, 3276.7)


@pytest.mark.parametrize("unit_mod", [1.0, 10.0, 1000.0, 0.001])
@pytest.mark.parametrize("data_type", [Data_Type.FLOAT32, Data_Type.FLOAT64, Data_Type.UINT64, Data_Type.INT64], ids=lambda t: t.name)
def test_mqtt_number_bounds_are_finite_and_json_safe(abb: protocol_settings, data_type: Data_Type, unit_mod: float) -> None:
    """Wide ranges scaled by unit_mod never become inf/NaN or oversized ints (invalid for strict JSON parsers)."""
    lo, hi = Data_Type.getDefaultRange(data_type)
    entry: registry_map_entry = _probe_entry(abb, data_type=data_type, value_min=lo, value_max=hi, unit_mod=unit_mod)
    bounds = number_bounds(entry)
    text: str = json.dumps(bounds, allow_nan=False)  # raises on inf/NaN
    assert all(abs(v) < 2 ** 53 or isinstance(v, float) for v in bounds), text
    assert bounds[0] <= bounds[1]


# --------------------------------------------------------------------------
# UINT64 write path must be exact (regression: int(float(value)) lost precision)
# --------------------------------------------------------------------------

def _uint64_entry(abb: protocol_settings) -> registry_map_entry:
    entry: registry_map_entry = next(e for e in abb.registry_map[Registry_Type.HOLDING] if e.variable_name == "active_energy_import")
    assert entry.data_type is Data_Type.UINT64
    return dataclasses.replace(entry, unit_mod=1.0)


@pytest.mark.parametrize(
    "value",
    [0, 1, (1 << 53) + 1, UINT64_MAX - 1, UINT64_MAX, str(UINT64_MAX), str((1 << 53) + 1)],
    ids=lambda v: str(v)[:24],
)
def test_uint64_write_is_exact(abb: protocol_settings, value: int | str) -> None:
    """Values beyond 2**53 (incl. UINT64_MAX) are written bit-for-bit, from ints or integer strings."""
    entry: registry_map_entry = _uint64_entry(abb)
    fake, write_registers = _writer(abb, current=0, register=entry.register)
    modbus_base.write_variable(fake, entry, value)
    assert write_registers.call_count == 1
    sent: list[int] = write_registers.call_args.args[1]
    assert int.from_bytes(b"".join(w.to_bytes(2, "big") for w in sent), "big") == int(value)


@pytest.mark.parametrize("value", [-1, UINT64_MAX + 1, str(UINT64_MAX + 1)], ids=lambda v: str(v)[:24])
def test_uint64_write_rejects_out_of_range(abb: protocol_settings, value: int | str) -> None:
    """Negative and >UINT64_MAX values are refused, nothing reaches the wire."""
    entry: registry_map_entry = _uint64_entry(abb)
    fake, write_registers = _writer(abb, current=0, register=entry.register)
    modbus_base.write_variable(fake, entry, value)
    write_registers.assert_not_called()
    assert fake._log.error.called


# --------------------------------------------------------------------------
# Fixed-length strings occupy size/2 registers (ASCII.N and STRING.N)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("data_type", [Data_Type.ASCII, Data_Type.STRING], ids=lambda t: t.name)
@pytest.mark.parametrize(("chars", "words"), [(1, 1), (2, 1), (3, 2), (6, 3), (16, 8), (32, 16)])
def test_sized_string_word_count(abb: protocol_settings, data_type: Data_Type, chars: int, words: int) -> None:
    """``ASCII.N`` / ``STRING.N`` span ceil(N / 2) registers."""
    entry: registry_map_entry = _probe_entry(
        abb, data_type=data_type, data_type_size=chars, register_list=[], concatenate=False, concatenate_registers=[]
    )
    assert abb.entry_word_count(entry) == words


def test_unsized_ascii_is_still_one_register(abb: protocol_settings) -> None:
    """A plain ``ASCII`` with no size keeps the one-register default (ranges set the length instead)."""
    entry: registry_map_entry = _probe_entry(
        abb, data_type=Data_Type.ASCII, data_type_size=-1, register_list=[], concatenate=False, concatenate_registers=[]
    )
    assert abb.entry_word_count(entry) == 1


def test_a_tronix_serial_number_decodes_from_register_words() -> None:
    """The 16-character serial is rebuilt from its eight 16-bit registers (it used to return two characters)."""
    serial: bytes = b"WTS3P10K00001234"
    entry: registry_map_entry = _entries("a_tronix", Registry_Type.HOLDING)["inverter_sn"]
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings("a_tronix")
    finally:
        logging.disable(logging.NOTSET)
    words: dict[int, int] = {entry.register + i: int.from_bytes(serial[2 * i:2 * i + 2], "big") for i in range(8)}
    assert proto.process_registery(words, [entry]) == {"inverter_sn": "WTS3P10K00001234"}


@pytest.mark.parametrize(
    ("protocol", "variable"),
    [("a_tronix", "inverter_sn"), ("abb_fimer_sunspec", "options"), ("abb_fimer_sunspec", "serial_number")],
)
def test_string_registers_are_fully_inside_a_read_range(protocol: str, variable: str) -> None:
    """The planned Modbus reads cover every register of a multi-register string, including at window edges."""
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings(protocol)
    finally:
        logging.disable(logging.NOTSET)
    entry: registry_map_entry = next(e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name == variable)
    last: int = entry.register + proto.entry_word_count(entry) - 1
    assert any(start <= entry.register and start + count - 1 >= last for start, count in proto.registry_map_ranges[Registry_Type.HOLDING])


def test_whole_field_ascii_still_stops_at_a_null_terminator() -> None:
    """A sized ``ASCII.N`` entry is a null-terminated field: text after the first NUL is ignored."""
    entry: registry_map_entry = _entries("a_tronix", Registry_Type.HOLDING)["inverter_sn"]
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings("a_tronix")
    finally:
        logging.disable(logging.NOTSET)
    assert proto.process_registery({entry.register: b"AB\x00CDEFGHIJKLMNO"}, [entry]) == {"inverter_sn": "AB"}


def test_ascii_range_members_skip_nul_bytes() -> None:
    """Range members are fragments: ABB's one-character-per-register strings (``00 31``) must not decode to nothing."""
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings("abb_fimer_trio_50_60")
    finally:
        logging.disable(logging.NOTSET)
    entries: list[registry_map_entry] = [e for e in proto.registry_map[Registry_Type.INPUT] if e.variable_name == "serial_number"]
    words: dict[int, int] = {1008 + i: ord(ch) for i, ch in enumerate("123789")}
    assert proto.process_registery(words, entries) == {"serial_number": "123789"}
