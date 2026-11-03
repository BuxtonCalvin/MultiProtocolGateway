"""Every SunSpec protocol that uses dynamic discovery, checked against the layout its JSON defaults stand for.

For each map the defaults describe where the blocks normally sit.  The tests rebuild that layout as a SunSpec chain,
run discovery against it, and require the registry map to come out unchanged, then remove one block from the chain and
require that block's rows to leave the map.  Fronius additionally has an integer+scale-factor variant that replaces the
float rows when the inverter reports models 101-103.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest
from discovery_helpers import (
    FakeReader,
    discover_and_apply,
    holding,
    load,
    register_map,
    tiled_chain,
)

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)


@dataclass(frozen=True)
class MapSpec:
    """The default layout of one dynamic SunSpec protocol."""

    base: int
    """Address of the ``SunS`` marker."""
    headers: list[tuple[int, int]]
    """``(model_id, header_address)`` of each block the defaults place, in address order."""
    deferred: int = 0
    """Template rows that stay unresolved with the defaults (equipment variants that are absent by default)."""
    dropped: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    """Variables the default chain above does not provide, so discovery removes them."""


SPECS: dict[str, MapSpec] = {
    "solaredge_sunspec": MapSpec(40000, [(1, 40002), (103, 40069), (120, 40121), (121, 40149)]),
    "fronius_sunspec": MapSpec(40000, [(1, 40002), (113, 40069), (120, 40131), (121, 40162)], deferred=42),
    "apsystems_ecu_sunspec": MapSpec(40000, [(1, 40002), (113, 40069)]),
    "enphase_iq_gateway_sunspec": MapSpec(
        40000,
        [(1, 40002), (103, 40069), (201, 40121)],
        dropped=frozenset({"der_control_did", "der_power_limit_pct", "der_power_limit_enabled", "der_power_limit_enabled_desc"}),
    ),
    "abb_fimer_vsn300_sunspec": MapSpec(0, [(1, 2), (103, 70), (160, 122), (120, 172), (123, 232)]),
    "abb_fimer_react2_sunspec": MapSpec(
        40000,
        [(1, 40002), (101, 40070), (120, 40122), (121, 40150), (122, 40182), (123, 40228), (160, 41104), (65230, 41154), (65233, 41179), (65234, 41364)],
    ),
}

# (protocol, model that is missing from the chain, a variable that must leave the map with it)
MISSING_BLOCKS: list[tuple[str, int, str]] = [
    ("solaredge_sunspec", 121, "active_power_limit_pct"),
    ("fronius_sunspec", 121, "active_power_limit_pct"),
    ("apsystems_ecu_sunspec", 113, "ac_power"),
    ("enphase_iq_gateway_sunspec", 201, "meter_ac_power"),
    ("abb_fimer_vsn300_sunspec", 123, "power_limit_percent"),
    ("abb_fimer_react2_sunspec", 65233, "meter_w"),
]

PROTOCOLS: list[str] = list(SPECS)


@pytest.mark.parametrize("name", PROTOCOLS)
def test_the_protocol_declares_discovery_and_the_marker_address(name: str) -> None:
    proto: protocol_settings = load(name)
    assert proto.is_dynamic and proto.discovery_provider == "sunspec"
    assert proto.settings["sunspec_base_address"] == str(SPECS[name].base)


@pytest.mark.parametrize("name", PROTOCOLS)
def test_the_defaults_resolve_every_template_row_except_the_absent_variants(name: str) -> None:
    proto: protocol_settings = load(name)
    assert len(proto.dynamic_registry_rows) == SPECS[name].deferred


@pytest.mark.parametrize("name", PROTOCOLS)
def test_discovering_the_default_layout_leaves_the_map_as_it_was(name: str) -> None:
    spec: MapSpec = SPECS[name]
    proto: protocol_settings = load(name)
    before: dict[str, int] = register_map(proto)

    discover_and_apply(proto, FakeReader(tiled_chain(spec.base, spec.headers)))

    after: dict[str, int] = register_map(proto)
    assert {key: value for key, value in before.items() if key not in spec.dropped} == after
    assert not any(variable in after for variable in spec.dropped)


@pytest.mark.parametrize("name", PROTOCOLS)
def test_the_marker_is_read_at_the_declared_base_address(name: str) -> None:
    spec: MapSpec = SPECS[name]
    reader: FakeReader = FakeReader(tiled_chain(spec.base, spec.headers))
    discover_and_apply(load(name), reader)
    assert reader.reads[0][0] == spec.base


@pytest.mark.parametrize("name, missing_model, variable", MISSING_BLOCKS)
def test_a_block_the_device_does_not_report_leaves_the_map(name: str, missing_model: int, variable: str) -> None:
    spec: MapSpec = SPECS[name]
    proto: protocol_settings = load(name)
    assert variable in holding(proto)

    headers: list[tuple[int, int]] = [(model, address) for model, address in spec.headers if model != missing_model]
    discover_and_apply(proto, FakeReader(tiled_chain(spec.base, headers)))
    assert variable not in holding(proto)


def test_a_discovered_block_that_moved_is_read_at_its_new_address() -> None:
    proto: protocol_settings = load("abb_fimer_react2_sunspec")
    spec: MapSpec = SPECS["abb_fimer_react2_sunspec"]
    moved: list[tuple[int, int]] = [(model, address + 8 if model >= 160 else address) for model, address in spec.headers]
    before: int = holding(proto)["meter_w"].register
    discover_and_apply(proto, FakeReader(tiled_chain(spec.base, moved)))
    assert holding(proto)["meter_w"].register == before + 8


def test_a_single_mppt_module_removes_the_second_modules_rows() -> None:
    proto: protocol_settings = load("abb_fimer_vsn300_sunspec")
    headers: list[tuple[int, int]] = [(1, 2), (103, 70), (160, 122), (120, 152), (123, 212)]  # 160 now holds one module
    discover_and_apply(proto, FakeReader(tiled_chain(0, headers)))
    entries: dict[str, registry_map_entry] = holding(proto)
    assert "mppt1_current" in entries and "mppt2_current" not in entries
    assert entries["rated_power"].register == 152 + 3


# --------------------------------------------------------------------------------------------------
# Fronius: float and integer inverter models
# --------------------------------------------------------------------------------------------------

FRONIUS_INTEGER_CHAIN: list[tuple[int, int]] = [(1, 40002), (103, 40069), (120, 40121), (121, 40149)]


def test_fronius_in_float_mode_uses_the_float_layout() -> None:
    entries: dict[str, registry_map_entry] = holding(load("fronius_sunspec"))
    assert entries["ac_power"].register == 40091 and entries["ac_power"].data_type.name == "FLOAT32"


def test_fronius_in_integer_mode_swaps_in_the_integer_rows_at_the_shifted_addresses() -> None:
    proto: protocol_settings = load("fronius_sunspec")
    discover_and_apply(proto, FakeReader(tiled_chain(40000, FRONIUS_INTEGER_CHAIN)))
    entries: dict[str, registry_map_entry] = holding(proto)
    assert entries["ac_power"].register == 40083 and entries["ac_power"].data_type.name == "SHORT"
    assert entries["active_power_limit_pct"].register == 40149 + 3
    assert all(entry.dynamic for name, entry in entries.items() if name.startswith("ac_"))


def test_fronius_integer_mode_publishes_the_same_variable_names() -> None:
    float_names: set[str] = set(holding(load("fronius_sunspec")))
    proto: protocol_settings = load("fronius_sunspec")
    discover_and_apply(proto, FakeReader(tiled_chain(40000, FRONIUS_INTEGER_CHAIN)))
    integer_names: set[str] = set(holding(proto))
    missing: set[str] = {name for name in float_names if name.startswith(("ac_", "dc_", "inverter_", "operating_", "vendor_", "alarm_", "sunspec_inverter"))} - integer_names
    assert missing == set()


def test_fronius_integer_values_are_scaled_by_their_scale_factor_registers() -> None:
    proto: protocol_settings = load("fronius_sunspec")
    discover_and_apply(proto, FakeReader(tiled_chain(40000, FRONIUS_INTEGER_CHAIN)))
    entries: dict[str, registry_map_entry] = holding(proto)
    registers: dict[int, int] = {entries["ac_power"].register: 1500, entries["ac_power_sf"].register: 0xFFFF}  # sf = -1
    out = proto.process_registery(registers, proto.registry_map[Registry_Type.HOLDING])
    assert math.isclose(float(out["ac_power"]), 150.0)


def test_fronius_returns_to_float_rows_when_the_inverter_is_switched_back() -> None:
    proto: protocol_settings = load("fronius_sunspec")
    discover_and_apply(proto, FakeReader(tiled_chain(40000, FRONIUS_INTEGER_CHAIN)))
    discover_and_apply(proto, FakeReader(tiled_chain(40000, SPECS["fronius_sunspec"].headers)))
    assert holding(proto)["ac_power"].register == 40091


# --------------------------------------------------------------------------------------------------
# Maps that deliberately stay static
# --------------------------------------------------------------------------------------------------


def test_abb_native_sunspec_stays_static_because_its_covered_prefix_is_fixed() -> None:
    proto: protocol_settings = load("abb_fimer_sunspec")
    assert not proto.is_dynamic and proto.dynamic_registry_types == set()
    assert not any(entry.dynamic for entry in proto.registry_map[Registry_Type.HOLDING])
