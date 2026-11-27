# Description: Tests for the serial_bluetooth_seplos transport: the Seplos V2 telemetry frame over Bluetooth, decoded from a captured frame
# File: test_serial_bluetooth_seplos.py
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
Unit tests for classes.transports.serial_bluetooth_seplos.

The frame is a capture from a 16-cell Seplos V2 pack with 6 sensors (54.43 V, +11.8 A, 98.2 percent) as published in
the aiobmsble test suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_seplos import serial_bluetooth_seplos

FRAME: bytes = bytes.fromhex(
    "7e14006100006a0000100d440d470d4c0d470d450d460d460d580d580d590d480d440d480d430d480d47060b930b920b900b"
    "950bd00ba0049c1543749d0676c003d676c000d603e815460000000000000000000000000000000000000000000000000203"
    "08000000000000000000020000d8c30d"
)
REQUEST: str = "7e10004661000100f7c10d"


def make(monkeypatch: pytest.MonkeyPatch, frame: bytes = FRAME, slice_size: int = 20) -> Any:
    return make_transport(serial_bluetooth_seplos, monkeypatch, FakeDevice(lambda request: frame, slice_size=slice_size), protocol_version="seplos_bms_v2")


def read(transport: Any) -> dict[str, Any]:
    try:
        transport.connect()
        return transport.read_data()
    finally:
        transport.cleanup()


def sealed(body: bytes) -> bytes:
    """Frames the bytes between the 7E and the crc with the head, crc and tail."""
    return b"\x7e" + body + serial_bluetooth_seplos.calculate_crc(body).to_bytes(2, "big") + b"\x0d"


def test_the_telemetry_request_is_the_one_sent_to_a_real_board(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).build_frame(0x61).hex() == REQUEST


def test_the_captured_frame_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded = make(monkeypatch).decode_frame(FRAME)
    assert decoded is not None
    assert decoded.command == 0x61


@pytest.mark.parametrize("frame", [
    FRAME[:-3] + b"\x00\x00\x0d",         # wrong crc
    FRAME[:-1] + b"\x00",                  # wrong tail
    FRAME[:60] + FRAME[61:],               # a byte is missing
    FRAME[:4] + b"\x02" + FRAME[5:],       # the board reports an error
])
def test_an_invalid_frame_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices_and_skips_noise() -> None:
    buffer: bytearray = bytearray(b"\x01\x7e\x02")
    found: list[bytes | None] = []
    for start in range(0, len(FRAME), 20):
        buffer += FRAME[start:start + 20]
        found.append(serial_bluetooth_seplos.extract_frame(buffer))
    assert found[:-1] == [None] * (len(found) - 1)
    assert found[-1] == FRAME


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    corrupt: bytes = FRAME[:50] + b"\x99" + FRAME[51:]
    assert serial_bluetooth_seplos.extract_frame(bytearray(corrupt + FRAME)) == FRAME


def test_it_reads_the_captured_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))

    assert out["voltage"] == approx(54.43)
    assert out["current"] == approx(11.8)
    assert out["state_of_charge"] == approx(98.2)
    assert out["state_of_health"] == approx(100.0)
    assert out["remaining_capacity"] == approx(298.53)
    assert out["design_capacity"] == approx(304.0)
    assert out["cycle_count"] == 214
    assert out["cell_count"] == 16
    assert out["cell_1_voltage"] == approx(3.396)
    assert out["cell_10_voltage"] == approx(3.417)
    assert [out[f"temperature_{i}"] for i in (1, 2, 3, 4, 5)] == [approx(v) for v in (23.2, 23.1, 22.9, 23.4, 29.3)]
    assert out["temperature_count"] == 6


def test_sensors_beyond_the_count_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))
    assert "temperature_6" in out
    assert "temperature_7" not in out


@pytest.mark.parametrize("size", [1, 20, 244])
def test_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(make(monkeypatch, slice_size=size))["voltage"] == approx(54.43)


def test_a_pack_with_fewer_cells_and_sensors_moves_the_values_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four cells and two sensors: every offset after the cells shifts, and the values must still be found."""
    old_values: bytes = FRAME[55:-3]
    cells: bytes = b"".join(mv.to_bytes(2, "big") for mv in (3300, 3301, 3302, 3303))
    temperatures: bytes = (2931).to_bytes(2, "big") + (2941).to_bytes(2, "big")
    data: bytes = b"\x00\x00\x04" + cells + b"\x02" + temperatures + old_values
    body: bytes = bytes([0x14, 0x00, 0x61, 0x00]) + len(data).to_bytes(2, "big") + data
    out: dict[str, Any] = read(make(monkeypatch, sealed(body)))

    assert out["cell_count"] == 4
    assert out["cell_4_voltage"] == approx(3.303)
    assert "cell_5_voltage" not in out
    assert out["temperature_1"] == approx(20.0)
    assert out["temperature_2"] == approx(21.0)
    assert "temperature_3" not in out
    assert out["voltage"] == approx(54.43)
    assert out["state_of_charge"] == approx(98.2)


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_seplos.transport_type == "scraper"
    assert (serial_bluetooth_seplos.SERVICE_UUID, serial_bluetooth_seplos.NOTIFY_UUID, serial_bluetooth_seplos.WRITE_UUID) == ("ff00", "ff01", "ff02")
