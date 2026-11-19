"""Acrel DTSD1352-C (``acrel_dtsd1352_c``) protocol map checks.

Everything is read with function 03.  Voltage, current, power and energy are raw integers that the
manual scales by the PT and CT ratio registers (0x008D, 0x008E), so those rows use ``Context``
adjustments driven by the ratio rows and the protocol's ``context_variables``.  4-byte values are
read high word first (the app default); the manual does not state the order, so it is taken from
AggSoft's DTSD1352 map (big-endian dwords) and should be confirmed on a meter.
"""

from __future__ import annotations

import csv
import logging
import math
import shutil
from pathlib import Path

import pytest

from classes.discovery.startup import read_startup_values
from classes.protocol_settings import (
    VIRTUAL_REGISTER_BASE,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

PROTOCOL: str = "acrel_dtsd1352_c"


def _load() -> protocol_settings:
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(PROTOCOL)
    finally:
        logging.disable(logging.NOTSET)


def _entries(protocol: protocol_settings) -> dict[str, registry_map_entry]:
    return {entry.variable_name: entry for entry in protocol.registry_map[Registry_Type.HOLDING]}


def _decode(raw: dict[str, bytes]) -> dict[str, float | int | str]:
    """Decode ``raw`` (variable name -> the register bytes at its start address) in one polling pass."""
    protocol: protocol_settings = _load()
    entries: dict[str, registry_map_entry] = _entries(protocol)
    return protocol.process_registery({entries[n].register: v for n, v in raw.items()}, [entries[n] for n in raw])


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _u32(value: int, signed: bool = False) -> bytes:
    return value.to_bytes(4, "big", signed=signed)


def _num(value: float | int | str) -> float:
    assert isinstance(value, (int, float)), value
    return float(value)


def test_json_defaults_and_shared_scale() -> None:
    protocol: protocol_settings = _load()
    assert protocol.settings.get("baud") == "9600"
    assert protocol.settings.get("byteorder", "big") == "big"
    assert protocol.settings.get("send_holding_register") == "true"
    assert protocol.settings.get("send_input_register") == "false"
    assert protocol.codes["context_variables"] == {"power_scale": "{pt_ratio}*{ct_ratio}"}


def test_no_endian_overrides_and_unique_names() -> None:
    protocol: protocol_settings = _load()
    rows = protocol.registry_map[Registry_Type.HOLDING]
    assert not [r.variable_name for r in rows if "Register_Endian" in r.adjustments]
    names: list[str] = [r.variable_name for r in rows]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("variable", "address"),
    [
        ("total_active_energy", 0x0000), ("forward_active_energy", 0x000A), ("reverse_active_energy", 0x0014),
        ("total_reactive_energy", 0x001E), ("reverse_reactive_energy_valley", 0x003A),
        ("voltage_l1", 0x0061), ("current_l3", 0x0066), ("frequency", 0x0077),
        ("voltage_l1_l2", 0x0078), ("pt_ratio", 0x008D), ("ct_ratio", 0x008E),
        ("forward_active_energy_l1", 0x0087), ("forward_active_energy_l3", 0x008B),
        ("power_l1", 0x0164), ("power_total", 0x016A), ("reactive_power_total", 0x0172),
        ("apparent_power_total", 0x017A), ("power_factor_l1", 0x017C), ("power_factor_total", 0x017F),
        ("max_voltage_l1", 0x0200), ("max_active_power_total", 0x021E), ("min_voltage_l1", 0x0242),
        # The manual prints 027EH for the Min apparent power of phase C; 027CH continues the pitch (total at 0280H).
        ("min_apparent_power_l3", 0x027C), ("min_apparent_power_total", 0x0280),
        ("temperature_t3", 0x2002),
        ("harmonic_u_l1_2", 0x05E3), ("harmonic_u_l2_2", 0x0601), ("harmonic_i_l1_2", 0x063D), ("harmonic_i_l3_31", 0x0696),
        ("fundamental_voltage_l1", 0x0697), ("harmonic_reactive_power_total", 0x06B2),
    ],
)
def test_register_addresses(variable: str, address: int) -> None:
    assert _entries(_load())[variable].register == address


