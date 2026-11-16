# Description: Byte offsets, single-byte types and multi-bit flag keys decode the same way on every transport.
# File: test_byte_offset_decoding.py
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
"""
The standard these tests pin down:

* ``.N`` selects byte N of the register or frame as received.  Byte 0 is the first byte on the wire, which is
  the high byte of a big-endian Modbus register.
* Types of one byte or less (``BYTE``, ``8BIT_FLAGS`` and bit fields of 8 bits or fewer) decode from the single
  byte at the offset, and their ``.bN`` offsets count from that byte's least significant bit.
* Wider types start at the offset byte and read on from there.
* A flag table key such as ``"b0&b1"`` matches when all the named bits are set and replaces their single-bit labels.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_protocol_settings import make_entry, make_protocol

from classes.protocol_settings import Data_Type, protocol_settings, registry_map_entry

_BADC: dict[str, Any] = {"register_endian": "big_endian_byte_swap-BADC"}


def _ushort(entry_overrides: dict[str, Any], register_value: int, codes: dict[str, dict[str, str]] | None = None) -> Any:
    settings: protocol_settings = make_protocol()
    settings.codes.update(codes or {})
    entry: registry_map_entry = make_entry(register=7, **entry_overrides)
    entry.register_byte_explicit = bool(entry_overrides.get("register_byte_explicit", False))  # make_entry predates the field
    return settings.process_register_ushort({7: register_value}, entry)


def _frame(entry_overrides: dict[str, Any], frame: bytes, codes: dict[str, dict[str, str]] | None = None) -> Any:
    settings: protocol_settings = make_protocol()
    settings.codes.update(codes or {})
    entry: registry_map_entry = make_entry(register=0x311, **entry_overrides)
    entry.register_byte_explicit = bool(entry_overrides.get("register_byte_explicit", False))  # make_entry predates the field
    return settings.process_register_bytes({0x311: frame}, entry)


# --- Modbus register (integer) path ---------------------------------------------------------------------------


@pytest.mark.parametrize(("offset", "expected"), [(0, 0x12), (1, 0x34)])
def test_explicit_byte_offset_on_a_modbus_register(offset: int, expected: int) -> None:
    """``0x0F.0`` is the high byte and ``0x0F.1`` the low byte, as protocols.md says."""
    assert _ushort({"data_type": Data_Type.BYTE, "register_byte": offset, "register_byte_explicit": True}, 0x1234) == expected


def test_byte_without_an_offset_still_reads_the_low_byte() -> None:
    assert _ushort({"data_type": Data_Type.BYTE}, 0x1234) == 0x34


@pytest.mark.parametrize("offset", [0, 1])
def test_explicit_byte_offset_names_the_physical_byte_under_a_byte_swap(offset: int) -> None:
    assert _ushort({"data_type": Data_Type.BYTE, "register_byte": offset, "register_byte_explicit": True, "adjustments": _BADC}, 0x1234) == (0x12, 0x34)[offset]


def test_byte_offset_outside_a_16_bit_register_is_skipped() -> None:
    assert _ushort({"data_type": Data_Type.BYTE, "register_byte": 2, "register_byte_explicit": True}, 0x1234) is None


@pytest.mark.parametrize(("offset", "expected"), [(0, "a"), (1, "b")])
def test_8bit_flags_on_a_modbus_register_use_the_selected_byte(offset: int, expected: str) -> None:
    codes: dict[str, dict[str, str]] = {"voltage_codes": {"b0": "a", "b1": "b"}}
    # high byte 0x01 sets b0 in byte 0; low byte 0x02 sets b1 in byte 1
    assert _ushort({"data_type": Data_Type._8BIT_FLAGS, "register_byte": offset, "register_byte_explicit": True}, 0x0102, codes) == expected


def test_a_bit_field_in_the_high_byte_of_a_modbus_register() -> None:
    entry: dict[str, Any] = {"data_type": Data_Type._2BIT, "register_byte": 0, "register_byte_explicit": True, "register_bit": 4, "register_bit_end": 4}
    assert _ushort(entry, 0x3000) == 3  # byte 0 is 0x30; bits 4-5 of that byte


def test_acrel_running_state_bytes_are_distinct() -> None:
    """The Acrel map names the high byte ``0x0091.0`` and the low byte ``0x0091.1``; they used to read the same byte."""
    settings = protocol_settings("acrel_dtsd1352_c")
    from classes.protocol_settings import Registry_Type

    info: dict[str, int | float | str] = settings.process_registery({0x91: 0x0502}, settings.get_registry_map(Registry_Type.HOLDING))

    assert (info["running_state_1"], info["running_state_2"]) == (5, 2)


# --- bytes (CAN / serial frame) path --------------------------------------------------------------------------


@pytest.mark.parametrize(("offset", "expected"), [(0, "a"), (1, "b"), (2, "a,b")])
def test_8bit_flags_in_a_frame_decode_the_byte_at_the_offset(offset: int, expected: str) -> None:
    """8-bit flags used to decode the byte *after* the offset."""
    codes: dict[str, dict[str, str]] = {"voltage_codes": {"b0": "a", "b1": "b"}}
    frame = bytes([0x01, 0x02, 0x03, 0x00, 0, 0, 0, 0])

    assert _frame({"data_type": Data_Type._8BIT_FLAGS, "register_byte": offset, "register_byte_explicit": offset > 0}, frame, codes) == expected


def test_a_2bit_field_in_a_frame_reads_the_byte_at_the_offset() -> None:
    frame = bytes([0, 0, 0, 0, 0, 0, 0b10, 0b11])  # byte 6 = 2, byte 7 = 3

    assert _frame({"data_type": Data_Type._2BIT, "register_byte": 6, "register_byte_explicit": True, "register_bit": 0, "register_bit_end": 0}, frame) == 2
    assert _frame({"data_type": Data_Type._2BIT, "register_byte": 7, "register_byte_explicit": True, "register_bit": 0, "register_bit_end": 0}, frame) == 3


def test_a_frame_byte_flag_row_is_unaffected_by_a_byte_swap_adjustment() -> None:
    codes: dict[str, dict[str, str]] = {"voltage_codes": {"b0": "a"}}
    frame = bytes([0x01, 0x00, 0, 0, 0, 0, 0, 0])

    assert _frame({"data_type": Data_Type._8BIT_FLAGS, "adjustments": _BADC}, frame, codes) == "a"


def test_16bit_flags_still_read_two_bytes_big_endian_from_the_offset() -> None:
    codes: dict[str, dict[str, str]] = {"voltage_codes": {"b0": "low", "b8": "high"}}
    frame = bytes([0x00, 0x01, 0x01, 0x00])  # offset 0: 0x0001 -> b0;  offset 1: 0x0101 -> b0 and b8

    assert _frame({"data_type": Data_Type._16BIT_FLAGS}, frame, codes) == "low"
    assert _frame({"data_type": Data_Type._16BIT_FLAGS, "register_byte": 1, "register_byte_explicit": True}, frame, codes) == "low,high"


# --- multi-bit flag keys --------------------------------------------------------------------------------------

_MULTI: dict[str, dict[str, str]] = {"voltage_codes": {"b0": "StandBy", "b1": "Error", "b0&b1": "Operational", "b2": "Hot"}}


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        (0b001, "StandBy"),
        (0b010, "Error"),
        (0b011, "Operational"),  # replaces StandBy and Error
        (0b101, "StandBy,Hot"),
        (0b111, "Hot,Operational"),
        (0b000, ""),
    ],
)
def test_multi_bit_flag_keys_on_a_modbus_register(bits: int, expected: str) -> None:
    assert _ushort({"data_type": Data_Type._8BIT_FLAGS}, bits, _MULTI) == expected


@pytest.mark.parametrize(
    ("bits", "expected"),
    [(0b001, "StandBy"), (0b011, "Operational"), (0b111, "Hot,Operational")],
)
def test_multi_bit_flag_keys_in_a_frame(bits: int, expected: str) -> None:
    assert _frame({"data_type": Data_Type._8BIT_FLAGS}, bytes([bits, 0]), _MULTI) == expected


def test_the_larger_multi_bit_key_wins_over_one_it_contains() -> None:
    codes: dict[str, dict[str, str]] = {"voltage_codes": {"b0&b1": "pair", "b0&b1&b2": "triple", "b3": "x"}}

    assert _ushort({"data_type": Data_Type._8BIT_FLAGS}, 0b0111, codes) == "triple"
    assert _ushort({"data_type": Data_Type._8BIT_FLAGS}, 0b1011, codes) == "x,pair"
