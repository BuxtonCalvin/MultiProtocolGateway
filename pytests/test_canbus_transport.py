# Description: Unit tests for the CAN bus transport: read path regressions and the opt-in transmit path.
# File: test_canbus_transport.py
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
# These tests deliberately exercise protected members (_encode_tx_value, _build_tx_frame, ...).
"""
Unit tests for classes.transports.canbus.

No hardware is involved: python-can's in-process ``virtual`` interface stands in for the bus, and a
second ``virtual`` bus on the same channel plays the device.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterator
from typing import Any

import can
import pytest
from conftest import DummySettings

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    registry_map_entry,
)
from classes.transports.canbus import canbus
from classes.transports.transport_base import TransportWriteMode

_channel_counter: Iterator[int] = itertools.count()


def _make(protocol: str = "growatt_bms_canbus_v1.04", **settings: Any) -> tuple[canbus, can.BusABC]:
    """A canbus transport plus a peer bus on the same virtual channel."""
    channel: str = f"pytest_can_{next(_channel_counter)}"
    values: dict[str, Any] = {
        "protocol_version": protocol,
        "port": channel,
        "interface": "virtual",
        "baudrate": 500000,
        "serial_number": "TEST0001",  # skip serial sniffing
        "bridge": "transport.mqtt",
    }
    values.update(settings)
    transport: canbus = canbus(DummySettings(**values))
    peer: can.BusABC = can.interface.Bus(interface="virtual", channel=channel)
    return transport, peer


def _close(transport: canbus, peer: can.BusABC) -> None:
    peer.shutdown()
    if transport.bus is not None:
        transport.bus.shutdown()


@pytest.fixture
def pair() -> Iterator[tuple[canbus, can.BusABC]]:
    transport, peer = _make()
    yield transport, peer
    _close(transport, peer)


def _entry(transport: canbus, name: str) -> registry_map_entry:
    assert transport.protocolSettings is not None
    for entry in transport._registry_map():
        if entry.variable_name == name:
            return entry
    raise AssertionError(f"{name} not in the protocol")  # noqa: EM102


def _make_writable(transport: canbus, name: str) -> registry_map_entry:
    entry: registry_map_entry = _entry(transport, name)
    entry.write_mode = WriteMode.WRITE
    return entry


def _wait_for_cache(transport: canbus, can_id: int, timeout: float = 3.0) -> None:
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if transport.cache is not None and can_id in transport.cache:
            return
        time.sleep(0.02)
    raise AssertionError(f"frame 0x{can_id:X} never reached the cache")  # noqa: EM102


# --- read path regressions -------------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", ["growatt_bms_canbus_v1.04", "pylon_can", "victron_gx_generic_canbus"])
def test_read_data_returns_without_deadlock_and_decodes(protocol: str) -> None:
    """read_data() used to call clean_cache() while holding the non-reentrant lock, so it hung forever.

    The pylon and victron CAN maps are named ``*.holding_registry_map.csv``; they must decode too.
    """
    transport, peer = _make(protocol)
    try:
        first: registry_map_entry = transport._registry_map()[0]
        peer.send(can.Message(arbitration_id=first.register, data=bytes(range(1, 9)), is_extended_id=first.register > 0x7FF))
        _wait_for_cache(transport, first.register)

        result: dict[str, Any] = {}
        worker: threading.Thread = threading.Thread(target=lambda: result.update(transport.read_data()), daemon=True)
        worker.start()
        worker.join(timeout=5)

        assert not worker.is_alive(), "read_data() deadlocked"
        assert result, "no values decoded from the cached frame"
    finally:
        _close(transport, peer)


@pytest.mark.parametrize("can_id", [0x311, 0x320])
def test_read_data_survives_rows_without_a_bit_position(can_id: int) -> None:
    """Growatt 0x311 holds a 2BIT row addressed by byte only (X311.6): register_bit is -1.

    Decoding used it as a shift count and raised ``ValueError: negative shift count`` on the first
    frame, killing read_data(). Frame 0x320 holds typeless rows and must not raise either.
    """
    transport, peer = _make("growatt_bms_canbus_v1.04")
    try:
        peer.send(can.Message(arbitration_id=can_id, data=bytes(range(1, 9)), is_extended_id=False))
        _wait_for_cache(transport, can_id)

        info: dict[str, Any] = transport.read_data()

        assert isinstance(info, dict)
    finally:
        _close(transport, peer)


# --- enable_write validation -----------------------------------------------------------------------------


def test_enable_write_refused_without_writable_rows(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    transport.write_enabled = True
    peer.send(can.Message(arbitration_id=314, data=bytes(8), is_extended_id=False))
    _wait_for_cache(transport, 314)

    transport.enable_write()

    assert transport.write_enabled is False  # every shipped CAN map is read-only


def test_enable_write_refused_when_bus_is_silent(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    _make_writable(transport, "battery_charge_voltage")
    transport.write_enabled = True
    transport._SN_SETTLE_SECS = 0.3

    transport.enable_write()

    assert transport.write_enabled is False


def test_enable_write_passes_with_writable_row_and_traffic(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    _make_writable(transport, "battery_charge_voltage")
    transport.write_enabled = True
    peer.send(can.Message(arbitration_id=314, data=bytes(8), is_extended_id=False))
    _wait_for_cache(transport, 314)

    transport.enable_write()

    assert transport.write_enabled is True


def test_enable_write_relaxed_waives_the_traffic_check() -> None:
    transport, peer = _make(write_type="relaxed")
    try:
        _make_writable(transport, "battery_charge_voltage")
        transport._SN_SETTLE_SECS = 0.3
        transport.write_enabled = True

        transport.enable_write()

        assert transport.write_enabled is True
    finally:
        _close(transport, peer)


def test_enable_write_unsafe_skips_validation() -> None:
    transport, peer = _make(write_type="unsafe")
    try:
        # no writable rows, no traffic: unsafe mode still leaves writes on
        transport.enable_write()
        assert transport.write_enabled is True
    finally:
        _close(transport, peer)


# --- value encoding --------------------------------------------------------------------------------------


def test_encode_applies_unit_mod_and_big_endian(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "battery_charge_voltage")  # USHORT, 0.1 V
    transport.write_enabled = True

    assert transport._encode_tx_value(entry, 54.4) == (544).to_bytes(2, "big")


def test_encode_signed_negative(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "battery_charge_voltage")
    entry.data_type = Data_Type.SHORT
    entry.unit_mod = 1
    transport.write_enabled = True
    transport.write_mode = TransportWriteMode.UNSAFE  # skip the row's 41~63 range check; this tests the encoding

    assert transport._encode_tx_value(entry, -5) == (-5).to_bytes(2, "big", signed=True)


def test_encode_rejects_out_of_range(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "battery_charge_voltage")
    entry.data_type = Data_Type.BYTE
    entry.unit_mod = 1

    assert transport._encode_tx_value(entry, 300) is None


def test_encode_rejects_types_it_cannot_send(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "battery_charge_voltage")
    entry.data_type = Data_Type.FLOAT32

    assert transport._encode_tx_value(entry, 1.5) is None


# --- frame building --------------------------------------------------------------------------------------


def test_build_frame_places_field_at_offset_and_pads_with_zeros(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "charge_current_limit")  # frame 785, byte 2

    frame: bytes | None = transport._build_tx_frame(entry, b"\x12\x34")

    assert frame == b"\x00\x00\x12\x34"


def test_build_frame_keeps_earlier_writes_to_the_same_frame(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    first: registry_map_entry = _make_writable(transport, "battery_charge_voltage")  # frame 785, byte 0
    second: registry_map_entry = _make_writable(transport, "charge_current_limit")  # frame 785, byte 2
    transport._tx_frames[first.register] = b"\xaa\xbb"

    assert transport._build_tx_frame(second, b"\x12\x34") == b"\xaa\xbb\x12\x34"


def test_build_frame_rejects_a_field_past_eight_bytes(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    entry: registry_map_entry = _make_writable(transport, "charge_current_limit")
    entry.register_byte = 7

    assert transport._build_tx_frame(entry, b"\x00\x01") is None


# --- transmitting ----------------------------------------------------------------------------------------


def test_write_data_sends_a_frame_the_device_receives(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    _make_writable(transport, "battery_charge_voltage")
    transport.write_enabled = True

    transport.write_data({"battery_charge_voltage": 54.4}, transport)

    message: can.Message | None = peer.recv(timeout=2)
    assert message is not None
    assert message.arbitration_id == 785
    assert bytes(message.data) == (544).to_bytes(2, "big")
    assert message.is_extended_id is False


def test_write_data_does_nothing_when_write_is_disabled(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    _make_writable(transport, "battery_charge_voltage")
    transport.write_enabled = False

    transport.write_data({"battery_charge_voltage": 54.4}, transport)

    assert peer.recv(timeout=0.3) is None


def test_write_data_ignores_read_only_rows(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    transport.write_enabled = True  # nothing is marked writable

    transport.write_data({"battery_charge_voltage": 54.4}, transport)

    assert peer.recv(timeout=0.3) is None


def test_send_frame_uses_extended_id_above_0x7ff(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair
    transport.write_enabled = True

    assert transport.send_frame(0x18FF50E5, b"\x01") is True

    message: can.Message | None = peer.recv(timeout=2)
    assert message is not None
    assert message.arbitration_id == 0x18FF50E5
    assert message.is_extended_id is True


def test_send_frame_can_send_a_remote_request(pair: tuple[canbus, can.BusABC]) -> None:
    """An active command: a remote-transmission request asks a device to answer."""
    transport, peer = pair
    transport.write_enabled = True

    assert transport.send_frame(0x305, remote=True, dlc=8) is True

    message: can.Message | None = peer.recv(timeout=2)
    assert message is not None
    assert message.is_remote_frame is True
    assert message.arbitration_id == 0x305


def test_send_frame_refused_when_write_is_disabled(pair: tuple[canbus, can.BusABC]) -> None:
    transport, peer = pair

    assert transport.send_frame(0x305, b"\x01") is False
    assert peer.recv(timeout=0.3) is None


def test_send_frame_rejects_more_than_eight_bytes(pair: tuple[canbus, can.BusABC]) -> None:
    transport, _ = pair
    transport.write_enabled = True

    assert transport.send_frame(0x305, bytes(9)) is False


def test_registry_type_enum_is_unchanged() -> None:
    """The transport relies on CUSTOM_BUS and HOLDING being distinct registry types."""
    assert Registry_Type.CUSTOM_BUS != Registry_Type.HOLDING