def test_energy_is_high_word_first_and_scaled_by_pt_and_ct() -> None:
    """E = data*PT*CT*0.01 kWh, 32-bit unsigned, high word first."""
    direct = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "total_active_energy": _u32(123456)})
    assert math.isclose(_num(direct["total_active_energy"]), 1234.56)
    ct_meter = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(40), "total_active_energy": _u32(123456)})
    assert math.isclose(_num(ct_meter["total_active_energy"]), 1234.56 * 40)
    word_order = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "total_active_energy": bytes.fromhex("0001E240")})
    assert math.isclose(_num(word_order["total_active_energy"]), 1234.56), "registers 0x0000 = high word 0x0001, 0x0001 = low word 0xE240"


def test_voltage_and_current_use_their_own_ratio() -> None:
    out = _decode({"pt_ratio": _u16(10), "ct_ratio": _u16(40), "voltage_l1": _u16(2301), "current_l1": _u16(199)})
    assert math.isclose(_num(out["voltage_l1"]), 2301 * 0.1 * 10)            # U = data*PT*0.1
    assert math.isclose(_num(out["current_l1"]), 199 * 0.01 * 40)            # I = data*CT*0.01


def test_power_is_signed_and_scaled_by_pt_times_ct() -> None:
    export = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(40), "power_total": _u32(-2500, signed=True)})
    assert math.isclose(_num(export["power_total"]), -2.5 * 40)              # PQS = data*PT*CT*0.001, negative = export
    reactive = _decode({"pt_ratio": _u16(1), "ct_ratio": _u16(1), "reactive_power_l2": _u32(-1000, signed=True)})
    assert math.isclose(_num(reactive["reactive_power_l2"]), -1.0)


def test_power_factor_frequency_and_unbalance() -> None:
    out = _decode({"power_factor_total": _u16(0xFC4A), "frequency": _u16(4998), "voltage_unbalance": _u16(15)})
    assert math.isclose(_num(out["power_factor_total"]), -0.950)            # signed: 0xFC4A = -950
    assert math.isclose(_num(out["frequency"]), 49.98)
    assert math.isclose(_num(out["voltage_unbalance"]), 1.5)


def test_values_stay_raw_when_the_ratio_rows_were_not_read() -> None:
    out = _decode({"voltage_l1": _u16(2301)})
    assert math.isclose(_num(out["voltage_l1"]), 230.1)


def test_byte_packed_settings_decode_from_integer_words() -> None:
    """Register 0x003F: high byte = address, low byte = baud code.  A Modbus transport delivers integer words."""
    protocol: protocol_settings = _load()
    entries: dict[str, registry_map_entry] = _entries(protocol)
    out = protocol.process_registery({0x3F: 0x0102}, [entries["communication_address_1"], entries["baud_rate_1"]])
    assert out["communication_address_1"] == 1, "high byte"
    assert out["baud_rate_1"] == 2, "low byte"
    parity = protocol.process_registery({0x95: 0x0200}, [entries["parity_1"], entries["stop_bits_1"]])
    assert parity["parity_1"] == 2 and parity["stop_bits_1"] == 0


def test_byte_packed_settings_decode_the_same_from_bytes() -> None:
    protocol: protocol_settings = _load()
    entries: dict[str, registry_map_entry] = _entries(protocol)
    out = protocol.process_registery({0x3F: bytes.fromhex("0102")}, [entries["communication_address_1"], entries["baud_rate_1"]])
    assert out["communication_address_1"] == 1 and out["baud_rate_1"] == 2


def test_byte_fields_use_byte_offsets() -> None:
    """``.0`` is the high byte of a register and ``.1`` the low byte, on Modbus integer words and on raw bytes alike."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("communication_address_1", "baud_rate_1", "parity_1", "stop_bits_1", "running_state_1", "clock_second"):
        assert entries[name].data_type.name == "BYTE", name
    assert entries["communication_address_1"].register_byte == 0 and entries["baud_rate_1"].register_byte == 1
    assert entries["parity_1"].register_byte == 0 and entries["stop_bits_1"].register_byte == 1


def test_ratio_rows_and_static_settings_are_read_once_at_startup() -> None:
    """The ratios, communication settings and pulse constant rarely change: read once at connect, not polled."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("pt_ratio", "ct_ratio", "communication_address_1", "baud_rate_1", "parity_1", "stop_bits_1", "pulse_constant"):
        assert entries[name].read_at_startup, name
        assert entries[name].write_mode.name in ("READ", "WRITE"), name
    for name in ("voltage_l1", "power_total", "total_active_energy"):
        assert not entries[name].read_at_startup, name


