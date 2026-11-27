# Description: Tests for the serial_bluetooth_ant transport: the ANT BMS status frame over Bluetooth, decoded from a captured frame, including the optional password
# File: test_serial_bluetooth_ant.py
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
Unit tests for classes.transports.serial_bluetooth_ant.

The frame is a capture from a 22-cell ANT BMS (50.88 V, +2.1 A, 10 percent) as published in the aiobmsble test suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_ant import serial_bluetooth_ant

FRAME: bytes = bytes.fromhex(
    "7ea11100009e050404160000000000000000000088010000000000000000000000001e091b091e091d091d091e0920091e09"
    "8f088e0890082209eb08200920091f091e091f091e091f0921091f091d001d001d001d001e001e00e01315000a0064000101"
    "000000b4c40486f197003aede8006a000000a1f2c0030000000022090c008e080a009400080900006d006a00af02f3fa4c74"
    "e5002866ec00c35d6200ccb39200edc8aa55"
)
REQUEST: str = "7ea1010000be1855aa55"


def make(monkeypatch: pytest.MonkeyPatch, frame: bytes = FRAME, slice_size: int = 20, **values: Any) -> Any:
    return make_transport(serial_bluetooth_ant, monkeypatch, FakeDevice(lambda request: frame if request[2] == 1 else None, slice_size=slice_size), protocol_version="ant_bms", **values)


def read(transport: Any) -> dict[str, Any]:
    try:
        transport.connect()
        return transport.read_data()
    finally:
        transport.cleanup()


def sealed(body: bytes) -> bytes:
    """Frames the bytes from the A1 to the end of the data with the head, crc and tail."""
    return b"\x7e" + body + serial_bluetooth_ant.calculate_crc(body).to_bytes(2, "little") + b"\xaa\x55"


def test_the_status_request_is_the_one_sent_to_a_real_board(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).build_frame(1).hex() == REQUEST


def test_the_password_frame_carries_the_password_at_address_6a01(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).build_frame(0x23, b"12345678").hex() == "7ea123016a08" "3132333435363738" "d9eeaa55"


def test_a_command_the_transport_does_not_send_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="not one this transport sends"):
        make(monkeypatch).build_frame(0x7F)


def test_the_captured_frame_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded = make(monkeypatch).decode_frame(FRAME)
    assert decoded is not None
    assert decoded.type == 0x11


@pytest.mark.parametrize("frame", [
    FRAME[:-4] + b"\x00\x00\xaa\x55",   # wrong crc
    FRAME[:-2] + b"\x00\x00",             # wrong tail
    FRAME[:60] + FRAME[61:],               # a byte is missing
])
def test_an_invalid_frame_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices_and_skips_noise() -> None:
    buffer: bytearray = bytearray(b"\x01\x7e\x02")
    found: list[bytes | None] = []
    for start in range(0, len(FRAME), 20):
        buffer += FRAME[start:start + 20]
        found.append(serial_bluetooth_ant.extract_frame(buffer))
    assert found[:-1] == [None] * (len(found) - 1)
    assert found[-1] == FRAME


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    corrupt: bytes = FRAME[:50] + b"\x99" + FRAME[51:]
    assert serial_bluetooth_ant.extract_frame(bytearray(corrupt + FRAME)) == FRAME


def test_it_reads_the_captured_board(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))

    assert out["voltage"] == approx(50.88)
    assert out["current"] == approx(2.1)
    assert out["state_of_charge"] == 10
    assert out["state_of_health"] == 100
    assert out["design_capacity"] == approx(80.0)
    assert out["remaining_capacity"] == approx(9.957766)
    assert out["total_charge"] == approx(15265.082)
    assert out["power"] == 106
    assert out["cell_voltage_delta"] == approx(0.148)
    assert out["cell_count"] == 22
    assert out["cell_1_voltage"] == approx(2.334)
    assert out["cell_9_voltage"] == approx(2.191)
    assert out["cell_22_voltage"] == approx(2.335)
    assert [out[f"temperature_{i}"] for i in (1, 2, 3, 4)] == [29, 29, 29, 29]
    assert out["mosfet_temperature"] == 30
    assert out["balancer_temperature"] == 30
    assert out["charge_mosfet"] == 1
    assert out["discharge_mosfet"] == 1


def test_cells_and_sensors_beyond_the_counts_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(make(monkeypatch))
    assert "cell_23_voltage" not in out
    assert "cell_32_voltage" not in out
    assert "temperature_5" not in out


@pytest.mark.parametrize("size", [1, 20, 244])
def test_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(make(monkeypatch, slice_size=size))["voltage"] == approx(50.88)


def test_a_board_with_fewer_cells_and_sensors_moves_the_values_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four cells and one sensor: every offset after the cells shifts, and the values must still be found."""
    cells: bytes = b"".join(mv.to_bytes(2, "little") for mv in (3300, 3301, 3302, 3303))
    temperatures: bytes = (25).to_bytes(2, "little") + (31).to_bytes(2, "little") + (32).to_bytes(2, "little")
    values: bytes = FRAME[90:-4]
    data: bytearray = bytearray(FRAME[6:34])
    data[2] = 1      # sensor count (byte 8 of the frame)
    data[3] = 4      # cell count (byte 9)
    body: bytes = bytes([0xA1, 0x11, 0, 0]) + b"\x00" + bytes(data) + cells + temperatures + bytes(values)
    body = body[:4] + bytes([len(body) - 5]) + body[5:]
    out: dict[str, Any] = read(make(monkeypatch, sealed(body)))
    assert out["cell_count"] == 4
    assert out["cell_4_voltage"] == approx(3.303)
    assert "cell_5_voltage" not in out
    assert out["temperature_1"] == 25
    assert "temperature_2" not in out
    assert out["mosfet_temperature"] == 31
    assert out["voltage"] == approx(50.88)
    assert out["state_of_charge"] == 10


# --------------------------------------------------------------------------- #
# the password                                                                #
# --------------------------------------------------------------------------- #

def test_a_locked_board_is_sent_its_password_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = FakeDevice(responder=lambda request: FRAME if request[2] == 1 else None)
    transport: Any = make_transport(serial_bluetooth_ant, monkeypatch, device, protocol_version="ant_bms", password="12345678")  # noqa: S106
    try:
        assert transport.connect()
        assert transport.read_data()["cell_count"] == 22
    finally:
        transport.cleanup()
    assert device.written[0] == serial_bluetooth_ant.build_frame(transport, 0x23, b"12345678")


def test_a_board_that_is_not_locked_is_not_sent_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = FakeDevice(responder=lambda request: FRAME)
    transport: Any = make_transport(serial_bluetooth_ant, monkeypatch, device, protocol_version="ant_bms")
    try:
        transport.connect()
        transport.read_data()
    finally:
        transport.cleanup()
    assert all(request[2] == 1 for request in device.written)


@pytest.mark.parametrize("password", ["pässword", "x" * 33])
def test_a_password_that_cannot_be_sent_is_refused_at_start(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    with pytest.raises(ValueError, match="password"):
        make(monkeypatch, password=password)  # noqa: S106


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_ant.transport_type == "scraper"
    assert (serial_bluetooth_ant.SERVICE_UUID, serial_bluetooth_ant.NOTIFY_UUID, serial_bluetooth_ant.WRITE_UUID) == ("ffe0", "ffe1", "ffe1")
