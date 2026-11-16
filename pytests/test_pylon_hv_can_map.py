# Description: Decode tests for the Pylontech high-voltage CAN protocol map, and the CAN poll-frame feature it needs.
# File: test_pylon_hv_can_map.py
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
"CANBUS Protocol of High Voltage system V1.17": 29-bit IDs, 500 kbps, little endian.  The host must send the broadcast
request 0x4200 (byte 0 = 0 for the ensemble information, 2 for the system equipment information) before the battery
answers on 0x4210+Addr and so on, so the protocol asks canbus to send those two frames every few seconds.
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


def _make(**settings: Any) -> tuple[canbus, can.BusABC]:
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    values: dict[str, Any] = {
        "protocol_version": "pylon_hv_can", "port": channel, "interface": "virtual", "baudrate": 500000,
        "serial_number": "T", "bridge": "transport.mqtt",
    }
    values.update(settings)
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)  # before the transport, so it hears the connect-time frames
    transport: canbus = canbus(DummySettings(**values))  # the constructor runs init_after_connect()
    return transport, peer


def _close(transport: canbus, peer: can.BusABC) -> None:
    peer.shutdown()
    if transport.bus is not None:
        transport.bus.shutdown()


@pytest.fixture
def bus() -> Iterator[tuple[canbus, can.BusABC]]:
    transport, peer = _make()
    yield transport, peer
    _close(transport, peer)


def _decode(bus: tuple[canbus, can.BusABC], can_id: int, data: list[int]) -> dict[str, Any]:
    transport, peer = bus
    peer.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=True))
    deadline: float = time.monotonic() + 3
    while time.monotonic() < deadline:
        if transport.cache is not None and can_id in transport.cache:
            break
        time.sleep(0.02)
    return transport.read_data()


def _le(value: int) -> list[int]:
    return list(value.to_bytes(2, "little"))


# --- poll frames -----------------------------------------------------------------------------------------------


def test_the_two_request_frames_are_sent_as_extended_ids_on_connect() -> None:
    peer_channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=peer_channel)
    transport: canbus = canbus(DummySettings(protocol_version="pylon_hv_can", port=peer_channel, interface="virtual", baudrate=500000, serial_number="T", bridge="x"))
    try:
        first, second = peer.recv(timeout=2), peer.recv(timeout=2)
        assert first is not None and second is not None
        assert (first.arbitration_id, first.is_extended_id, bytes(first.data)) == (0x4200, True, bytes(8))
        assert (second.arbitration_id, bytes(second.data)) == (0x4200, bytes([2, 0, 0, 0, 0, 0, 0, 0]))
    finally:
        _close(transport, peer)


def test_poll_frames_are_sent_again_once_the_interval_has_passed() -> None:
    transport, peer = _make(poll_interval="1")
    try:
        assert peer.recv(timeout=1) is not None and peer.recv(timeout=1) is not None  # the two sent on connect
        transport.read_data()  # well inside the interval: nothing sent
        assert peer.recv(timeout=0.2) is None

        transport._last_poll -= 2  # the interval has passed
        transport.read_data()
        assert peer.recv(timeout=2) is not None
    finally:
        _close(transport, peer)


def test_poll_frames_none_turns_the_protocol_default_off() -> None:
    transport, peer = _make(poll_frames="none")
    try:
        assert peer.recv(timeout=0.3) is None
    finally:
        _close(transport, peer)


def test_an_empty_poll_frames_setting_keeps_the_protocol_default() -> None:
    """A pre-filled settings form leaves the value empty; that must not silently disable the protocol's requests."""
    transport, peer = _make(poll_frames="")
    try:
        assert peer.recv(timeout=1) is not None
    finally:
        _close(transport, peer)