def test_optional_and_unverified_rows_are_read_disabled() -> None:
    """Harmonics (CH option), Max/Min, demand, tariff energies, temperature and the clock are not polled by default."""
    entries: dict[str, registry_map_entry] = _entries(_load())
    optional: list[str] = [
        name for name in entries
        if not name.endswith("_desc") and (
            "harmonic" in name or name.startswith(("max_", "min_", "thd_", "temperature_", "clock_", "fundamental_"))
            or name.endswith(("_sharp", "_peak", "_flat", "_valley")) or "demand" in name
        )
    ]
    assert optional
    for name in optional:
        assert entries[name].write_mode.name == "READDISABLED", name


def test_core_measurements_are_polled() -> None:
    entries: dict[str, registry_map_entry] = _entries(_load())
    for name in ("voltage_l1", "current_l1", "power_total", "reactive_power_total", "apparent_power_total", "power_factor_total",
                 "frequency", "total_active_energy", "forward_active_energy", "reverse_active_energy", "forward_active_energy_l1"):
        assert entries[name].write_mode.name == "READ", name


# ================================================================ record blocks, event log, reserved registers
PROTOCOLS_DIR: Path = Path(__file__).resolve().parent.parent / "protocols"


def _protocol_with_enabled(tmp_path: Path, prefixes: tuple[str, ...]) -> protocol_settings:
    """A copy of the protocol whose rows named ``<prefix>...`` are read-enabled (they ship Read Disabled)."""
    for source in (PROTOCOLS_DIR / "acrel").glob(f"{PROTOCOL}.*"):
        shutil.copy(source, tmp_path / source.name)
    path: Path = tmp_path / f"{PROTOCOL}.holding_registry_map.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows: list[dict[str, str]] = list(csv.DictReader(handle))
    for row in rows:
        if row["variable_name"].startswith(prefixes):
            row["writable"] = "R"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(PROTOCOL, settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)


class FakeAcrel:
    """Answers plain reads from a register dict and record reads only at exact record handles and sizes."""

    def __init__(self, registers: dict[int, int], records: dict[int, list[int]]) -> None:
        self.registers = registers
        self.records = records
        self.requests: list[tuple[int, int]] = []

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        self.requests.append((start, count))
        if start in self.records:
            words = self.records[start]
            return list(words) if count == len(words) else None
        if all(address in self.registers for address in range(start, start + count)):
            return [self.registers[start + offset] for offset in range(count)]
        return None


def test_the_three_record_blocks_are_declared() -> None:
    blocks = _load().record_blocks
    assert {name: (b.address, b.records, b.words, b.address_step) for name, b in blocks.items()} == {
        "history_month": (0x1001, 48, 60, 1),
        "history_day": (0x1101, 90, 60, 1),
        "event": (0x3001, 100, 6, 1),
    }


def test_every_record_is_addressed_by_its_own_handle() -> None:
    blocks = _load().record_blocks
    assert blocks["history_month"].handle(1) == 0x1001 and blocks["history_month"].handle(48) == 0x1030
    assert blocks["history_day"].handle(1) == 0x1101 and blocks["history_day"].handle(90) == 0x115A
    assert blocks["event"].handle(1) == 0x3001 and blocks["event"].handle(100) == 0x3064


def _fields(protocol: protocol_settings, block: str) -> dict[str, tuple[int, str]]:
    """{field name without the record number: (word offset in the record, data type)} for record 1 of ``block``."""
    blocks = protocol.record_blocks
    fields: dict[str, tuple[int, str]] = {}
    for entry in protocol.registry_map[Registry_Type.HOLDING]:
        if entry.register >= VIRTUAL_REGISTER_BASE and entry.variable_name.startswith(f"{block}_1_") and not entry.variable_name.endswith("_desc"):
            fields[entry.variable_name.removeprefix(f"{block}_1_")] = (entry.register - blocks[block].virtual_base(1), entry.data_type.name)
    return fields


def test_the_month_and_day_records_have_an_identical_layout() -> None:
    protocol = _load()
    assert _fields(protocol, "history_month") == _fields(protocol, "history_day")
    assert len(_fields(protocol, "history_month")) > 40


