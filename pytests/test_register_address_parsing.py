"""Tests for how the registry-map loader turns a CSV ``register`` cell into an entry.

These guard against a regression in which the register regex matched only the
leading digits of an address, so that ``0x5000`` loaded as register ``0``, the bare
``x351`` as decimal 351, and ranges such as ``115~119`` collapsed to one register.
They also cover text-keyed protocols (``"register_key_type": "text"``), whose
register column holds labels such as ``SOC`` or OBIS codes such as ``0:1.8.0``.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pytest

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from defs.common import strtoint, strtoint_safe

HEADER: list[str] = [
    "register", "variable_name", "documented_name", "unit", "data_type",
    "values", "read_interval", "writable", "adjustments", "note",
]


def _load(
    tmp_path: Path,
    rows: list[tuple[str, str, str]],
    settings: dict[str, str] | None = None,
) -> dict[str, list[registry_map_entry]]:
    """Write a one-off protocol to ``tmp_path`` and return its entries grouped by variable name.

    ``rows`` are ``(register cell, data type, values)`` tuples; row ``i`` is named ``v{i}``.
    A cell that the loader rejects simply has no entry.
    """
    with (tmp_path / "probe.holding_registry_map.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index, (cell, data_type, values) in enumerate(rows):
            writer.writerow([cell, f"v{index}", f"v{index}", "", data_type, values, "", "R", "", ""])
    (tmp_path / "probe.json").write_text(json.dumps({"transport": "modbus_rtu", **(settings or {})}), encoding="utf-8")

    protocol: protocol_settings = protocol_settings("probe", settings_dir=str(tmp_path))
    grouped: dict[str, list[registry_map_entry]] = {}
    for entry in protocol.registry_map.get(Registry_Type.HOLDING, []):
        grouped.setdefault(entry.variable_name, []).append(entry)
    return grouped


def _one(tmp_path: Path, cell: str, data_type: str = "USHORT") -> registry_map_entry | None:
    """Load a single cell; return its entry or None if the loader rejected it."""
    entries: list[registry_map_entry] = _load(tmp_path, [(cell, data_type, "")]).get("v0", [])
    return entries[-1] if entries else None


# --------------------------------------------------------------------------
# Single addresses, bit and byte offsets
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cell", "register", "bit", "bit_end", "byte"),
    [
        ("5000", 5000, -1, -1, 0),
        ("0", 0, -1, -1, 0),
        ("65535", 65535, -1, -1, 0),
        ("0150", 150, -1, -1, 0),                    # zero-padded decimal is decimal, not octal/invalid
        ("536870911", 536870911, -1, -1, 0),         # 29-bit extended CAN id: more than 5 digits
        ("0x5000", 0x5000, -1, -1, 0),
        ("0X5b00", 0x5B00, -1, -1, 0),               # prefix and digits are case-insensitive
        ("0x000B", 0x000B, -1, -1, 0),
        ("x351", 0x351, -1, -1, 0),                  # bare 'x' prefix (CAN ids, Pylon commands)
        ("X4642", 0x4642, -1, -1, 0),
        ("0x1FFFFFFF", 0x1FFFFFFF, -1, -1, 0),       # 29-bit hex id: more than 4 digits
        ("X311.2", 0x311, -1, -1, 2),                # hex address with a byte offset
        ("5.0", 5, -1, -1, 0),
        ("5.1", 5, -1, -1, 1),
        ("3.b8", 3, 8, 8, 0),
        ("0x1A.b3", 0x1A, 3, 3, 0),                  # hex address with a bit offset
        ("3.b3-5", 3, 3, 5, 0),                      # bit range, short form
        ("3.b3-b5", 3, 3, 5, 0),                     # bit range, form shown in protocols.md
    ],
)
def test_single_register_addresses(tmp_path: Path, cell: str, register: int, bit: int, bit_end: int, byte: int) -> None:
    """Every documented single-address shape resolves to the intended register, bit and byte."""
    entry: registry_map_entry | None = _one(tmp_path, cell)
    assert entry is not None, f"{cell!r} was rejected"
    assert (entry.register, entry.register_bit, entry.register_bit_end, entry.register_byte) == (register, bit, bit_end, byte)
    assert entry.register_list == []
    assert not entry.concatenate
    assert entry.register_key == ""


# --------------------------------------------------------------------------
# Underscore (low_high) lists
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cell", "addresses"),
    [
        ("40_41", [40, 41]),
        ("41_40", [41, 40]),                         # token 0 is the low word whatever the numeric order
        ("40_50", [40, 50]),                         # non-contiguous
        ("40_41_42_43", [40, 41, 42, 43]),
        ("231_232", [231, 232]),                     # was read as the single integer 231232
        ("0x28_0x29", [0x28, 0x29]),
        ("x28_x29", [0x28, 0x29]),                   # bare x prefix inside a list
        ("0x28_41", [0x28, 41]),                     # mixed notations
    ],
)
def test_underscore_lists(tmp_path: Path, cell: str, addresses: list[int]) -> None:
    """Underscore lists keep their word order and never collapse into one integer."""
    entry: registry_map_entry | None = _one(tmp_path, cell, "UINT64" if len(addresses) == 4 else "UINT")
    assert entry is not None
    assert entry.register_list == addresses
    assert entry.register == addresses[0]


# --------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("7~14", list(range(7, 15))),
        ("115-119", list(range(115, 120))),
        ("r7~14", list(range(14, 6, -1))),           # 'r' reads descending
        ("0170-0179", list(range(170, 180))),        # zero-padded decimal range (PACE PACK SN, 20 bytes)
        ("0x011A~0x011B", [0x11A, 0x11B]),
        ("x10~x12", [0x10, 0x11, 0x12]),
    ],
)
def test_ranges_are_expanded(tmp_path: Path, cell: str, expected: list[int]) -> None:
    """A range concatenates every register in it; it must not collapse to its first address."""
    entry: registry_map_entry | None = _one(tmp_path, cell, "ASCII")
    assert entry is not None
    assert entry.concatenate
    assert entry.concatenate_registers == expected


# --------------------------------------------------------------------------
# Malformed addresses are rejected loudly, never half-parsed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cell",
    [
        "0x011A~011B",   # second half lost its 0x (a real typo that used to load as register 282 alone)
        "abc",
        "12abc",
        "0:1.8.0",       # OBIS code in a numeric-keyed protocol
        "1_", "40_", "40__41", "_40",  # empty list tokens must not become a phantom register 0
        "0x",
        "5.b",
    ],
)
def test_malformed_addresses_are_skipped_with_a_warning(
    tmp_path: Path, cell: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad cell loses only its own row and logs a warning; it is never partly parsed."""
    with caplog.at_level(logging.WARNING):
        entry: registry_map_entry | None = _one(tmp_path, cell)
    assert entry is None, f"{cell!r} unexpectedly loaded as register {entry.register if entry else None}"
    assert any("register" in record.getMessage().lower() for record in caplog.records)


