# Description: Tests for the serial_bluetooth_jk transport: JK02 frames over Bluetooth, decoded from a captured JK-PB2A16S20P frame
# File: test_serial_bluetooth_jk.py
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
Unit tests for classes.transports.serial_bluetooth_jk.

The device information and status frames are captures from a JK-PB2A16S20P (16 cells, firmware 15.38) as published
in the aiobmsble test suite: 53.224 V, +31.881 A charging, 25 percent, 9 cycles.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_jk import serial_bluetooth_jk

DEVICE_INFO: bytes = bytes.fromhex(
    "55aaeb9003214a4b5f50423241313653323050000000313541000000000031352e3338000000204801000500000034313031"
    "3834393235353500500000003132333400000000000000000000000032353032313000003431303138343932353535003030"
    "30004a4b2d424d5300000000000000000000373132303300000000000000000000004a4b2d424d5300000000000000000000"
    "feffffff8fe91d0200000000901f00000000c0d8e7fe3f00000100000000000000000002ff67000000000000000000000000"
    "0000ff0f000000000000000000000000000001ff670000000000000000000000000009080001640000005f0000003c000000"
    "320000000000000000000000100e00000a50011e0000000000000000000000000000000000fe9fe9ff0f00000000000000f1"
)
STATUS: bytes = bytes.fromhex(
    "55aaeb9002ac050dfe0cfe0c010d010dfd0cfb0c010dfc0cfb0cfe0cfb0cf80cfb0cfb0c090d000000000000000000000000"
    "0000000000000000000000000000000000000000ffff0000ff0c10000f0c40003d0040003d0041003f0041003e0041003e00"
    "41003d0040003e0041003f000000000000000000000000000000000000000000000000000000000000000000810000000000"
    "e8cf00004ae41900897c000086008000000000000000001986c00000400d030009000000b15f1c00640000008c4c76010101"
    "0000000000000000000000000000ff0001000000050452000000d44e404000000000ca1400000001010100060000ea670000"
    "00000000cd00c300bf00bd03287f030a1d0000008051010000000100a50202000000000000feff7fdc2f0101b0cf070000bb"
)
READY: bytes = bytes.fromhex("aa5590ebc8010100000000000000000000000044")