def test_poll_frames_can_be_set_in_the_transport_section() -> None:
    transport, peer = _make(poll_frames="0x4200:0100000000000000")
    try:
        message: can.Message | None = peer.recv(timeout=2)
        assert message is not None and bytes(message.data)[0] == 1
        assert peer.recv(timeout=0.3) is None  # the protocol's own two frames were replaced, not added to
    finally:
        _close(transport, peer)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x4200:00", [(0x4200, b"\x00")]),
        ("16896:0200000000000000;0x351:", [(16896, bytes.fromhex("0200000000000000")), (0x351, b"")]),
        ("0x305:00 00 00 00", [(0x305, bytes(4))]),
        ("", []),
        ("0x4200:zz;0x4200:00", [(0x4200, b"\x00")]),  # a bad item is skipped, the rest kept
        ("0x4200:000000000000000000", []),  # more than 8 bytes
        ("0x20000000:00", []),  # beyond 29 bits
    ],
)
def test_parse_poll_frames(raw: str, expected: list[tuple[int, bytes]]) -> None:
    assert canbus._parse_poll_frames(raw) == expected


def test_a_protocol_without_poll_frames_sends_nothing() -> None:
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    transport: canbus = canbus(DummySettings(protocol_version="pylon_can", port=channel, interface="virtual", baudrate=500000, serial_number="T", bridge="x"))
    try:
        assert peer.recv(timeout=0.3) is None
    finally:
        _close(transport, peer)


# --- decoding --------------------------------------------------------------------------------------------------


def test_0x4210_pile_values_with_their_offsets(bus: tuple[canbus, can.BusABC]) -> None:
    # 512.4 V, 30123 -> +12.3 A (offset -3000 A), 1250 -> 25.0 C (offset -100 C), SOC 80, SOH 99
    info: dict[str, Any] = _decode(bus, 0x4210, [*_le(5124), *_le(30123), *_le(1250), 80, 99])

    assert info["battery_pile_total_voltage"] == approx(512.4)
    assert info["battery_pile_current"] == approx(12.3)
    assert info["bms_temperature"] == approx(25.0)
    assert (info["soc"], info["soh"]) == (80, 99)


def test_a_discharging_pile_reads_negative(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x4210, [*_le(5000), *_le(29000), *_le(1000), 50, 100])  # 29000 -> -100.0 A; 1000 -> 0.0 C

    assert info["battery_pile_current"] == approx(-100.0)
    assert info["bms_temperature"] == approx(0.0)


def test_0x4220_limits(bus: tuple[canbus, can.BusABC]) -> None:
    info: dict[str, Any] = _decode(bus, 0x4220, [*_le(5600), *_le(4200), *_le(30250), *_le(30500)])

    assert info["charge_cutoff_voltage"] == approx(560.0)
    assert info["discharge_cutoff_voltage"] == approx(420.0)
    assert info["max_charge_current"] == approx(25.0)
    assert info["max_discharge_current"] == approx(50.0)


def test_0x4230_and_0x4240_cell_extremes(bus: tuple[canbus, can.BusABC]) -> None:
    cells: dict[str, Any] = _decode(bus, 0x4230, [*_le(3352), *_le(3340), *_le(17), *_le(3)])
    temps: dict[str, Any] = _decode(bus, 0x4240, [*_le(1260), *_le(1230), *_le(4), *_le(9)])

    assert (cells["max_cell_voltage"], cells["min_cell_voltage"]) == (approx(3.352), approx(3.34))
    assert (cells["max_cell_voltage_number"], cells["min_cell_voltage_number"]) == (17, 3)
    assert (temps["max_cell_temperature"], temps["min_cell_temperature"]) == (approx(26.0), approx(23.0))
    assert (temps["max_cell_temperature_number"], temps["min_cell_temperature_number"]) == (4, 9)


@pytest.mark.parametrize(("state", "label"), [(0, "sleep"), (1, "charge"), (2, "discharge"), (3, "idle")])
def test_0x4250_battery_state(bus: tuple[canbus, can.BusABC], state: int, label: str) -> None:
    info: dict[str, Any] = _decode(bus, 0x4250, [state, 0, 0, 0, 0, 0, 0, 0])

    assert info["battery_state_desc"] == label


