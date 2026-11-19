"""Startup read: rows read once per connection, record blocks, unreadable registers and shared code tables.

Uses a small synthetic protocol so the behavior is pinned independently of any manufacturer's map.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pytest
from conftest import approx

from classes.discovery.startup import (
    StartupCache,
    StartupResult,
    plain_ranges,
    read_startup_values,
    startup_entries,
)
from classes.protocol_settings import (
    VIRTUAL_REGISTER_BASE,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

HEADER: list[str] = [
    "register", "variable_name", "documented_name", "unit", "data_type", "values",
    "read_interval", "writable", "adjustments", "note",
]
SCALE: str = json.dumps({"Context": {"formula": "x*{ct}"}})
VOLTS: str = json.dumps({"Context": {"formula": "x*{pt}"}})

ROWS: list[list[str]] = [
    ["0x0010", "pt", "PT", "", "USHORT", "", "startup", "R", "", ""],
    ["0x0011", "ct", "CT", "", "USHORT", "", "startup", "R", "", ""],
    ["0x0012", "disabled_static", "Disabled static", "", "USHORT", "", "startup", "RD", "", ""],
    ["0x0013", "reserved_static", "Reserved static", "", "USHORT", "", "startup", "R", "", ""],      # unreadable
    ["0x0020", "volts", "Voltage", "0.1V", "USHORT", "", "", "R", VOLTS, ""],                     # polled
    ["0x0030.0", "address", "Address", "", "BYTE", "", "startup", "R", "", ""],
    ["0x0030.1", "baud_code", "Baud code", "", "BYTE", "", "startup", "R", "", ""],
    # a record block: 3 records of 6 words, one template row per field
    ["[hist_{i}_base+0]", "hist_{i}_year", "History {i} year", "", "USHORT", "", "startup", "R", "", ""],
    ["[hist_{i}_base+2]", "hist_{i}_energy", "History {i} energy", "0.01kWh", "UINT", "", "startup", "R", SCALE, ""],
    ["[hist_{i}_base+4]", "hist_{i}_event", "History {i} event", "", "USHORT", "@event_codes", "startup", "R", "", ""],
]
SETTINGS: dict[str, object] = {
    "transport": "modbus_rtu",
    "batch_size": 40,
    "record_blocks": {"hist": {"registry": "holding", "address": "0x1001", "records": 3, "words": 6, "address_step": 1}},
    "unreadable_registers": {"holding": ["0x0013-0x0015", "0x0090"]},
    "event_codes": {"1": "Power on", "2": "Power off"},
}


def _protocol(tmp_path: Path, rows: list[list[str]] | None = None, settings: dict[str, object] | None = None) -> protocol_settings:
    with (tmp_path / "probe.holding_registry_map.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows if rows is not None else ROWS)
    (tmp_path / "probe.json").write_text(json.dumps(settings if settings is not None else SETTINGS), encoding="utf-8")
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings("probe", settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)


class FakeMeter:
    """A meter that answers plain reads from a register dict and record reads only at exact record handles."""

    def __init__(self, registers: dict[int, int], records: dict[int, list[int]]) -> None:
        self.registers: dict[int, int] = registers
        self.records: dict[int, list[int]] = records
        self.requests: list[tuple[int, int, Registry_Type]] = []

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        self.requests.append((start, count, registry_type))
        if start in self.records:                      # a record handle: only a whole-record read is answered
            words: list[int] = self.records[start]
            return list(words) if count == len(words) else None
        if all(address in self.registers for address in range(start, start + count)):
            return [self.registers[start + offset] for offset in range(count)]
        return None


def _meter() -> FakeMeter:
    return FakeMeter(
        registers={0x10: 10, 0x11: 40, 0x12: 99, 0x30: 0x0102},
        records={
            0x1001: [2026, 0, 0x0001, 0x86A0, 1, 0],       # energy 0x000186A0 = 100000 -> 1000.00 kWh
            0x1002: [2025, 0, 0x0000, 0x2710, 2, 0],       # 10000 -> 100.00 kWh, event 2
            0x1003: [2024, 0, 0x0000, 0x03E8, 9, 0],       # 1000  -> 10.00 kWh, event 9 (not in the table)
        },
    )


# ---------------------------------------------------------------- loading
def test_startup_rows_and_record_fields_are_flagged(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    entries: dict[str, registry_map_entry] = {e.variable_name: e for e in protocol.registry_map[Registry_Type.HOLDING]}
    assert entries["pt"].read_at_startup and entries["ct"].read_at_startup and entries["address"].read_at_startup
    assert not entries["volts"].read_at_startup
    for record in (1, 2, 3):
        field: registry_map_entry = entries[f"hist_{record}_energy"]
        assert field.read_at_startup and field.register >= VIRTUAL_REGISTER_BASE
    assert "hist_4_energy" not in entries
    assert protocol.has_startup_registers


def test_a_template_row_gets_one_entry_per_record_and_unique_virtual_addresses(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    fields: list[registry_map_entry] = [e for e in protocol.registry_map[Registry_Type.HOLDING] if e.variable_name.startswith("hist_") and not e.variable_name.endswith("_desc")]
    assert len(fields) == 9
    assert len({e.register for e in fields}) == 9


def test_startup_rows_are_not_polled(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    ranges: list[tuple[int, int]] = protocol.calculate_registry_ranges(protocol.registry_map[Registry_Type.HOLDING], 0x40, init=True)
    assert ranges == [(0x20, 1)], "only the polled row is in the polling windows"


def test_record_fields_do_not_enlarge_the_register_map(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    assert protocol.registry_map_size[Registry_Type.HOLDING] < 0x100


def test_the_readable_startup_rows_exclude_disabled_ones(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    names: set[str] = {e.variable_name for e in startup_entries(protocol, Registry_Type.HOLDING)}
    assert "disabled_static" not in names
    assert {"pt", "ct", "address", "baud_code", "hist_1_year"} <= names


# ---------------------------------------------------------------- unreadable registers
def test_requests_never_touch_an_unreadable_register(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    meter: FakeMeter = _meter()
    read_startup_values(meter, protocol)
    unreadable: set[int] = {0x13, 0x14, 0x15, 0x90}
    for start, count, _type in meter.requests:
        if start < VIRTUAL_REGISTER_BASE and start not in meter.records:
            assert unreadable.isdisjoint(range(start, start + count)), (start, count)
    assert (0x13, 1, Registry_Type.HOLDING) not in meter.requests, "a row on a reserved register is never requested"


def test_plain_ranges_split_around_unreadable_registers_and_batch_size(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    entries: list[registry_map_entry] = startup_entries(protocol, Registry_Type.HOLDING)
    assert plain_ranges(protocol, Registry_Type.HOLDING, entries) == [(0x10, 2), (0x30, 1)]


def test_polled_windows_split_around_unreadable_registers(tmp_path: Path) -> None:
    rows: list[list[str]] = [
        ["0x0020", "a", "A", "", "USHORT", "", "", "R", "", ""],
        ["0x0022", "b", "B", "", "USHORT", "", "", "R", "", ""],
        ["0x0023", "c", "C", "", "USHORT", "", "", "R", "", ""],
    ]
    split: protocol_settings = _protocol(tmp_path, rows, {"transport": "modbus_rtu", "unreadable_registers": {"holding": ["0x0021"]}})
    assert split.calculate_registry_ranges(split.registry_map[Registry_Type.HOLDING], 0x30, init=True) == [(0x20, 1), (0x22, 2)]


def test_without_unreadable_registers_polling_ranges_are_unchanged(tmp_path: Path) -> None:
    rows: list[list[str]] = [
        ["0x0020", "a", "A", "", "USHORT", "", "", "R", "", ""],
        ["0x0022", "b", "B", "", "USHORT", "", "", "R", "", ""],
    ]
    plain: protocol_settings = _protocol(tmp_path, rows, {"transport": "modbus_rtu"})
    assert plain.calculate_registry_ranges(plain.registry_map[Registry_Type.HOLDING], 0x30, init=True) == [(0x20, 3)]


def test_unreadable_register_specs_accept_ranges_addresses_and_numbers(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path, ROWS, {**SETTINGS, "unreadable_registers": {"holding": ["0x0013-0x0015", "144", 200, "0x00C8-0x00CA"], "bogus": ["0x1"], "input": "nope"}})
    assert protocol.is_unreadable(Registry_Type.HOLDING, 0x14)
    assert protocol.is_unreadable(Registry_Type.HOLDING, 144)
    assert protocol.is_unreadable(Registry_Type.HOLDING, 0xC9)
    assert not protocol.is_unreadable(Registry_Type.HOLDING, 0x16)
    assert not protocol.is_unreadable(Registry_Type.INPUT, 0x14)


# ---------------------------------------------------------------- the read
def test_startup_values_are_decoded_with_scaling_codes_and_packed_bytes(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    result: StartupResult = read_startup_values(_meter(), protocol)
    values: dict[str, int | float | str] = result.values
    assert values["pt"] == 10 and values["ct"] == 40
    assert values["address"] == 1 and values["baud_code"] == 2, "high and low byte of one register"
    assert values["hist_1_year"] == 2026 and values["hist_3_year"] == 2024
    assert values["hist_1_energy"] == approx(1000.00 * 40), "scaled by the ct value read in the same pass"
    assert values["hist_2_energy"] == approx(100.00 * 40)
    assert values["hist_1_event_desc"] == "Power on" and values["hist_2_event_desc"] == "Power off"
    assert "disabled_static" not in values and "reserved_static" not in values
    assert result.complete and result.records_read == 3


def test_records_are_read_with_exact_size_requests_at_their_own_address(tmp_path: Path) -> None:
    meter: FakeMeter = _meter()
    read_startup_values(meter, _protocol(tmp_path))
    record_requests: list[tuple[int, int]] = [(start, count) for start, count, _t in meter.requests if start >= 0x1001]
    assert record_requests == [(0x1001, 6), (0x1002, 6), (0x1003, 6)]


def test_a_record_with_no_enabled_row_is_not_read(tmp_path: Path) -> None:
    rows: list[list[str]] = [list(row) for row in ROWS]
    for row in rows:
        if row[1].startswith("hist_"):
            row[7] = "RD"
    meter: FakeMeter = _meter()
    result: StartupResult = read_startup_values(meter, _protocol(tmp_path, rows))
    assert not [r for r in meter.requests if r[0] >= 0x1001]
    assert result.records_read == 0 and "hist_1_year" not in result.values


def test_only_the_records_with_an_enabled_row_are_requested(tmp_path: Path) -> None:
    """Disabling a row for every record keeps its record out of the read once all of that record's rows are off."""
    rows: list[list[str]] = [row for row in ROWS if not row[1].startswith("hist_")]
    rows.append(["[hist_{i}_base+0]", "hist_{i}_year", "History {i} year", "", "USHORT", "", "startup", "R", "", ""])
    meter: FakeMeter = _meter()
    result: StartupResult = read_startup_values(meter, _protocol(tmp_path, rows))
    assert result.records_read == 3 and sorted(k for k in result.values if k.startswith("hist_")) == ["hist_1_year", "hist_2_year", "hist_3_year"]


