"""Dynamic register discovery: template rows, anchors, the SunSpec provider and probes.

The fixtures model a SolarEdge device whose SunSpec chain is common(65) -> inverter 103(50) -> nameplate 120(26)
-> basic settings 121(30), which is the fixed layout the protocol CSV used before it became a template.  The
device is simulated by ``FakeReader``, a dict of register values that answers only the addresses it holds, so no
Modbus stack is needed.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from classes.discovery import (
    DiscoveryResult,
    SunSpecProvider,
    decode_ascii,
    parse_probes,
    run_discovery,
    run_probes,
)
from classes.discovery.probes import ProbeSpec
from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

BASE: int = 40000
SUNS: tuple[int, int] = (0x5375, 0x6E53)
DEFAULT_CHAIN: list[tuple[int, int]] = [(1, 65), (103, 50), (120, 26), (121, 30)]
"""The SolarEdge layout the JSON defaults describe: (model_id, length) in chain order."""
VERSION_ADDRESS: int = 40044
"""Wire address of the SunSpec common-model version string (the CSV ``version`` row)."""


class FakeReader:
    """A register image that implements ``DiscoveryReader``; unknown addresses read as a failure."""

    def __init__(self, words: dict[int, int] | None = None) -> None:
        """Start with ``words`` (address -> 16-bit value)."""
        self.words: dict[int, int] = dict(words or {})
        self.reads: list[tuple[int, int, Registry_Type]] = []

    def read_discovery_registers(self, start: int, count: int, registry_type: Registry_Type) -> list[int] | None:
        """Return ``count`` words from ``start`` or ``None`` if any is missing."""
        self.reads.append((start, count, registry_type))
        if any(start + offset not in self.words for offset in range(count)):
            return None
        return [self.words[start + offset] for offset in range(count)]


def chain_image(models: list[tuple[int, int]], base: int = BASE, end_marker: bool = True) -> dict[int, int]:
    """Build the register image of a SunSpec chain: marker, one header per model, then the end header."""
    words: dict[int, int] = {base: SUNS[0], base + 1: SUNS[1]}
    address: int = base + 2
    for model_id, length in models:
        words[address] = model_id
        words[address + 1] = length
        address += 2 + length
    if end_marker:
        words[address] = 0xFFFF
        words[address + 1] = 0
    return words


def ascii_words(text: str, count: int = 8) -> dict[int, int]:
    """Encode ``text`` as ``count`` ASCII registers starting at the version address (NUL padded)."""
    raw: bytes = text.encode("ascii").ljust(count * 2, b"\x00")
    return {VERSION_ADDRESS + index: (raw[2 * index] << 8) | raw[2 * index + 1] for index in range(count)}


def load(name: str) -> protocol_settings:
    """Load a protocol with logging silenced."""
    logging.disable(logging.CRITICAL)
    try:
        return protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)


def holding(proto: protocol_settings) -> dict[str, registry_map_entry]:
    """Return the HOLDING map keyed by variable name."""
    return {entry.variable_name: entry for entry in proto.registry_map[Registry_Type.HOLDING]}


def discover_and_apply(proto: protocol_settings, reader: FakeReader) -> bool:
    """Run discovery against ``reader`` and apply the result the way the transport does."""
    result: DiscoveryResult | None = run_discovery(reader, proto, logging.getLogger("test"))
    assert result is not None
    return proto.apply_anchors(result.anchors, result.owned_patterns)


# --------------------------------------------------------------------------------------------------
# Static protocols are untouched
# --------------------------------------------------------------------------------------------------


def test_static_protocol_carries_no_dynamic_state() -> None:
    proto: protocol_settings = load("eastron_eastron_sdm120m")
    assert proto.is_dynamic is False
    assert proto.anchors == {} and proto.dynamic_registry_types == set()
    assert not any(entry.dynamic for entries in proto.registry_map.values() for entry in entries)


def test_static_protocol_discovery_is_skipped_entirely() -> None:
    proto: protocol_settings = load("eastron_eastron_sdm120m")
    reader: FakeReader = FakeReader()
    assert run_discovery(reader, proto, logging.getLogger("test")) is None
    assert reader.reads == []


# --------------------------------------------------------------------------------------------------
# SolarEdge: defaults reproduce the old static layout
# --------------------------------------------------------------------------------------------------


def test_solaredge_defaults_reproduce_the_fixed_layout() -> None:
    entries: dict[str, registry_map_entry] = holding(load("solaredge_sunspec"))
    assert entries["ac_power"].register == 40083
    assert entries["rated_power"].register == 40124
    assert entries["active_power_limit_pct"].register == 40151
    assert entries["battery_1_soe"].register == 62852


def test_template_rows_are_flagged_dynamic_and_literal_rows_are_not() -> None:
    entries: dict[str, registry_map_entry] = holding(load("solaredge_sunspec"))
    assert entries["ac_power"].dynamic and entries["rated_power"].dynamic
    assert not entries["sunspec_id"].dynamic and not entries["battery_1_soe"].dynamic


def test_optional_equipment_is_absent_until_discovered() -> None:
    names: list[str] = list(holding(load("solaredge_sunspec")))
    assert not any(name.startswith(("meter_", "battery_legacy_")) for name in names)


def test_solaredge_declares_discovery_in_json() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    assert proto.is_dynamic and proto.discovery_provider == "sunspec"
    assert proto.anchors["inverter_1_base"] == 40069


# --------------------------------------------------------------------------------------------------
# Expression engine
# --------------------------------------------------------------------------------------------------


def test_expressions_substitute_names_inside_arithmetic() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    assert proto.evaluate_expressions("[base+14]", {"base": 40069}) == ["40083"]
    assert proto.evaluate_expressions("[1~count]", {"count": 3}) == ["1", "2", "3"]


def test_expressions_with_an_unknown_name_are_left_alone() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    assert proto.evaluate_expressions("[missing+14]", {"base": 1}) == ["[missing+14]"]


# --------------------------------------------------------------------------------------------------
# apply_anchors: instances, moved blocks, vanished blocks
# --------------------------------------------------------------------------------------------------


def test_each_discovered_meter_gets_its_own_instance_of_the_template() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader(chain_image([*DEFAULT_CHAIN, (203, 105), (203, 105)])))

    entries: dict[str, registry_map_entry] = holding(proto)
    first_meter: int = 40181  # header address right after model 121 (40149 + 2 + 30)
    assert entries["meter_1_ac_power"].register == first_meter + 18
    assert entries["meter_2_ac_power"].register == first_meter + 2 + 105 + 18
    assert entries["meter_1_ac_power"].dynamic and entries["meter_2_ac_power"].dynamic
    assert "meter_3_ac_power" not in entries


def test_instance_number_reaches_the_scale_factor_reference() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader(chain_image([*DEFAULT_CHAIN, (203, 105), (203, 105)])))
    entry: registry_map_entry = holding(proto)["meter_2_ac_power"]
    context: object = entry.adjustments["Context"]
    assert isinstance(context, dict) and context["key"] == "meter_2_ac_power_sf"
    assert "meter_2_ac_power_sf" in holding(proto)


def test_discovered_meter_values_are_published_in_real_units() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader(chain_image([*DEFAULT_CHAIN, (203, 105)])))
    entries: dict[str, registry_map_entry] = holding(proto)
    registers: dict[int, int] = {
        entries["meter_1_ac_power"].register: (-1234) & 0xFFFF,
        entries["meter_1_ac_power_sf"].register: 0,
    }
    out = proto.process_registery(registers, proto.registry_map[Registry_Type.HOLDING])
    assert out["meter_1_ac_power"] == -1234


def test_blocks_the_device_does_not_report_leave_the_map_even_though_defaults_list_them() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader(chain_image([(1, 65), (103, 50), (121, 30)])))

    entries: dict[str, registry_map_entry] = holding(proto)
    assert "rated_power" not in entries  # model 120 is gone
    assert entries["active_power_limit_pct"].register == 40121 + 2  # model 121 moved up
    assert entries["ac_power"].register == 40083


def test_a_moved_inverter_block_moves_its_registers_and_the_map_size() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    size_before: int = proto.registry_map_size[Registry_Type.HOLDING]
    discover_and_apply(proto, FakeReader(chain_image([(1, 65), (999, 10), (103, 50)])))
    entries: dict[str, registry_map_entry] = holding(proto)
    assert entries["ac_power"].register == 40083 + 12
    assert proto.registry_map_size[Registry_Type.HOLDING] == size_before


def test_applying_the_same_discovery_twice_is_a_no_op_the_second_time() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    reader: FakeReader = FakeReader(chain_image(DEFAULT_CHAIN))
    assert discover_and_apply(proto, reader) is True
    assert discover_and_apply(proto, reader) is False


def test_a_later_discovery_replaces_rather_than_accumulates() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader(chain_image([*DEFAULT_CHAIN, (203, 105), (203, 105)])))
    assert "meter_2_ac_power" in holding(proto)
    discover_and_apply(proto, FakeReader(chain_image([*DEFAULT_CHAIN, (203, 105)])))
    entries: dict[str, registry_map_entry] = holding(proto)
    assert "meter_1_ac_power" in entries and "meter_2_ac_power" not in entries


# --------------------------------------------------------------------------------------------------
# SunSpec provider
# --------------------------------------------------------------------------------------------------


def test_anchor_numbering_follows_chain_order_per_model_and_per_alias() -> None:
    anchors: dict[str, int] = SunSpecProvider().build_anchors(
        BASE, [(1, 40002, 65), (103, 40069, 50), (203, 40181, 105), (204, 40288, 105)]
    )
    assert anchors["model_203_1_base"] == 40181 and anchors["model_203_count"] == 1
    assert anchors["meter_1_base"] == 40181 and anchors["meter_2_base"] == 40288
    assert anchors["meter_2_model"] == 204 and anchors["meter_count"] == 2
    assert anchors["inverter_1_model"] == 103 and anchors["inverter_count"] == 1
    assert anchors["sunspec_base"] == BASE


def test_owned_pattern_covers_what_the_provider_emits_and_nothing_else() -> None:
    pattern = SunSpecProvider.owned_pattern()
    emitted: dict[str, int] = SunSpecProvider().build_anchors(BASE, [(103, 40069, 50), (111, 40200, 60)])
    assert all(pattern.match(name) for name in emitted)
    assert not pattern.match("battery_legacy_count") and not pattern.match("battery_legacy_1_base")


def test_provider_walks_the_chain_to_the_end_header() -> None:
    result: DiscoveryResult | None = SunSpecProvider().discover(FakeReader(chain_image(DEFAULT_CHAIN)), load("solaredge_sunspec"))
    assert result is not None
    assert result.anchors["model_120_1_base"] == 40121 and result.anchors["model_121_1_base"] == 40149
    assert "model_203_count" not in result.anchors


def test_provider_returns_none_when_there_is_no_marker() -> None:
    assert SunSpecProvider().discover(FakeReader(), load("solaredge_sunspec")) is None


def test_provider_keeps_the_models_found_when_the_chain_is_cut_short() -> None:
    image: dict[int, int] = chain_image(DEFAULT_CHAIN, end_marker=False)
    result: DiscoveryResult | None = SunSpecProvider().discover(FakeReader(image), load("solaredge_sunspec"))
    assert result is not None and result.anchors["model_121_count"] == 1


def test_provider_tries_the_standard_marker_addresses_when_none_is_configured() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    proto.settings.pop("sunspec_base_address")
    result: DiscoveryResult | None = SunSpecProvider().discover(FakeReader(chain_image([(1, 65), (103, 50)], base=50000)), proto)
    assert result is not None and result.anchors["sunspec_base"] == 50000


def test_provider_stops_at_the_model_limit_on_a_corrupt_chain() -> None:
    looping: dict[int, int] = {BASE: SUNS[0], BASE + 1: SUNS[1]}
    address: int = BASE + 2
    for _ in range(SunSpecProvider.MAX_MODELS + 10):
        looping[address], looping[address + 1] = 103, 0
        address += 2
    result: DiscoveryResult | None = SunSpecProvider().discover(FakeReader(looping), load("solaredge_sunspec"))
    assert result is not None and result.anchors["model_103_count"] == SunSpecProvider.MAX_MODELS


# --------------------------------------------------------------------------------------------------
# Probes and the early StorEdge battery block
# --------------------------------------------------------------------------------------------------


def test_ascii_decoding_drops_padding() -> None:
    assert decode_ascii([0x3030, 0x3033, 0x2E32, 0x0000]) == "0003.2"


def test_malformed_probes_are_skipped_without_stopping_the_good_ones() -> None:
    raw: list[object] = [
        {"anchor": "ok", "address": "0x9C6C", "prefix": "A"},
        {"anchor": "no_prefix", "address": 1},
        {"address": 1, "prefix": "A"},
        {"anchor": "bad_registry", "address": 1, "prefix": "A", "registry": "nope"},
        "not an object",
    ]
    probes: list[ProbeSpec] = parse_probes(raw, logging.getLogger("test"))
    assert [probe.anchor for probe in probes] == ["ok"] and probes[0].address == 0x9C6C and probes[0].count == 8


def test_probe_sets_value_on_match_else_value_on_mismatch_and_nothing_when_unreadable() -> None:
    raw: list[object] = [{"anchor": "legacy", "address": VERSION_ADDRESS, "count": 8, "prefix": "0003", "value": 3}]
    probes: list[ProbeSpec] = parse_probes(raw, logging.getLogger("test"))
    log: logging.Logger = logging.getLogger("test")
    assert run_probes(FakeReader(ascii_words("0003.2519")), probes, log) == {"legacy": 3}
    assert run_probes(FakeReader(ascii_words("0004.0016")), probes, log) == {"legacy": 0}
    assert run_probes(FakeReader(), probes, log) == {}


@pytest.mark.parametrize("firmware, batteries", [("0003.2519", 3), ("0004.0016", 0)])
def test_legacy_battery_rows_follow_the_firmware_probe(firmware: str, batteries: int) -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader({**chain_image(DEFAULT_CHAIN), **ascii_words(firmware)}))

    entries: dict[str, registry_map_entry] = holding(proto)
    found: list[str] = sorted({name.split("_voltage")[0] for name in entries if name.startswith("battery_legacy_") and name.endswith("_voltage")})
    assert len(found) == batteries
    if batteries:
        assert entries["battery_legacy_1_voltage"].register == 57600 + 112
        assert entries["battery_legacy_2_voltage"].register == 57856 + 112
        assert entries["battery_legacy_3_voltage"].register == 58368 + 112
        assert entries["battery_legacy_1_voltage"].data_type is Data_Type.FLOAT32


def test_legacy_battery_float_is_decoded_word_swapped() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader({**chain_image(DEFAULT_CHAIN), **ascii_words("0003.2519")}))
    voltage: registry_map_entry = holding(proto)["battery_legacy_1_voltage"]
    # 52.5 V is 0x42520000; word-swapped little-endian puts the low word first.
    registers: dict[int, int] = {voltage.register: 0x0000, voltage.register + 1: 0x4252}
    out: dict[str, int | float | str] = proto.process_registery(registers, [voltage])
    assert math.isclose(float(out["battery_legacy_1_voltage"]), 52.5)


def test_the_static_battery_rows_are_untouched_by_the_legacy_block() -> None:
    proto: protocol_settings = load("solaredge_sunspec")
    discover_and_apply(proto, FakeReader({**chain_image(DEFAULT_CHAIN), **ascii_words("0003.2519")}))
    entries: dict[str, registry_map_entry] = holding(proto)
    assert entries["battery_1_soe"].register == 62852 and not entries["battery_1_soe"].dynamic


# --------------------------------------------------------------------------------------------------
# Deferred expressions (resolve_dynamic_registry_entries)
# --------------------------------------------------------------------------------------------------

CSV_HEADER: str = "register,variable_name,documented_name,unit,data_type,values,read_interval,writable,adjustments,note\n"


def test_deferred_rows_become_entries_once_their_values_are_known(tmp_path: Path) -> None:
    (tmp_path / "dyn_test.json").write_text("{}")
    (tmp_path / "dyn_test.holding_registry_map.csv").write_text(
        CSV_HEADER + "[block_a+2],dyn_a,Dyn A,,uint16,,,R,,\n" + "10,static_b,Static B,,uint16,,,R,,\n"
    )
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings("dyn_test", settings_dir=str(tmp_path))
    finally:
        logging.disable(logging.NOTSET)

    assert [entry.variable_name for entry in proto.registry_map[Registry_Type.HOLDING]] == ["static_b"]
    assert len(proto.dynamic_registry_rows) == 1

    proto.resolve_dynamic_registry_entries({"unrelated": 1})
    assert len(proto.dynamic_registry_rows) == 1

    proto.resolve_dynamic_registry_entries({"block_a": 100})
    entries: dict[str, registry_map_entry] = holding(proto)
    assert entries["dyn_a"].register == 102 and entries["dyn_a"].dynamic
    assert proto.dynamic_registry_rows == []
    assert [entry.variable_name for entry in proto.registry_map[Registry_Type.HOLDING]].count("static_b") == 1
