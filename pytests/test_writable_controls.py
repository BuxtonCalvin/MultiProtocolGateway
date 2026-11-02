"""Vendor read/write registers are marked writable in the maps that record them.

The ``writable`` column records what the manufacturer's specification allows; the app's gates
(transport ``write_enabled`` and the per-register toggle in the web UI) decide what is actually
written, so leaving a documented control read-only in the CSV only hides it from users.
"""

from __future__ import annotations

import logging

import pytest

from classes.protocol_settings import (
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)


def _writable(name: str) -> dict[str, registry_map_entry]:
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)
    return {
        e.variable_name: e
        for e in proto.registry_map[Registry_Type.HOLDING]
        if e.write_mode is WriteMode.WRITE and not e.variable_name.endswith("_desc")
    }


def test_a_tronix_settings_are_writable_with_the_vendor_ranges() -> None:
    entries: dict[str, registry_map_entry] = _writable("a_tronix")
    assert {25000, 25100, 25103, 50000, 50001, 52500, 52501, 53006} <= {e.register for e in entries.values()}
    assert (entries["offgrid_frequency_setting"].value_min, entries["offgrid_frequency_setting"].value_max) == (4500, 6500)
    assert (entries["ongrid_battery_end_soc"].value_min, entries["ongrid_battery_end_soc"].value_max) == (0, 1000)
    assert "inverter_sn" not in entries and "battery_soc" not in entries      # telemetry stays read-only


def test_eastron_configuration_registers_are_writable() -> None:
    entries: dict[str, registry_map_entry] = _writable("eastron_eastron_sdm120m")
    assert set(entries) == {"demand_period", "relay_pulse_width", "network_parity_stop", "meter_id", "baud_rate", "ct1_primary", "ct2_secondary"}
    assert (entries["meter_id"].value_min, entries["meter_id"].value_max) == (1, 247)
    assert (entries["demand_period"].value_min, entries["demand_period"].value_max) == (0, 60)
    assert "demand_time" not in entries


@pytest.mark.parametrize("name", ["a_tronix", "eastron_eastron_sdm120m", "abb_fimer_sunspec"])
def test_every_writable_row_is_in_the_holding_map(name: str) -> None:
    """Write-back targets holding registers, so no input-register row may carry a write flag."""
    logging.disable(logging.CRITICAL)
    try:
        proto: protocol_settings = protocol_settings(name)
    finally:
        logging.disable(logging.NOTSET)
    assert all(e.write_mode is not WriteMode.WRITE for e in proto.registry_map.get(Registry_Type.INPUT, []))
