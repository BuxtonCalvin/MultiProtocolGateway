# Description: Decode tests for the Victron / SMA CAN-bus BMS protocol map (victron_gx_generic_canbus).
# File: test_victron_canbus_map.py
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
Victron does not publish its CAN-bus BMS protocol (its staff cite an NDA); the frames are documented by SMA's "Connecting
batteries with external BMS to Sunny Island" (in documentation/3rdparty/protocols), by Pylontech V1.2 / V1.3, and by open
implementations such as SimpBMS, all of which agree: 500 kbit/s, standard IDs, little endian.

Every 16-bit value used to be read big endian (a SOC of 87 came out as 22272), and the Sunny Island's own frames
0x305-0x307, which a battery never sends, were mixed into the map.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator
from typing import Any

import can
import pytest
from conftest import DummySettings, approx

from classes.protocol_settings import protocol_settings
from classes.transports.canbus import canbus

_ids: Iterator[int] = itertools.count()


@pytest.fixture
def bus() -> Iterator[tuple[canbus, can.BusABC]]:
    channel: str = f"pytest_victron_{next(_ids)}"
    transport = canbus(DummySettings(protocol_version="victron_gx_generic_canbus", port=channel, interface="virtual", baudrate=500000, serial_number="T", bridge="x"))
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


def _le(value: int) -> list[int]:
    return list((value & 0xFFFF).to_bytes(2, "little"))


def test_the_protocol_is_500_kbit_canbus() -> None:
    assert protocol_settings.get_transport_type("victron_gx_generic_canbus") == "canbus"
    assert protocol_settings("victron_gx_generic_canbus").settings["baud"] == "500000"


def test_the_sunny_island_frames_are_not_in_the_battery_map(bus: tuple[canbus, can.BusABC]) -> None:
    ids: set[int] = {entry.register for entry in bus[0]._registry_map()}

    assert not ids & {0x305, 0x306, 0x307}


def test_soc_voltage_and_current_are_little_endian(bus: tuple[canbus, can.BusABC]) -> None:
    soc: dict[str, Any] = _decode(bus, 0x355, [*_le(87), *_le(100), *_le(8750), 0, 0])
    measured: dict[str, Any] = _decode(bus, 0x356, [*_le(5310), *_le(-7), *_le(140), 0, 0])  # a public capture: BE 14 F9 FF 8C 00

    assert (soc["state_of_charge"], soc["state_of_health"]) == (87, 100)
    assert soc["high_res_state_of_charge"] == approx(87.5)
    assert measured["battery_voltage"] == approx(53.1)
    assert measured["battery_current"] == approx(-0.7)
    assert measured["battery_temperature"] == approx(14.0)


def test_0x351_limits_have_no_sunny_island_only_range(bus: tuple[canbus, can.BusABC]) -> None:
    """The 41~63 V and 0~1200 A ranges are SMA Sunny Island setpoint limits; a 24 V battery is valid."""
    info: dict[str, Any] = _decode(bus, 0x351, [*_le(288), *_le(1500), *_le(1500), *_le(220)])

    assert info["battery_charge_voltage"] == approx(28.8)
    assert info["dc_charge_current_limitation"] == approx(150.0)
    assert info["battery_discharge_voltage"] == approx(22.0)


def test_0x373_cell_extremes_are_mv_and_kelvin(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x373, [*_le(3304), *_le(3338), *_le(293), *_le(305)])

    assert info["min_cell_voltage"] == approx(3.304)
    assert info["max_cell_voltage"] == approx(3.338)
    assert info["lowest_cell_temperature"] == approx(19.85)
    assert info["highest_cell_temperature"] == approx(31.85)


def test_0x378_energy_is_in_units_of_100_wh(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x378, [0x10, 0x27, 0, 0, 0x20, 0x4E, 0, 0])  # 10000 and 20000 -> 1000 kWh and 2000 kWh

    assert info["historic_energy_charged"] == approx(1000.0)
    assert info["historic_energy_discharged"] == approx(2000.0)


def test_0x35a_four_alarm_bytes_then_four_warning_bytes(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x35A, [0xAA, 0xAA, 0xAA, 0b00000001, 0b10101001, 0xAA, 0xAA, 0xAA])

    assert info["external_alarm_4"] == "Cell imbalance alarm"
    assert info["external_warning_1"] == "General warning"
    assert info["external_alarm_1"] == info["external_warning_4"] == ""


def test_0x35e_is_the_manufacturer(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x35E, list(b"AOLITHIU"))

    assert info["manufacturer_name"] == "AOLITHIU"