def test_one_bad_row_does_not_discard_the_rest(tmp_path: Path) -> None:
    """Rows around a rejected cell still load."""
    grouped: dict[str, list[registry_map_entry]] = _load(tmp_path, [("10", "USHORT", ""), ("oops", "USHORT", ""), ("0x20", "USHORT", "")])
    assert sorted(grouped) == ["v0", "v2"]
    assert (grouped["v0"][0].register, grouped["v2"][0].register) == (10, 0x20)


# --------------------------------------------------------------------------
# Text-keyed protocols
# --------------------------------------------------------------------------

def test_text_keys_are_preserved_and_numbered_in_order(tmp_path: Path) -> None:
    """Labels and OBIS codes load verbatim; each distinct key gets a stable integer id."""
    rows: list[tuple[str, str, str]] = [
        ("Alarm", "STRING", ""),       # v0
        ("AR", "UINT", ""),            # v1
        ("0:1.8.0", "UINT64", ""),     # v2: dots and colons belong to the key
        ("F_AC_IN_V_L1", "FLOAT32", ""),  # v3: underscores belong to the key
        ("soc", "USHORT", ""),         # v4
        ("SOC", "USHORT", ""),         # v5: keys are case-sensitive
        ("Alarm", "USHORT", ""),       # v6: repeated key shares v0's id
        ("5", "USHORT", ""),           # v7: numeric-looking text is still a key, not register 5
        ("0x10", "USHORT", ""),        # v8
    ]
    grouped: dict[str, list[registry_map_entry]] = _load(tmp_path, rows, {"register_key_type": "text"})
    keys: list[str] = [grouped[f"v{i}"][0].register_key for i in range(len(rows))]
    ids: list[int] = [grouped[f"v{i}"][0].register for i in range(len(rows))]
    assert keys == ["Alarm", "AR", "0:1.8.0", "F_AC_IN_V_L1", "soc", "SOC", "Alarm", "5", "0x10"]
    assert ids == [0, 1, 2, 3, 4, 5, 0, 6, 7]
    for entries in grouped.values():
        entry: registry_map_entry = entries[0]
        assert (entry.register_bit, entry.register_bit_end, entry.register_byte) == (-1, -1, 0)
        assert entry.register_list == [] and not entry.concatenate


def test_text_key_description_entries_inherit_the_key(tmp_path: Path) -> None:
    """The generated ``*_desc`` companion of an enum-valued entry carries the same key and id."""
    grouped: dict[str, list[registry_map_entry]] = _load(
        tmp_path, [("CS", "USHORT", '{"0": "off", "3": "bulk"}')], {"register_key_type": "text"}
    )
    desc: list[registry_map_entry] = [e for name, entries in grouped.items() if name.endswith("_desc") for e in entries]
    assert len(desc) == 1
    assert (desc[0].register_key, desc[0].register) == ("CS", 0)


