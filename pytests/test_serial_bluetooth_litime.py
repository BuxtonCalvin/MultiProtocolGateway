# Description: Tests for the serial_bluetooth_litime transport: the LiTime status frame over Bluetooth, decoded from a captured frame
# File: test_serial_bluetooth_litime.py
#
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

"""
Unit tests for classes.transports.serial_bluetooth_litime.

The frame is a capture from an 8-cell 24V LiTime-family pack (26.556 V, -1.435 A, 65 percent) as published in the
aiobmsble test suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_litime import serial_bluetooth_litime

FRAME: bytes = bytes.fromhex(
    "000065019355aa0046660000bc670000f50cf70cfc0cfb0cf80cf20cfa0cf50c0000000000000000000000000000000065fa"
    "ffff17001600feff00000000e91a042900000000000000000000000000000000000000000000000041006400000003000000"
    "5f010000a2"
)


def make(monkeypatch: pytest.MonkeyPatch, frame: bytes = FRAME, slice_size: int = 20) -> Any:
    return make_transport(serial_bluetooth_litime, monkeypatch, FakeDevice(lambda request: frame, slice_size=slice_size), protocol_version="litime_bms")


def read(transport: Any) -> dict[str, Any]:
    try:
        transport.connect()
        return transport.read_data()
    finally:
        transport.cleanup()


def sealed(body: bytes) -> bytes:
    return body + bytes([serial_bluetooth_litime.calculate_checksum(body)])


def test_the_request_is_the_one_sent_to_a_real_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).build_frame(0x13).hex() == "000004011355aa17"


def test_the_captured_frame_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded = make(monkeypatch).decode_frame(FRAME)
    assert decoded is not None
    assert len(decoded.payload) == FRAME[2] + 4


@pytest.mark.parametrize("frame", [
    FRAME[:-1] + b"\x00",     # wrong checksum
    FRAME[:-2],               # truncated
    b"\x01\x01" + FRAME[2:],  # wrong start
])
def test_an_invalid_frame_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices_and_skips_noise() -> None:
    buffer: bytearray = bytearray(b"\x07\x08")
    found: list[bytes | None] = []
    for start in range(0, len(FRAME), 20):
        buffer += FRAME[start:start + 20]
        found.append(serial_bluetooth_litime.extract_frame(buffer))
    assert found[:-1] == [None] * (len(found) - 1)
    assert found[-1] == FRAME


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    corrupt: bytes = FRAME[:30] + b"\xaa" + FRAME[31:]
    assert serial_bluetooth_litime.extract_frame(bytearray(corrupt + FRAME)) == FRAME


def test_it_reads_the_captured_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))

    assert out["voltage"] == approx(26.556)
    assert out["current"] == approx(-1.435)
    assert out["state_of_charge"] == 65
    assert out["state_of_health"] == 100
    assert out["remaining_capacity"] == approx(68.89)
    assert out["design_capacity"] == approx(105.0)
    assert out["cycle_count"] == 3
    assert out["problem_code"] == 0
    assert [out[f"cell_{i}_voltage"] for i in range(1, 9)] == [approx(v) for v in (3.317, 3.319, 3.324, 3.323, 3.320, 3.314, 3.322, 3.317)]
    assert [out[f"temperature_{i}"] for i in (1, 2, 3)] == [23, 22, -2]


def test_cells_and_sensors_the_pack_does_not_have_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))
    assert "cell_9_voltage" not in out
    assert "cell_16_voltage" not in out
    assert "temperature_4" not in out
    assert "temperature_5" not in out


@pytest.mark.parametrize("size", [1, 20, 244])
def test_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(make(monkeypatch, slice_size=size))["voltage"] == approx(26.556)


def test_a_sensor_that_reads_zero_is_kept_once_a_later_one_has_shown_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    changed: bytearray = bytearray(FRAME[:-1])
    changed[52:54] = (0).to_bytes(2, "little", signed=True)   # sensor 1 reads exactly 0 degrees
    out: dict[str, Any] = read(make(monkeypatch, sealed(bytes(changed))))
    assert out["temperature_1"] == 0
    assert out["temperature_3"] == -2


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_litime.transport_type == "scraper"
    assert (serial_bluetooth_litime.SERVICE_UUID, serial_bluetooth_litime.NOTIFY_UUID, serial_bluetooth_litime.WRITE_UUID) == ("ffe0", "ffe1", "ffe2")
