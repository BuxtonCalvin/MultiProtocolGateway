# Description: Tests for the serial_bluetooth_pace transport: PACEEX smart BMS frames over Bluetooth, decoded from captured frames
# File: test_serial_bluetooth_pace.py
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
Unit tests for classes.transports.serial_bluetooth_pace.

The frames are captures from a PACEEX 16S 100Ah pack (53 V, 74 percent) as published in the aiobmsble test
suite, so the decoded values are those of a real battery.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_pace import serial_bluetooth_pace

SYSTEM: bytes = (
    b"\x9a\x00\x00\x0a\x00\x00\x00\x33\x01\xff\xff\xfe\x59\x00\x00\x14\xb5\x00\x00\x1c\xe8\x00\x00\x27\x10\x00\x00\x27\x10"
    b"\x4a\x64\x00\x00\x00\xef\x00\x00\x00\x00\x00\x00\x00\x00\x01\x08\x0c\xef\x01\x10\x0c\xec\x01\x03\x0b\x8e\x01\x01\x0b\x89"
    b"\x99\x16\x9d"
)
PACK: bytes = (
    b"\x9a\x00\x00\x0a\x01\x00\x00\x2b\x02\xfe\x80\x14\xb6\x1c\xff\x27\x10\x27\x10\x4a\x64\x00\xef\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x14\xd7\x00\x00\x03\x0b\x8e\x01\x0b\x89\x0b\x93\x0b\x9c\x0a\x0c\xf0\x10\x0c\xec\xa4\xb4\x9d"
)
CELLS: bytes = (
    b"\x9a\x00\x00\x0a\x02\x00\x00\x44\x02\x14\xb6\x10\x0c\xee\x0b\x89\x0c\xec\x0b\x8b\x0c\xee\x0b\x8e\x0c\xef\x0b\x8b"
    b"\x0c\xef\x00\x00\x0c\xee\x00\x00\x0c\xef\x00\x00\x0c\xef\x00\x00\x0c\xef\x00\x00\x0c\xf0\x00\x00\x0c\xef\x00\x00"
    b"\x0c\xee\x00\x00\x0c\xee\x00\x00\x0c\xef\x00\x00\x0c\xee\x00\x00\x0c\xec\x00\x00\x32\xa1\x9d"
)
BALANCE: bytes = b"\x9a\x00\x00\x0a\x03\x00\x00\x0d\x02\x00\x00\x26\x06\x00\xb7\x1f\x00\x00\x00\x00\x00\xfc\xeb\x9d"
SERIAL: bytes = b"\x9a\x00\x00\x00\x02\x00\x00\x0e\x0d\x31\x32\x33\x34\x35\x36\x37\x38\x39\x30\x31\x32\x5a\x7d\x7d\x9d"
VERSIONS: bytes = (
    b"\x9a\x00\x00\x00\x01\x00\x00\x43\x00\x15\x50\x31\x36\x53\x31\x30\x30\x41\x2d\x33\x31\x39\x31\x36\x2d\x31\x2e\x30\x36\x00\x00"
    b"\x20\x50\x31\x36\x53\x31\x30\x30\x41\x2d\x33\x31\x39\x31\x36\x2d\x31\x2e\x30\x36\x2d\x30\x30\x31\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x0a\x32\x31\x39\x33\x39\x56\x31\x34\x30\x00\x6c\xa7\x9d"
)
FRAMES: dict[int, bytes] = {0x0A00: SYSTEM, 0x0A01: PACK, 0x0A02: CELLS, 0x0A03: BALANCE, 0x0002: SERIAL, 0x0001: VERSIONS}