def test_0x4250_status_flags_cycle_period_and_error_words(bus: tuple[canbus, can.BusABC]) -> None:
    # byte 0: discharging + forced charge + balance request; cycle period 300; fault bits 2 and 3;
    # alarm 0x0803 = BLV + BHV + MHV; protection 0x0201 = BUV + DOC
    info: dict[str, Any] = _decode(bus, 0x4250, [0x1A, *_le(300), 0b00001100, *_le(0x0803), *_le(0x0201)])

    assert info["battery_state"] == 2
    assert info["basic_status_flags"] == "forced charge request,balance charge request"
    assert info["cycle_period"] == 300
    assert info["fault"] == "internal communication error,input over voltage"
    assert info["alarm"] == "BLV: single cell low voltage,BHV: single cell high voltage,MHV: module high voltage"
    assert info["protection"] == "BUV: single cell under voltage,DOC: discharge over current"


def test_0x4260_0x4270_module_extremes(bus: tuple[canbus, can.BusABC]) -> None:
    voltages: dict[str, Any] = _decode(bus, 0x4260, [*_le(53500), *_le(53100), *_le(2), *_le(5)])
    temperatures: dict[str, Any] = _decode(bus, 0x4270, [*_le(1270), *_le(1220), *_le(1), *_le(4)])

    assert (voltages["module_max_voltage"], voltages["module_min_voltage"]) == (approx(53.5), approx(53.1))
    assert (temperatures["module_max_temperature"], temperatures["module_min_temperature"]) == (approx(27.0), approx(22.0))


def test_0x4280_forbidden_marks_and_0x4290_fault_extension(bus: tuple[canbus, can.BusABC]) -> None:
    marks: dict[str, Any] = _decode(bus, 0x4280, [0xAA, 0x00, 0, 0, 0, 0, 0, 0])
    extension: dict[str, Any] = _decode(bus, 0x4290, [0b00001010, 0, 0, 0, 0, 0, 0, 0])

    assert marks["charge_forbidden_mark"] == 170
    assert marks["charge_forbidden_mark_desc"] == "charge forbidden"
    assert marks["discharge_forbidden_mark"] == 0
    assert extension["fault_extension"] == "BMIC error,self-test error"


def test_serial_number_and_manufacturer(bus: tuple[canbus, can.BusABC]) -> None:
    serial: dict[str, Any] = _decode(bus, 0x42E0, list(b"12345678"))
    maker: dict[str, Any] = _decode(bus, 0x42F0, list(b"Dyness\x00\x00"))

    assert serial["serial_number"] == "12345678"
    assert maker["manufacturer_name"].strip("\x00 ") == "Dyness"


def test_0x7310_and_0x7320_system_information(bus: tuple[canbus, can.BusABC]) -> None:
    version: dict[str, Any] = _decode(bus, 0x7310, [2, 0, 2, 1, 1, 2, 3, 4])
    system: dict[str, Any] = _decode(bus, 0x7320, [*_le(8), 8, 16, *_le(512), *_le(100)])

    assert version["hardware_version_letter_desc"] == "B"
    assert [version[n] for n in ("hardware_version_v", "hardware_version_r", "software_version_major", "software_version_minor")] == [2, 1, 1, 2]
    assert system["battery_module_quantity"] == 8
    assert (system["battery_modules_in_series"], system["cells_per_battery_module"]) == (8, 16)
    assert (system["voltage_level"], system["battery_capacity"]) == (512, 100)


# --- several towers: one section per address ------------------------------------------------------------------


def _tower(channel: str, address: int | None) -> canbus:
    values: dict[str, Any] = {
        "protocol_version": "pylon_hv_can", "port": channel, "interface": "virtual", "baudrate": 500000,
        "serial_number": f"T{address}", "bridge": "transport.mqtt", "poll_frames": "none",
    }
    if address is not None:
        values["address"] = address
    return canbus(DummySettings(**values))


