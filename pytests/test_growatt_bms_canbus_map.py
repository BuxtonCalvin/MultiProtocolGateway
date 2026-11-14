# Description: Decode tests for the Growatt low-voltage BMS CAN protocol map (growatt_bms_canbus_v1.04).
# File: test_growatt_bms_canbus_map.py
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
Frames below follow the "Growatt BMS CAN-Bus-protocol low-voltage Rev 04 (V1.04)" document: the CAN
frame is big-endian, 0x311 carries a 16-bit status word in bytes 6-7, and the protection / alarm
tables are single bytes (table 1 in byte 0, table 2 in byte 1, ...).  The 0x311 / 0x313 sample frames
are the ones the Battery-Emulator project verified against real captures.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator
from typing import Any

import can
import pytest
from conftest import DummySettings, approx

from classes.transports.canbus import canbus

_ids: Iterator[int] = itertools.count()


@pytest.fixture
def bus() -> Iterator[tuple[canbus, can.BusABC]]:
    channel: str = f"pytest_growatt_{next(_ids)}"
    transport: canbus = canbus(
        DummySettings(
            protocol_version="growatt_bms_canbus_v1.04",
            port=channel,
            interface="virtual",
            baudrate=500000,
            serial_number="TEST",
            bridge="transport.mqtt",
        )
    )
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    yield transport, peer
    peer.shutdown()
    if transport.bus is not None:
        transport.bus.shutdown()


def _decode(bus: tuple[canbus, can.BusABC], can_id: int, data: list[int]) -> dict[str, Any]:
    transport, peer = bus
    peer.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=False))
    deadline: float = time.monotonic() + 3
    while time.monotonic() < deadline:
        if transport.cache is not None and can_id in transport.cache:
            break
        time.sleep(0.02)
    return transport.read_data()


def test_0x311_limits_and_enable_bits_match_the_verified_capture(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x311, [0x02, 0x40, 0x03, 0xE8, 0x03, 0xE8, 0x00, 0x60])

    assert info["battery_charge_voltage"] == approx(57.6)
    assert info["charge_current_limit"] == approx(100.0)
    assert info["discharge_current_limit"] == approx(100.0)


@pytest.mark.parametrize(
    ("low_bits", "expected"),
    [(0, "soft starting"), (1, "stand by"), (2, "charging"), (3, "discharging")],
)
def test_0x311_status_uses_the_spec_encoding(bus: tuple[canbus, can.BusABC], low_bits: int, expected: str) -> None:
    """Spec: 00 soft_starting, 01 stand by, 10 charging, 11 discharging (the map had these as flags)."""
    info: dict[str, Any] = _decode(bus, 0x311, [0, 0, 0, 0, 0, 0, 0, low_bits])

    assert info["status"] == low_bits
    assert info["status_desc"] == expected


def test_0x311_high_byte_of_the_status_word(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x311, [0, 0, 0, 0, 0, 0, 0x06, 0x00])  # bits 8-9 = 2, bits 10-11 = 1

    assert info["master_box_operation_mode_desc"] == "parallel preparing"
    assert info["sp_status_desc"] == "stand by"


@pytest.mark.parametrize(
    ("frame_bytes", "name", "expected"),
    [
        ((0x01, 0x00, 0, 0), "protection_flags", "Soft start fail"),  # table 1 bit 0 is in byte 0
        ((0x80, 0x00, 0, 0), "protection_flags", "DisCharge over current"),  # table 1 bit 7
        ((0x00, 0x80, 0, 0), "protection_flags", "OTD (Over Temperature Discharge) protection"),  # table 2 bit 7
        ((0, 0, 0x10, 0x00), "alarm_flags", "Cell under voltage"),  # table 3 bit 4
        ((0, 0, 0x20, 0x00), "alarm_flags", "Cell over voltage"),  # table 3 bit 5
        ((0, 0, 0x00, 0x80), "alarm_flags", "OTD (Over Temperature Discharge) protection"),  # table 4 bit 7
        ((0, 0, 0x00, 0x02), "alarm_flags", "Internal communication fail"),  # table 4 bit 1
    ],
)
def test_0x312_protection_and_alarm_bytes(
    bus: tuple[canbus, can.BusABC], frame_bytes: tuple[int, int, int, int], name: str, expected: str
) -> None:
    info: dict[str, Any] = _decode(bus, 0x312, [*frame_bytes, 2, 0, 0, 16])

    assert info[name] == expected


def test_0x312_pack_and_cell_counts(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x312, [0, 0, 0, 0, 3, 0xAA, 0xBB, 16])

    assert info["pack_number"] == 3
    assert info["total_cell_number"] == 16


def test_0x313_measurements_and_soh_bits(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x313, [0x14, 0xB4, 0xFF, 0xCE, 0x00, 0xC8, 80, 0xE4])

    assert info["battery_voltage"] == approx(53.0)
    assert info["total_current_of_system"] == approx(-5.0)
    assert info["maximum_cell_temperature"] == approx(20.0)
    assert info["average_state_of_charge_of_system"] == 80
    assert info["state_of_health"] == 100  # bits 0-6 of byte 7
    assert info["state_of_health_flag"] == 1  # bit 7


def test_0x314_rows_use_the_hex_can_id(bus: tuple[canbus, can.BusABC]) -> None:
    """FCC, delta-V and cycle count were written as decimal 314.x (CAN ID 0x13A) and never decoded."""
    info: dict[str, Any] = _decode(bus, 0x314, [0x03, 0xE8, 0x0B, 0xB8, 0x00, 0x0A, 0x01, 0x2C])

    assert info["remaining_capacity"] == approx(10.0)
    assert info["full_charge_capacity"] == approx(30.0)
    assert info["cell_voltage_difference"] == 10
    assert info["cycle_count"] == 300


def test_0x319_request_flags_and_battery_type_come_from_byte_0(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x319, [0xC1, 0x0E, 0x10, 0x0C, 0x80, 5, 6, 7])

    assert info["request_flags"] == "Discharge enable,Charge enable"
    assert info["battery_type"] == 1
    assert info["battery_type_desc"] == "Nickel Cobalt Manganese (NCM)"
    assert info["maximum_cell_voltage"] == 3600  # bytes 1-2 are untouched by byte 0's flags
    assert info["protect_pack_id"] == 7


def test_0x320_date_time_is_one_32_bit_word(bus: tuple[canbus, can.BusABC]) -> None:
    stamp: int = (24 << 26) | (5 << 22) | (17 << 17) | (13 << 12) | (45 << 6) | 30
    info: dict[str, Any] = _decode(bus, 0x320, [ord("G"), ord("R"), 3, 4, *stamp.to_bytes(4, "big")])

    assert info["manufacturer_name"] == "GR"
    assert info["hardware_version"] == 3
    assert info["software_version"] == 4
    assert (info["date_time_year_since_2000"], info["date_time_month"], info["date_time_day"]) == (24, 5, 17)
    assert (info["date_time_hour"], info["date_time_minute"], info["date_time_second"]) == (13, 45, 30)


def test_0x321_update_status_is_byte_0(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x321, [0x01, 50, 2, 1, 0, 0, 0, 0])

    assert info["update_status"] == 1
    assert info["update_schedule_of_single_pack"] == 50