def test_numeric_protocols_have_no_register_key(tmp_path: Path) -> None:
    """Without the setting, nothing is treated as text and register_key stays empty."""
    entry: registry_map_entry | None = _one(tmp_path, "7")
    assert entry is not None and entry.register_key == "" and entry.register == 7


def test_unknown_register_key_type_falls_back_to_numeric(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A typo in the JSON setting warns and behaves as numeric (so labels are rejected, numbers still load)."""
    with caplog.at_level(logging.WARNING):
        grouped: dict[str, list[registry_map_entry]] = _load(
            tmp_path, [("12", "USHORT", ""), ("SOC", "USHORT", "")], {"register_key_type": "txt"}
        )
    assert sorted(grouped) == ["v0"]
    assert any("register_key_type" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------
# strtoint / strtoint_safe
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0x1F", 31), ("0X1f", 31), ("x1F", 31), ("X1f", 31),
        ("150", 150), ("0150", 150), ("00150", 150), ("-5", -5), ("+7", 7),
        (" 42 ", 42), (17, 17), ("0", 0),
    ],
)
def test_strtoint_parses_numbers(text: str | int, expected: int) -> None:
    """Hex needs a 0x/x prefix; everything else numeric is decimal, including zero-padded values."""
    assert strtoint(text) == expected
    assert strtoint_safe(text) == expected


@pytest.mark.parametrize("text", ["231_232", "40_41", "1_000", "12abc", "abc", "0xZZ", "1.5", "0:1.8.0"])
def test_strtoint_rejects_non_numbers(text: str) -> None:
    """Underscores, stray letters and non-integers are not numbers (int() would accept '231_232' as 231232)."""
    assert isinstance(strtoint(text), str)
    with pytest.raises(ValueError):
        strtoint_safe(text)


# --------------------------------------------------------------------------
# Shipped maps (regression checks against real data)
# --------------------------------------------------------------------------

def _entries(name: str) -> list[registry_map_entry]:
    logging.disable(logging.CRITICAL)
    try:
        protocol: protocol_settings = protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)
    return [entry for entries in protocol.registry_map.values() for entry in entries if not entry.variable_name.endswith("_desc")]


def test_pylon_can_ids_are_hex() -> None:
    """Pylontech CAN frame 0x351 loads as 0x351 (849), not decimal 351."""
    registers: set[int] = {entry.register for entry in _entries("pylon_can")}
    assert 0x351 in registers and 351 not in registers


def test_pylon_rs485_command_is_hex() -> None:
    """The Pylon RS-485 command x4642 loads as 0x4642, which is what gets packed into the frame."""
    registers: set[int] = {entry.register for entry in _entries("pylon_rs485_v3.3")}
    assert 0x4642 in registers and 4642 not in registers


def test_srne_hex_registers_are_distinct() -> None:
    """SRNE's 0x-prefixed addresses used to collapse to register 0."""
    registers: list[int] = [entry.register for entry in _entries("srne_v3.9")]
    assert 0 not in registers or registers.count(0) < 5
    assert len(set(registers)) > len(registers) // 2


def test_srne_discharge_amp_hours_is_a_two_register_range() -> None:
    """``0x011A~0x011B`` (typo fixed in the CSV) concatenates two registers."""
    entry: registry_map_entry = next(e for e in _entries("srne_v3.9") if e.variable_name == "total_discharging_amp_hrs_of_the_battery")
    assert entry.concatenate_registers == [0x11A, 0x11B]


def test_eg4_serial_number_range_is_not_truncated() -> None:
    """EG4's inverter serial number ``115~119`` reads five registers, not one."""
    serial: list[registry_map_entry] = [e for e in _entries("eg4_18kpv") if e.variable_name == "serial_number"]
    assert serial and all(e.concatenate_registers == [115, 116, 117, 118, 119] for e in serial)
    assert all(e.data_type is Data_Type.ASCII for e in serial)


@pytest.mark.parametrize(
    ("protocol", "key", "expected_type"),
    [
        ("victron_vedirect_serial", "Alarm", Data_Type.STRING),
        ("victron_mk3usb_vebus", "F_AC_IN_V_L1", Data_Type.FLOAT32),
        ("sma_energy_meter_speedwire", "0:1.8.0", Data_Type.UINT64),
    ],
)
def test_text_keyed_protocols_load(protocol: str, key: str, expected_type: Data_Type) -> None:
    """VE.Direct, VE.Bus and SMA Speedwire maps (label/OBIS keys) now load every row."""
    entries: list[registry_map_entry] = _entries(protocol)
    assert len(entries) > 20
    keys: list[str] = [entry.register_key for entry in entries]
    assert all(keys), "every row of a text-keyed protocol must carry its key"
    match: registry_map_entry = next(entry for entry in entries if entry.register_key == key)
    assert match.data_type is expected_type