def make(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    return make_transport(serial_bluetooth_jk, monkeypatch, device, protocol_version="jk_bms_ble", **values)


def with_crc(frame: bytes) -> bytes:
    """Seals a 300 byte frame whose last byte is to be the checksum."""
    return frame[:-1] + bytes([serial_bluetooth_jk.calculate_checksum(frame[:-1])])


def bms(device_info: bytes = DEVICE_INFO, status: bytes = STATUS, slice_size: int = 20, **options: Any) -> FakeDevice:
    return FakeDevice(responder=lambda request: device_info if request[4] == 0x97 else status, slice_size=slice_size, **options)


def read(monkeypatch: pytest.MonkeyPatch, device: FakeDevice) -> dict[str, Any]:
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        return transport.read_data()
    finally:
        transport.cleanup()


def test_the_captured_frames_are_valid_status_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    assert transport.decode_frame(DEVICE_INFO).type == 0x03
    assert transport.decode_frame(STATUS).type == 0x02


@pytest.mark.parametrize(("command", "expected"), [
    (0x97, "aa5590eb9700" + "00" * 13 + "11"),
    (0x96, "aa5590eb9600" + "00" * 13 + "10"),
])
def test_requests_are_the_20_byte_frames_the_jk_app_sends(monkeypatch: pytest.MonkeyPatch, command: int, expected: str) -> None:
    frame: bytes = make(monkeypatch).build_frame(command)
    assert len(frame) == 20
    assert frame.hex() == expected


@pytest.mark.parametrize("frame", [
    STATUS[:-1] + bytes([(STATUS[-1] + 1) & 0xFF]),   # wrong checksum
    STATUS[:100],                                      # truncated
    b"\x00" * 300,                                     # not a status frame
])
def test_an_invalid_frame_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices() -> None:
    buffer: bytearray = bytearray()
    found: list[bytes | None] = []
    for start in range(0, len(STATUS), 20):
        buffer += STATUS[start:start + 20]
        found.append(serial_bluetooth_jk.extract_frame(buffer))
    assert found[:-1] == [None] * (len(found) - 1)
    assert found[-1] == STATUS


def test_the_ready_message_is_a_frame_of_its_own() -> None:
    buffer: bytearray = bytearray(READY + STATUS)
    assert serial_bluetooth_jk.extract_frame(buffer) == READY
    assert serial_bluetooth_jk.extract_frame(buffer) == STATUS


def test_the_text_some_bluetooth_modules_add_is_removed() -> None:
    buffer: bytearray = bytearray(b"AT\r\n" + STATUS + b"AT\r\n" + READY + b"AT\r\n")
    assert serial_bluetooth_jk.extract_frame(buffer) == STATUS
    assert serial_bluetooth_jk.extract_frame(buffer) == READY
    assert serial_bluetooth_jk.extract_frame(buffer) is None
    assert not buffer


def test_a_start_marker_split_across_notifications_is_not_lost() -> None:
    buffer: bytearray = bytearray(b"\x00\x00\x55\xaa")
    assert serial_bluetooth_jk.extract_frame(buffer) is None
    buffer += STATUS[2:]
    assert serial_bluetooth_jk.extract_frame(buffer) == STATUS


def test_it_reads_the_captured_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(monkeypatch, bms())

    assert out["total_voltage"] == approx(53.224)
    assert out["current"] == approx(31.881)
    assert out["state_of_charge"] == 25
    assert out["state_of_health"] == 100
    assert out["remaining_capacity"] == approx(49.286)
    assert out["nominal_capacity"] == approx(200.0)
    assert out["cycle_count"] == 9
    assert out["charge_mosfet"] == 1
    assert out["discharge_mosfet"] == 1
    assert out["average_cell_voltage"] == approx(3.327)
    assert out["cell_1_voltage"] == approx(3.333)
    assert out["cell_16_voltage"] == approx(3.337)
    assert out["mosfet_temperature"] == approx(12.9)
    assert out["battery_temperature_1"] == approx(13.4)
    assert out["battery_temperature_2"] == approx(12.8)
    assert out["mosfet_temperature_2"] == approx(20.5)
    assert out["battery_temperature_3"] == approx(19.5)
    assert out["battery_temperature_4"] == approx(19.1)


def test_the_device_information_is_read_once_and_published(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = bms()
    out: dict[str, Any] = read(monkeypatch, device)
    assert out["model"] == "JK_PB2A16S20P"
    assert out["software_version"] == "15.38"
    assert out["hardware_version"] == "15A"
    assert [request[4] for request in device.written].count(0x97) == 1


def test_cells_that_are_not_in_the_cell_mask_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(monkeypatch, bms())
    assert "cell_16_voltage" in out
    assert "cell_17_voltage" not in out
    assert "cell_32_voltage" not in out
    assert "cell_17_resistance" not in out


def test_the_cell_mask_decides_which_cells_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    changed: bytearray = bytearray(STATUS)
    changed[70:74] = (0x0000000F).to_bytes(4, "little")
    out: dict[str, Any] = read(monkeypatch, bms(status=with_crc(bytes(changed))))
    assert sorted(int(name.split("_")[1]) for name in out if name.endswith("_voltage") and name.startswith("cell_")) == [1, 2, 3, 4]


def test_a_sensor_that_reports_minus_200_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    changed: bytearray = bytearray(STATUS)
    changed[164:166] = (-2000).to_bytes(2, "little", signed=True)
    out: dict[str, Any] = read(monkeypatch, bms(status=with_crc(bytes(changed))))
    assert "battery_temperature_2" not in out
    assert "battery_temperature_1" in out


def test_firmware_before_14_has_no_third_and_fourth_battery_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    info: bytearray = bytearray(DEVICE_INFO)
    info[30:38] = b"11.48".ljust(8, b"\0")
    out: dict[str, Any] = read(monkeypatch, bms(device_info=with_crc(bytes(info))))
    assert "mosfet_temperature_2" in out
    assert "battery_temperature_3" not in out
    assert "battery_temperature_4" not in out


def test_firmware_with_the_old_24_cell_layout_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    info: bytearray = bytearray(DEVICE_INFO)
    info[30:38] = b"10.20".ljust(8, b"\0")
    device: FakeDevice = bms(device_info=with_crc(bytes(info)))
    transport: Any = make(monkeypatch, device)
    try:
        assert not transport.connect()
        assert not transport.connected
        assert transport.read_data() == {}
    finally:
        transport.cleanup()


def test_frames_the_bms_pushes_between_replies_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_frame: bytearray = bytearray(STATUS)
    settings_frame[4] = 0x01
    out: dict[str, Any] = read(monkeypatch, bms(pushed=with_crc(bytes(settings_frame)) + READY))
    assert out["state_of_charge"] == 25


@pytest.mark.parametrize("size", [1, 20, 244])
def test_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(monkeypatch, bms(slice_size=size))["total_voltage"] == approx(53.224)


def test_a_bms_that_never_answers_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakeDevice(responder=lambda request: None))
    try:
        assert not transport.connect()
        assert transport.read_data() == {}
    finally:
        transport.cleanup()


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_jk.transport_type == "scraper"
    assert (serial_bluetooth_jk.SERVICE_UUID, serial_bluetooth_jk.NOTIFY_UUID, serial_bluetooth_jk.WRITE_UUID) == ("ffe0", "ffe1", "ffe1")