def test_history_record_layout_matches_the_manual() -> None:
    fields = _fields(_load(), "history_month")
    expected = {
        "year": 0x00, "month": 0x00, "day": 0x01, "hour": 0x01,
        "forward_active_energy": 0x02, "forward_active_energy_sharp": 0x04, "forward_active_energy_valley": 0x0A,
        "reverse_active_energy": 0x0C, "reverse_active_energy_valley": 0x14,
        "forward_reactive_energy": 0x16, "forward_reactive_energy_valley": 0x1E,
        "reverse_reactive_energy": 0x20, "reverse_reactive_energy_valley": 0x28,
        "forward_active_energy_l1": 0x2A, "forward_active_energy_l3": 0x2E,
        "max_forward_active_demand": 0x30, "max_forward_active_demand_minute": 0x31, "max_forward_active_demand_day": 0x32,
        "max_reverse_active_demand": 0x33, "max_forward_reactive_demand": 0x36, "max_reverse_reactive_demand": 0x39,
        "max_reverse_reactive_demand_hour": 0x3A, "max_reverse_reactive_demand_month": 0x3B,
    }
    for name, offset in expected.items():
        assert fields[name][0] == offset, name


def test_every_history_field_fits_inside_the_120_byte_record() -> None:
    protocol = _load()
    for block in ("history_month", "history_day"):
        for name, (offset, data_type) in _fields(protocol, block).items():
            words = 2 if data_type == "UINT" else 1
            assert offset + words <= 60, (block, name)


def test_event_record_layout_follows_the_manuals_worked_example() -> None:
    """The example returns 12 bytes: date, time, time, event number, details, reserved."""
    fields = _fields(_load(), "event")
    assert fields["year"][0] == 0 and fields["month"][0] == 0 and fields["day"][0] == 1 and fields["hour"][0] == 1
    assert fields["minute"][0] == 2 and fields["second"][0] == 2
    assert fields["number"][0] == 3 and fields["details"][0] == 4
    assert max(offset for offset, _t in fields.values()) < 6


def test_event_numbers_come_from_one_table_in_the_json() -> None:
    protocol = _load()
    table = protocol.codes["event_numbers"]
    assert isinstance(table, dict) and table["256"] == "Power on" and table["1024"] == "UI record"
    names = [k for k in protocol.codes if k.startswith("event_") and k.endswith("number_codes")]
    assert len(names) == 100, "one entry per record"
    assert all(protocol.codes[k] is table for k in names), "shared, not copied"


def test_record_fields_ship_read_disabled_and_are_never_polled() -> None:
    protocol = _load()
    fields = [e for e in protocol.registry_map[Registry_Type.HOLDING] if e.register >= VIRTUAL_REGISTER_BASE and not e.variable_name.endswith("_desc")]
    assert len(fields) > 7000
    assert all(e.write_mode.name == "READDISABLED" and e.read_at_startup for e in fields)
    size = protocol.registry_map_size[Registry_Type.HOLDING]
    assert size < 0x3000, "record fields do not inflate the polling map"
    assert all(start < VIRTUAL_REGISTER_BASE for start, _count in protocol.calculate_registry_ranges(protocol.registry_map[Registry_Type.HOLDING], size, init=True))


def test_no_mapped_register_is_on_a_reserved_address() -> None:
    protocol = _load()
    on_reserved = [
        e.variable_name
        for e in protocol.registry_map[Registry_Type.HOLDING]
        if e.register < VIRTUAL_REGISTER_BASE
        and protocol.is_unreadable(Registry_Type.HOLDING, e.register, e.register + protocol.entry_word_count(e) - 1)
    ]
    assert on_reserved == []


def test_polling_requests_never_span_a_reserved_range() -> None:
    protocol = _load()
    size = protocol.registry_map_size[Registry_Type.HOLDING]
    ranges = protocol.calculate_registry_ranges(protocol.registry_map[Registry_Type.HOLDING], size, init=True)
    assert ranges
    for start, count in ranges:
        assert not protocol.is_unreadable(Registry_Type.HOLDING, start, start + count - 1), (hex(start), count)
    # the voltage/current window stops before the reserved 0x0067-0x0076 and frequency (0x0077) is its own request
    assert (0x61, 6) in ranges and (0x77, 1) in ranges


@pytest.mark.parametrize("address", [0x67, 0x76, 0x90, 0x98, 0xB1, 0xC4, 0x120, 0x123, 0x163, 0x19C, 0x1FF, 0x285, 0x5DC, 0x6B3, 0x6FF, 0x72A, 0x1FFF])
def test_manual_reserved_registers_are_unreadable(address: int) -> None:
    assert _load().is_unreadable(Registry_Type.HOLDING, address)


@pytest.mark.parametrize("address", [0x66, 0x77, 0x91, 0x121, 0x122, 0x164, 0x5DD, 0x6B2, 0x700, 0x2000])
def test_documented_registers_next_to_reserved_ranges_stay_readable(address: int) -> None:
    assert not _load().is_unreadable(Registry_Type.HOLDING, address)