def make(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    return make_transport(serial_bluetooth_pace, monkeypatch, device, protocol_version="paceex_bms", **values)


def pack(slice_size: int = 20) -> FakeDevice:
    return FakeDevice(responder=lambda request: FRAMES.get((request[3] << 8) | request[4]), slice_size=slice_size)


# the requests the aiobmsble project sends to a real pack
REQUESTS: dict[int, str] = {
    0x0A00: "9a00000a0000000019519d",
    0x0A01: "9a00000a01000002" "01011b9c9d",
}


@pytest.mark.parametrize(("command", "expected"), REQUESTS.items())
def test_requests_match_those_sent_to_a_real_pack(monkeypatch: pytest.MonkeyPatch, command: int, expected: str) -> None:
    assert make(monkeypatch).build_frame(command).hex() == expected


@pytest.mark.parametrize("frame", FRAMES.values())
def test_the_captured_frames_decode(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    decoded = make(monkeypatch).decode_frame(frame)
    assert decoded is not None
    assert decoded.command == (frame[3] << 8) | frame[4]
    assert len(decoded.payload) == frame[7]


@pytest.mark.parametrize("frame", [
    SYSTEM[:-3] + b"\x00\x00\x9d",                    # wrong crc
    SYSTEM[:-1] + b"\x00",                            # wrong tail
    SYSTEM[:20] + SYSTEM[21:],                        # a byte is missing
])
def test_an_invalid_reply_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices_and_skips_noise() -> None:
    buffer: bytearray = bytearray(b"\x01\x02")
    found: list[bytes | None] = []
    for start in range(0, len(CELLS), 20):
        buffer += CELLS[start:start + 20]
        found.append(serial_bluetooth_pace.extract_frame(buffer))
    assert found[:-1] == [None] * (len(found) - 1)
    assert found[-1] == CELLS


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    corrupt: bytes = SYSTEM[:20] + b"\x00" + SYSTEM[21:]
    buffer: bytearray = bytearray(corrupt + PACK)
    assert serial_bluetooth_pace.extract_frame(buffer) == PACK


def test_it_polls_the_six_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._poll_commands() == [0x0001, 0x0002, 0x0A00, 0x0A01, 0x0A02, 0x0A03]


def test_it_reads_a_pack_and_decodes_what_the_real_pack_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack(slice_size=20))
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()

    assert out["voltage"] == approx(53.02)
    assert out["current"] == approx(-3.84)
    assert out["remaining_capacity"] == approx(74.23)
    assert out["design_capacity"] == 100
    assert out["state_of_charge"] == 74
    assert out["state_of_health"] == 100
    assert out["cycle_count"] == 239
    assert out["stack_voltage"] == approx(53.01)
    assert out["stack_current"] == approx(-4.23)
    assert out["stack_pack_count"] == 1
    assert out["stack_max_temperature"] == approx(22.7)
    assert out["stack_min_temperature"] == approx(22.2)
    assert out["balancing_mask"] == 0x1FB7
    assert out["serial_number"] == "123456789012Z"
    assert out["software_version"] == "P16S100A-31916-1.06"
    assert out["hardware_version"] == "21939V140"


def test_the_cell_voltages_and_the_temperatures_between_them(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack())
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()

    assert out["cell_count"] == 16
    assert [out[f"cell_{i}_voltage"] for i in (1, 2, 10, 16)] == [approx(3.310), approx(3.308), approx(3.312), approx(3.308)]
    assert [out[f"cell_temperature_{i}"] for i in (1, 2, 3, 4)] == [approx(22.2), approx(22.4), approx(22.7), approx(22.4)]


def test_sensors_and_cells_the_pack_does_not_have_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack())
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()
    assert "mosfet_temperature" not in out, "the pack reports 0 for a sensor it lacks, not -273 C"
    assert "ambient_temperature" not in out


def test_a_pack_with_fewer_cells_does_not_get_phantom_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    four_cells: bytearray = bytearray(CELLS)
    four_cells[11] = 4
    four_cells[-3:-1] = serial_bluetooth_pace.calculate_crc(bytes(four_cells[:-3])).to_bytes(2, "big")
    frames: dict[int, bytes] = {**FRAMES, 0x0A02: bytes(four_cells)}
    device: FakeDevice = FakeDevice(responder=lambda request: frames.get((request[3] << 8) | request[4]))
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()
    assert out["cell_count"] == 4
    assert "cell_4_voltage" in out
    assert "cell_5_voltage" not in out


def test_a_reply_to_another_command_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = FakeDevice(responder=lambda request: PACK, slice_size=20)
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        assert transport._read_command(0x0A02) is None
        assert transport._read_command(0x0A01) is not None
    finally:
        transport.cleanup()


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_pace.transport_type == "scraper"
    assert (serial_bluetooth_pace.SERVICE_UUID, serial_bluetooth_pace.NOTIFY_UUID, serial_bluetooth_pace.WRITE_UUID) == ("fff0", "fff1", "fff2")