def test_each_tower_decodes_only_its_own_ids() -> None:
    """Address n answers on base + n (0x4210 / 0x4211 ...), so two sections on one bus must not mix their frames."""
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    tower0, tower1 = _tower(channel, 0), _tower(channel, 1)
    try:
        for can_id, soc in ((0x4210, 40), (0x4211, 90)):
            peer.send(can.Message(arbitration_id=can_id, data=bytes([*_le(5000), *_le(30000), *_le(1000), soc, 100]), is_extended_id=True))
        deadline: float = time.monotonic() + 3
        while time.monotonic() < deadline and not (tower0.cache and tower1.cache and 0x4210 in tower0.cache and 0x4211 in tower1.cache):
            time.sleep(0.02)

        assert tower0.read_data()["soc"] == 40
        assert tower1.read_data()["soc"] == 90
    finally:
        peer.shutdown()
        for t in (tower0, tower1):
            if t.bus is not None:
                t.bus.shutdown()


def test_a_tower_without_a_frame_of_its_own_publishes_nothing_for_it() -> None:
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    tower3: canbus = _tower(channel, 3)
    try:
        peer.send(can.Message(arbitration_id=0x4210, data=bytes(8), is_extended_id=True))  # tower 0's frame
        time.sleep(0.3)

        assert "soc" not in tower3.read_data()
    finally:
        peer.shutdown()
        if tower3.bus is not None:
            tower3.bus.shutdown()


def test_the_protocols_own_map_is_not_changed_by_a_towers_address() -> None:
    from classes.protocol_settings import Registry_Type

    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    tower2: canbus = _tower(channel, 2)
    try:
        assert tower2.protocolSettings is not None
        base: dict[str, int] = {e.variable_name: e.register for e in tower2.protocolSettings.get_registry_map(Registry_Type.HOLDING)}
        assert base["soc"] == 0x4210  # shared protocol object untouched
        assert {e.variable_name: e.register for e in tower2._registry_map()}["soc"] == 0x4212
    finally:
        if tower2.bus is not None:
            tower2.bus.shutdown()


def test_read_variable_uses_the_towers_address() -> None:
    from classes.protocol_settings import Registry_Type

    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    tower1: canbus = _tower(channel, 1)
    try:
        peer.send(can.Message(arbitration_id=0x4211, data=bytes([*_le(5000), *_le(30000), *_le(1000), 77, 100]), is_extended_id=True))
        deadline: float = time.monotonic() + 3
        while time.monotonic() < deadline and not (tower1.cache and 0x4211 in tower1.cache):
            time.sleep(0.02)

        assert tower1.read_variable("soc", Registry_Type.HOLDING) == 77
    finally:
        peer.shutdown()
        if tower1.bus is not None:
            tower1.bus.shutdown()


def test_the_poll_frames_stay_broadcast_whatever_the_address() -> None:
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    tower = canbus(DummySettings(protocol_version="pylon_hv_can", port=channel, interface="virtual", baudrate=500000, serial_number="T", bridge="x", address=5))
    try:
        first: can.Message | None = peer.recv(timeout=2)
        assert first is not None and first.arbitration_id == 0x4200
    finally:
        peer.shutdown()
        if tower.bus is not None:
            tower.bus.shutdown()


@pytest.mark.parametrize("address", [16, -1, 255])
def test_an_address_outside_the_protocols_range_is_refused(address: int, caplog: pytest.LogCaptureFixture) -> None:
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    with caplog.at_level("WARNING"):
        tower: canbus = _tower(channel, address)
    try:
        assert tower.address == 0
        assert f"address {address} is outside 0-15" in caplog.text
    finally:
        if tower.bus is not None:
            tower.bus.shutdown()


def test_an_address_is_ignored_with_a_warning_by_a_protocol_that_does_not_use_it(caplog: pytest.LogCaptureFixture) -> None:
    """pylon_can ids are fixed; moving them would silently read the wrong frames."""
    channel: str = f"pytest_pylon_hv_{next(_ids)}"
    with caplog.at_level("WARNING"):
        transport = canbus(DummySettings(protocol_version="pylon_can", port=channel, interface="virtual", baudrate=500000, serial_number="T", bridge="x", address=3))
    try:
        assert transport.address == 0
        assert {e.variable_name: e.register for e in transport._registry_map()}["average_soc"] == 0x355
        assert "address = 3 is ignored" in caplog.text
    finally:
        if transport.bus is not None:
            transport.bus.shutdown()