def test_a_block_that_does_not_answer_is_abandoned_after_three_failures(tmp_path: Path) -> None:
    settings: dict[str, object] = {**SETTINGS, "record_blocks": {"hist": {"registry": "holding", "address": "0x1001", "records": 8, "words": 6}}}
    meter: FakeMeter = _meter()
    meter.records = {}                                  # no record answers
    result: StartupResult = read_startup_values(meter, _protocol(tmp_path, ROWS, settings))
    record_requests: list[tuple[int, int, Registry_Type]] = [r for r in meter.requests if r[0] >= 0x1001]
    assert len(record_requests) <= 3, "stops instead of timing out on every record"
    assert not result.complete
    assert result.values["pt"] == 10, "the ordinary rows are still captured"


def test_a_device_that_does_not_answer_stops_the_read_early(tmp_path: Path) -> None:
    meter: FakeMeter = FakeMeter(registers={}, records={})
    result: StartupResult = read_startup_values(meter, _protocol(tmp_path))
    assert result.gave_up and not result.complete and result.values == {}
    assert result.requests <= 3


def test_the_decoded_values_feed_context_formulas_in_later_poll_passes(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    volts: registry_map_entry = next(e for e in protocol.registry_map[Registry_Type.HOLDING] if e.variable_name == "volts")
    assert protocol.process_registery({0x20: 2301}, [volts])["volts"] == approx(230.1), "pt not known yet: raw"
    protocol.static_values = read_startup_values(_meter(), protocol).values
    assert protocol.process_registery({0x20: 2301}, [volts])["volts"] == approx(230.1 * 10)


# ---------------------------------------------------------------- shared code tables and configuration errors
def test_values_can_name_a_code_table_defined_once_in_the_json(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    names: list[str] = [k for k in protocol.codes if k.endswith("_codes") and k.startswith("history")]
    assert len(names) == 3
    assert all(protocol.codes[k] is protocol.codes["event_codes"] for k in names), "the same table is shared, not copied"


def test_an_unknown_code_table_name_is_ignored_not_fatal(tmp_path: Path) -> None:
    rows: list[list[str]] = [["0x0010", "a", "A", "", "USHORT", "@nope", "startup", "R", "", ""]]
    protocol: protocol_settings = _protocol(tmp_path, rows, {"transport": "modbus_rtu"})
    assert protocol.registry_map[Registry_Type.HOLDING][0].variable_name == "a"


@pytest.mark.parametrize(
    "bad",
    [
        {"hist": {"registry": "holding"}},
        {"hist": {"registry": "holding", "address": "0x1001", "records": 0, "words": 6}},
        {"hist": {"registry": "holding", "address": "0x1001", "records": 3, "words": 999}},
        {"hist": {"registry": "nonsense", "address": "0x1001", "records": 3, "words": 6}},
        {"hist": "not an object"},
        "not an object",
    ],
)
def test_invalid_record_blocks_are_ignored_not_fatal(tmp_path: Path, bad: object) -> None:
    rows: list[list[str]] = [row for row in ROWS if not row[1].startswith("hist_")]
    protocol: protocol_settings = _protocol(tmp_path, rows, {**SETTINGS, "record_blocks": bad})
    assert protocol.record_blocks == {}


# ---------------------------------------------------------------- the transport-side cache
def test_cache_stores_values_merges_without_overriding_and_reports_complete(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    cache = StartupCache(protocol, logging.getLogger("test"))
    assert cache.enabled
    result: StartupResult | None = cache.refresh(_meter(), now=1000.0)
    assert result is not None and result.complete
    assert protocol.static_values["pt"] == 10
    info: dict[str, int | float | str] = {"volts": 1.0, "pt": 77}
    cache.merge_into(info)
    assert info["pt"] == 77, "a value this cycle decoded itself wins"
    assert info["ct"] == 40 and info["hist_1_year"] == 2026
    assert not cache.retry_due(now=5000.0), "a complete read is not repeated"


def test_an_incomplete_read_is_retried_a_minute_later_a_limited_number_of_times(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    cache = StartupCache(protocol, logging.getLogger("test"))
    cache.refresh(FakeMeter({}, {}), now=1000.0)
    assert not cache.retry_due(now=1010.0), "too soon"
    assert cache.retry_due(now=1061.0)
    for attempt in range(StartupCache.MAX_ATTEMPTS - 1):
        cache.refresh(FakeMeter({}, {}), now=2000.0 + attempt * 100)
    assert not cache.retry_due(now=99999.0), "gives up after MAX_ATTEMPTS until the next connection"
    cache.reset()
    assert not cache.retry_due(now=99999.0)


def test_a_retry_keeps_what_an_earlier_attempt_captured(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path)
    cache = StartupCache(protocol, logging.getLogger("test"))
    cache.refresh(_meter(), now=1000.0)
    cache.refresh(FakeMeter({}, {}), now=2000.0)
    assert protocol.static_values["pt"] == 10


def test_a_protocol_without_startup_rows_is_untouched(tmp_path: Path) -> None:
    rows: list[list[str]] = [["0x0020", "a", "A", "", "USHORT", "", "", "R", "", ""]]
    protocol: protocol_settings = _protocol(tmp_path, rows, {"transport": "modbus_rtu"})
    cache = StartupCache(protocol, logging.getLogger("test"))
    assert not cache.enabled and cache.refresh(FakeMeter({}, {}), now=1.0) is None and not cache.retry_due(now=1e9)


# ---------------------------------------------------------------- devices that number registers by byte offset (register_address_step)
STEP_ROWS: list[list[str]] = [
    ["0x0010", "total", "Total", "0.1", "UINT", "", "startup", "R", "", ""],           # two words: addresses 0x10 and 0x12
    ["0x0014", "single", "Single", "", "USHORT", "", "startup", "R", "", ""],
    ["0x0030", "polled", "Polled", "", "USHORT", "", "", "R", "", ""],
    ["[rec_{i}_base+0]", "rec_{i}_energy", "Record {i} energy", "", "UINT", "", "startup", "R", "", ""],
    ["[rec_{i}_base+4]", "rec_{i}_flag", "Record {i} flag", "", "USHORT", "", "startup", "R", "", ""],
]
STEP_SETTINGS: dict[str, object] = {
    "transport": "modbus_rtu",
    "register_address_step": 2,
    "batch_size": 40,
    "record_blocks": {"rec": {"registry": "holding", "address": "0x2000", "records": 2, "words": 3, "address_step": 8}},
}


def test_startup_ranges_count_words_not_addresses_when_the_address_step_is_two(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path, STEP_ROWS, STEP_SETTINGS)
    entries: list[registry_map_entry] = startup_entries(protocol, Registry_Type.HOLDING)
    assert plain_ranges(protocol, Registry_Type.HOLDING, entries) == [(0x10, 3)], "addresses 0x10, 0x12, 0x14 are three words"


def test_words_are_placed_at_every_second_address_and_decode(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path, STEP_ROWS, STEP_SETTINGS)

    class StepMeter:
        def __init__(self) -> None:
            self.requests: list[tuple[int, int]] = []

        def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
            self.requests.append((start, count))
            if start == 0x10:
                return [0x0001, 0x0000, 0x0005][:count]            # total = 0x00010000 (words at 0x10, 0x12), single = 5 (0x14)
            if start == 0x2000:
                return [0x0000, 0x00FF, 0x0007]                    # record 1: energy = 0x000000FF, flag = 7
            if start == 0x2008:
                return [0x0000, 0x0010, 0x0009]                    # record 2: address_step 8 between records
            return None

    meter = StepMeter()
    values: dict[str, int | float | str] = read_startup_values(meter, protocol).values
    assert values["total"] == approx(0x00010000 * 0.1)
    assert values["single"] == 5
    assert values["rec_1_energy"] == 0x00FF and values["rec_1_flag"] == 7
    assert values["rec_2_energy"] == 0x0010 and values["rec_2_flag"] == 9
    assert [r for r in meter.requests if r[0] >= 0x2000] == [(0x2000, 3), (0x2008, 3)]


def test_the_polling_range_for_a_stepped_protocol_is_unchanged_by_startup_rows(tmp_path: Path) -> None:
    protocol: protocol_settings = _protocol(tmp_path, STEP_ROWS, STEP_SETTINGS)
    assert protocol.calculate_registry_ranges(protocol.registry_map[Registry_Type.HOLDING], 0x40, init=True) == [(0x30, 1)]


def test_a_record_that_does_not_fit_the_address_span_is_rejected_when_the_step_is_large(tmp_path: Path) -> None:
    settings: dict[str, object] = {**STEP_SETTINGS, "register_address_step": 2, "record_blocks": {"rec": {"registry": "holding", "address": "0x2000", "records": 2, "words": 200}}}
    protocol: protocol_settings = _protocol(tmp_path, STEP_ROWS, settings)
    assert protocol.record_blocks == {}, "200 words at 2 addresses each exceed the 256-address record span"
