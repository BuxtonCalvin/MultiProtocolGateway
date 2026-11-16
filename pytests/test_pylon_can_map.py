# Description: Decode tests for the Pylon low-voltage CAN protocol map (pylon_can, V1.2).
# File: test_pylon_can_map.py
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
"CAN Bus Protocol PYLON Low Voltage V1.2" says the frame is little endian, 500 kbps, standard IDs.  The 16-bit values
were being read big endian (``little_endian-CDAB`` swaps words, which does nothing to a single 16-bit value), so every
voltage, current and flag word was wrong.  The 0x356 frame below is a capture of a real pack.
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
    channel: str = f"pytest_pylon_can_{next(_ids)}"
    transport: canbus = canbus(
        DummySettings(protocol_version="pylon_can", port=channel, interface="virtual", baudrate=500000, serial_number="T", bridge="transport.mqtt")
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


def test_0x351_limits_are_little_endian(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x351, [0x14, 0x02, 0x4A, 0x02, 0x4A, 0x02, 0xCC, 0x01])  # 0x0214, 0x024A, 0x024A, 0x01CC

    assert info["battery_charge_voltage"] == approx(53.2)
    assert info["charge_current_limit"] == approx(58.6)
    assert info["discharge_current_limit"] == approx(58.6)
    assert info["discharge_voltage_limit"] == approx(46.0)


def test_0x355_soc_and_soh_are_16_bit_little_endian(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x355, [87, 0, 100, 0, 0, 0, 0, 0])

    assert (info["average_soc"], info["average_soh"]) == (87, 100)


def test_0x356_matches_a_real_capture(bus: tuple[canbus, can.BusABC]) -> None:
    """BE 14 F9 FF 8C 00 00 00: 53.1 V, -0.7 A (discharging), 14.0 C (from a public BYD / Pylon-compatible capture)."""
    info: dict[str, Any] = _decode(bus, 0x356, [0xBE, 0x14, 0xF9, 0xFF, 0x8C, 0x00, 0x00, 0x00])

    assert info["average_voltage"] == approx(53.1)
    assert info["total_current"] == approx(-0.7)
    assert info["average_cell_temperature"] == approx(14.0)


@pytest.mark.parametrize(
    ("frame", "name", "expected"),
    [
        ([0x02, 0, 0, 0], "protection", "cell or module over voltage"),  # table 1 bit 1 (fixed in the V1.1 revision)
        ([0x04, 0, 0, 0], "protection", "cell or module under voltage"),  # table 1 bit 2
        ([0x80, 0, 0, 0], "protection", "discharge over current"),  # table 1 bit 7
        ([0, 0x01, 0, 0], "protection", "charge over current"),  # table 2 bit 0
        ([0, 0x08, 0, 0], "protection", "system error"),  # table 2 bit 3
        ([0, 0, 0x10, 0], "alarm", "cell low temperature"),  # table 3 bit 4
        ([0, 0, 0x80, 0], "alarm", "discharge high current"),  # table 3 bit 7
        ([0, 0, 0, 0x01], "alarm", "charge high current"),  # table 4 bit 0
        ([0, 0, 0, 0x08], "alarm", "Internal communication fail"),  # table 4 bit 3
    ],
)
def test_0x359_protection_and_alarm_tables(bus: tuple[canbus, can.BusABC], frame: list[int], name: str, expected: str) -> None:
    info: dict[str, Any] = _decode(bus, 0x359, [*frame, 10, 0x50, 0x4E, 0])

    assert info[name] == expected
    assert info["module_numbers"] == 10
    assert info["protocol_identifier"] == "PN"


def test_0x35c_request_flags_come_from_byte_0(bus: tuple[canbus, can.BusABC]) -> None:
    """These used to be read from byte 1."""
    info: dict[str, Any] = _decode(bus, 0x35C, [0xC8, 0, 0, 0, 0, 0, 0, 0])  # charge + discharge enable, request full charge

    assert info["request_flag"] == "Request full charge,Discharge enable,Charge enable"


def test_0x35e_manufacturer(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x35E, list(b"PYLON\x00\x00\x00"))

    assert info["manufacturer_name"].strip("\x00 ") == "PYLON"


def test_0x373_temperatures_are_kelvin(bus: tuple[canbus, can.BusABC]) -> None:
    """Victron / SMA CAN-bus BMS spec: mV, mV, K, K."""
    info: dict[str, Any] = _decode(bus, 0x373, [0xE8, 0x0C, 0x0A, 0x0D, 0x25, 0x01, 0x31, 0x01])  # 3304 mV, 3338 mV, 293 K, 305 K

    assert info["min_cell_voltage"] == approx(3.304)
    assert info["max_cell_voltage"] == approx(3.338)
    assert info["lowest_cell_temperature"] == approx(19.85)
    assert info["highest_cell_temperature"] == approx(31.85)
