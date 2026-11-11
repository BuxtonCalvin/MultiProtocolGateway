# Description: Tests for the optional ``register_address_step`` protocol setting (byte-addressed Modbus maps such as JK BMS).
# File: test_register_address_step.py
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

"""Tests for ``register_address_step``.

Most Modbus devices number registers consecutively, so word ``i`` of a read starting at address ``a``
belongs to address ``a + i``.  A few devices (JK BMS Modbus) number registers by byte offset, so
consecutive 16-bit words are 2 addresses apart.  ``register_address_step`` in the protocol JSON
describes that, and must leave every protocol that does not set it untouched.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
import random
import re
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from classes.transports.modbus_base import modbus_base

JK: str = "jk_bms_modbus_rtu"
# Protocols that do not set the option: their ranges must be exactly what the original formulas gave.
UNCHANGED: list[str] = ["eg4_v58", "deye_sunsynk_hybrid", "abb_fimer_trio_50_60", "pace_bms_v1.3", "victron_smartsolar_mppt"]


def _load(name: str) -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def _legacy_ranges(proto: protocol_settings, registry_map: list[registry_map_entry], max_register: int) -> list[tuple[int, int]]:
    """The range algorithm exactly as it was before ``register_address_step`` existed."""
    batch: int = int(proto.settings.get("batch_size", 40))
    ranges: list[tuple[int, int]] = []
    for start in range(0, max_register + 1, batch):
        end: int = start + batch
        lo: int | None = None
        hi: int | None = None
        for r in registry_map:
            if start <= r.register < end:
                if r.write_mode.name in ("READDISABLED", "WRITEONLY"):
                    continue
                top: int = max(r.register_list) if r.register_list else r.register + proto.entry_word_count(r) - 1
                lo = r.register if lo is None or r.register < lo else lo
                hi = top if hi is None or top > hi else hi
        if lo is not None and hi is not None:
            ranges.append((lo, hi - lo + 1))
    return ranges


def _transport(proto: protocol_settings) -> Any:
    """Minimal stand-in carrying just what the extraction helpers use from a modbus transport."""
    fake: Any = SimpleNamespace(_protocol=proto)
    fake._register_step = modbus_base._register_step.__get__(fake)
    return fake


def _extract(fake: Any, registers: list[int], start: int) -> dict[int, int]:
    response: Any = SimpleNamespace(registers=registers)
    result: dict[int, int] | None = modbus_base._extract_response_values(fake, response, Registry_Type.HOLDING, (start, len(registers)))
    assert result is not None
    return result


# --------------------------------------------------------------------------- default behavior


@pytest.mark.parametrize("name", UNCHANGED)
def test_protocols_without_the_setting_keep_step_one_and_identical_ranges(name: str) -> None:
    """Regression: a protocol that does not set the option behaves exactly as before."""
    proto: protocol_settings = _load(name)
    assert proto.register_address_step == 1
    for registry_type, registry_map in proto.registry_map.items():
        size: int = proto.registry_map_size[registry_type]
        assert proto.calculate_registry_ranges(registry_map, size, init=True) == _legacy_ranges(proto, registry_map, size)


@pytest.mark.parametrize("value", [None, 0, -3, "x", "", 1.0])
def test_invalid_or_missing_values_fall_back_to_one(value: Any) -> None:
    """Edge case: anything that is not a positive integer means the normal consecutive layout."""
    proto: protocol_settings = _load("eg4_v58")
    if value is None:
        proto.settings.pop("register_address_step", None)
    else:
        proto.settings["register_address_step"] = value
    assert proto.register_address_step == 1


def test_string_value_from_json_is_accepted() -> None:
    """Settings values can arrive as strings; ``"2"`` must work like ``2``."""
    proto: protocol_settings = _load("eg4_v58")
    proto.settings["register_address_step"] = "2"
    assert proto.register_address_step == 2


def test_transport_step_helper_falls_back_when_protocol_has_no_attribute() -> None:
    """Mocks and partial protocol objects must not break reads."""
    fake: Any = SimpleNamespace(_protocol=SimpleNamespace())
    assert modbus_base._register_step(fake) == 1
    fake._protocol = SimpleNamespace(register_address_step="bad")
    assert modbus_base._register_step(fake) == 1


def test_extraction_keys_are_consecutive_for_step_one_and_spaced_for_step_two() -> None:
    """Word ``i`` of a read at ``a`` is stored at ``a + i * step``."""
    normal: Any = _transport(_load("eg4_v58"))
    assert _extract(normal, [10, 20, 30], 100) == {100: 10, 101: 20, 102: 30}
    jk: Any = _transport(_load(JK))
    assert _extract(jk, [10, 20, 30], 4608) == {4608: 10, 4610: 20, 4612: 30}


# --------------------------------------------------------------------------- JK end-to-end


def _jk_rows() -> list[registry_map_entry]:
    return _load(JK).registry_map[Registry_Type.HOLDING]


def test_jk_protocol_declares_a_step_and_an_aligned_map() -> None:
    """The JK JSON sets the step, and every JK address is even (so a step of 2 can reach it)."""
    proto: protocol_settings = _load(JK)
    assert proto.register_address_step == 2
    assert proto.settings["transport"] == "modbus_rtu"
    assert all(e.register % 2 == 0 for e in proto.registry_map[Registry_Type.HOLDING])


def test_jk_ranges_are_block_reads_within_protocol_limits() -> None:
    """Blocks are read together (few requests), stay within Modbus limits and never overlap."""
    proto: protocol_settings = _load(JK)
    ranges: list[tuple[int, int]] = proto.registry_map_ranges[Registry_Type.HOLDING]
    assert 3 <= len(ranges) <= 12
    assert all(1 <= count <= 125 for _, count in ranges)
    spans: list[tuple[int, int]] = sorted((s, s + (c - 1) * 2) for s, c in ranges)
    assert all(a_end < b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def _memory_image(proto: protocol_settings) -> tuple[dict[int, int], dict[str, float]]:
    """A byte-addressed device: random values written at each row's JK address."""
    rnd: random.Random = random.Random(11)  # noqa: S311
    mem: dict[int, int] = {}
    expected: dict[str, float] = {}

    def put(addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            mem[addr + i] = b

    for e in proto.registry_map[Registry_Type.HOLDING]:
        if e.data_type.name in ("USHORT", "SHORT", "UINT", "INT"):
            fmt, lo, hi = {"USHORT": (">H", 0, 60000), "SHORT": (">h", -3000, 3000), "UINT": (">I", 0, 4_000_000_000), "INT": (">i", -2_000_000_000, 2_000_000_000)}[e.data_type.name]
            value: int = rnd.randint(lo, hi)
            put(e.register, struct.pack(fmt, value))
            expected[e.variable_name] = value * e.unit_mod
    return mem, expected


def _read_poll(proto: protocol_settings, entries: list[registry_map_entry], mem: dict[int, int]) -> dict[str, float | int | str]:
    fake: Any = _transport(proto)
    registry: dict[int, int] = {}
    max_register: int = max(e.register + 2 for e in entries)
    for start, count in proto.calculate_registry_ranges(entries, max_register, init=True):
        words: list[int] = [(mem.get(start + 2 * i, 0) << 8) | mem.get(start + 2 * i + 1, 0) for i in range(count)]
        registry.update(_extract(fake, words, start))
    return proto.process_registery(registry, entries)


def test_jk_full_map_decodes_every_numeric_row_correctly() -> None:
    """Block reads of a byte-addressed device decode exactly like per-row reads."""
    proto: protocol_settings = _load(JK)
    mem, expected = _memory_image(proto)
    out: dict[str, float | int | str] = _read_poll(proto, proto.registry_map[Registry_Type.HOLDING], mem)
    assert len(expected) > 100
    for name, value in expected.items():
        assert float(out[name]) == pytest.approx(value, rel=1e-9, abs=1e-9), name # type: ignore


@pytest.mark.parametrize("keep", [["cell_1_voltage"], ["cell_32_voltage", "serial_number"], ["state_of_charge", "total_voltage", "current"], ["mosfet_temperature", "battery_temperature_3"]])
def test_jk_allowlist_masks_are_safe_for_any_selection(keep: list[str]) -> None:
    """Selecting any subset (including a block's first row or none of it) must still read correct values."""
    proto: protocol_settings = _load(JK)
    mem, expected = _memory_image(proto)
    entries: list[registry_map_entry] = [e for e in proto.registry_map[Registry_Type.HOLDING] if e.variable_name in keep]
    out: dict[str, float | int | str] = _read_poll(proto, entries, mem)
    for name in keep:
        if name in expected:
            assert float(out[name]) == pytest.approx(expected[name], rel=1e-9, abs=1e-9), name # type: ignore


def test_jk_ascii_and_byte_halves_decode_at_byte_addresses() -> None:
    """Text blocks and packed 8-bit pairs, which span several JK addresses, decode correctly."""
    proto: protocol_settings = _load(JK)
    by_name: dict[str, registry_map_entry] = {e.variable_name: e for e in proto.registry_map[Registry_Type.HOLDING]}
    mem: dict[int, int] = {}
    model: bytes = b"JK_PB2A16S20P\x00\x00\x00"
    for i, b in enumerate(model):
        mem[by_name["device_model"].register + i] = b
    soc_reg: int = by_name["state_of_charge"].register
    mem[soc_reg], mem[soc_reg + 1] = 1, 87  # high byte = balancing state, low byte = state of charge
    entries: list[registry_map_entry] = [by_name[n] for n in ("device_model", "state_of_charge", "balancing_state")]
    out: dict[str, float | int | str] = _read_poll(proto, entries, mem)
    assert out["device_model"] == "JK_PB2A16S20P"
    assert out["state_of_charge"] == 87
    assert out["balancing_state"] == 1


def test_jk_slow_rows_are_not_read_every_poll() -> None:
    """Settings/device-info rows use long read intervals, so a later poll is only the live-data blocks."""
    proto: protocol_settings = _load(JK)
    entries: list[registry_map_entry] = proto.registry_map[Registry_Type.HOLDING]
    for e in entries:
        e.next_read_timestamp = 0
        e.read_interval = 15_000 if e.read_interval < 3_600_000 else e.read_interval
    first: list[tuple[int, int]] = proto.calculate_registry_ranges(entries, 6000, init=True)
    proto.calculate_registry_ranges(entries, 6000, timestamp=1000.0)  # everything due once, schedules the next read
    later: list[tuple[int, int]] = proto.calculate_registry_ranges(entries, 6000, timestamp=1020.0)
    assert 0 < len(later) < len(first)
    covered: set[int] = {s + 2 * i for s, c in later for i in range(c)}
    assert not any(e.variable_name == "smart_sleep_voltage" and e.register in covered for e in entries)


def test_jk_csv_registers_match_jk_documented_addresses_in_notes() -> None:
    """Each row's note starts with the JK hex address; it must equal the register column."""
    for e in _jk_rows():
        if e.variable_name.endswith("_desc"):  # derived helper rows the loader adds for flag registers
            continue
        match = re.match(r"0x([0-9A-Fa-f]+)", e.note)
        assert match is not None, e.variable_name
        assert int(match.group(1), 16) == e.register, e.variable_name