def test_startup_read_decodes_a_month_record_and_an_event(tmp_path: Path) -> None:
    protocol = _protocol_with_enabled(tmp_path, ("history_month_{i}_", "event_{i}_"))
    month = [0] * 60
    month[0x00], month[0x01] = 0x1A0B, 0x0C0D                     # 2026-11, day 12, hour 13
    month[0x02], month[0x03] = 0x0001, 0x86A0                      # total forward active energy = 100000 -> 1000.00 kWh at PT*CT=1
    month[0x30], month[0x31], month[0x32] = 1500, 0x1E0A, 0x1C0B   # max forward active demand 1.5; 10:30 on 28/11 (minute-hour, day-month)
    meter = FakeAcrel(
        registers={0x8D: 1, 0x8E: 40, 0x3F: 0x0102, 0x40: 6400, 0x95: 0x0000},
        records={0x1001: month, 0x3001: [0x1201, 0x080A, 0x0101, 0x0100, 0x0000, 0x0000]},
    )
    result = read_startup_values(meter, protocol)
    values = result.values
    assert values["pt_ratio"] == 1 and values["ct_ratio"] == 40
    assert values["communication_address_1"] == 1 and values["baud_rate_1"] == 2
    assert values["history_month_1_year"] == 2026 and values["history_month_1_month"] == 11
    assert values["history_month_1_day"] == 12 and values["history_month_1_hour"] == 13
    assert math.isclose(float(values["history_month_1_forward_active_energy"]), 1000.00 * 40), "scaled by the cached CT ratio"
    assert math.isclose(float(values["history_month_1_max_forward_active_demand"]), 1.5 * 40)
    assert values["history_month_1_max_forward_active_demand_minute"] == 30 and values["history_month_1_max_forward_active_demand_hour"] == 10
    assert values["history_month_1_max_forward_active_demand_day"] == 28 and values["history_month_1_max_forward_active_demand_month"] == 11
    # the manual's own example: 2018-01-08 10:01:01, event 0x0100
    assert [values[f"event_1_{n}"] for n in ("year", "month", "day", "hour", "minute", "second")] == [2018, 1, 8, 10, 1, 1]
    assert values["event_1_number"] == 256 and values["event_1_number_desc"] == "Power on"
    assert values["event_1_details"] == 0


def test_only_whole_records_are_requested_and_only_for_enabled_rows(tmp_path: Path) -> None:
    protocol = _protocol_with_enabled(tmp_path, ("history_month_{i}_year", "event_{i}_number"))
    meter = FakeAcrel(registers={0x8D: 1, 0x8E: 1, 0x3F: 0, 0x40: 0, 0x95: 0}, records={})
    read_startup_values(meter, protocol)
    record_requests = sorted(r for r in meter.requests if r[0] >= 0x1001)
    assert {count for start, count in record_requests if start < 0x3001} <= {60}, "a history record is always requested whole"
    assert {count for start, count in record_requests if start >= 0x3001} <= {6}, "an event record is always requested whole"
    assert not any(0x1101 <= start <= 0x115A for start, _c in record_requests), "daily records: no enabled field, no request"


def test_a_basic_model_without_history_stops_after_three_unanswered_records(tmp_path: Path) -> None:
    protocol = _protocol_with_enabled(tmp_path, ("history_month_{i}_",))
    meter = FakeAcrel(registers={0x8D: 1, 0x8E: 1, 0x3F: 0, 0x40: 0, 0x95: 0}, records={})
    result = read_startup_values(meter, protocol)
    assert len([r for r in meter.requests if 0x1001 <= r[0] <= 0x1030]) <= 3
    assert not result.complete and result.values["pt_ratio"] == 1


def test_cached_ratios_scale_polled_rows_in_later_passes(tmp_path: Path) -> None:
    protocol = _load()
    voltage = _entries(protocol)["voltage_l1"]
    assert math.isclose(float(protocol.process_registery({0x61: 2301}, [voltage])["voltage_l1"]), 230.1)
    meter = FakeAcrel(registers={0x8D: 10, 0x8E: 40, 0x3F: 0, 0x40: 0, 0x95: 0}, records={})
    protocol.static_values = read_startup_values(meter, protocol).values
    assert math.isclose(float(protocol.process_registery({0x61: 2301}, [voltage])["voltage_l1"]), 2301.0)
