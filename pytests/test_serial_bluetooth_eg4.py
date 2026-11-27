# Description: Tests for the serial_bluetooth_eg4 transport: Modbus RTU over the EG4 Bluetooth service, decoded with the eg4_ll_s register map
# File: test_serial_bluetooth_eg4.py
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
Unit tests for classes.transports.serial_bluetooth_eg4.

The status frame is a capture from an EG4 12V pack (4 cells, 13.24 V, 48 percent) as published in the
aiobmsble test suite. Its current is the one open question: that project reads it in units of 0.1 A, while the
eg4_ll_s map (verified on a 48V LL-S) uses 0.01 A, so the current is not asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth_eg4 import serial_bluetooth_eg4

STATUS: bytes = (
    b"\x01\x03\x4e\x05\x2c\xff\xc2\x0c\xee\x0c\xf1\x0c\xed\x0c\xf1\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00"
    b"\x11\x00\x11\x00\x00\x02\xee\x00\x64\x00\x30\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x01\x00\x00\x00\x00\x11\xc8\x00\x00\x00\x00\x00\x04\x00\x00\x00\x00\x95\xa4"
)
REQUEST_STATUS: bytes = bytes.fromhex("010300000027" "040E")


def with_crc(body: bytes) -> bytes:
    return body + serial_bluetooth_eg4.calculate_crc(body).to_bytes(2, "little")


def info_frame(text: str = "EG4-LL", firmware: str = "1.2", serial: str = "SN123456") -> bytes:
    """A reply to the 46 register read from register 105: model (12 registers), firmware (3), serial (8), and the rest zero."""
    data: bytes = text.encode().ljust(24, b"\0") + firmware.encode().ljust(6, b"\0") + serial.encode().ljust(16, b"\0")
    return with_crc(bytes([1, 3, 92]) + data.ljust(92, b"\0"))


def make(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    return make_transport(serial_bluetooth_eg4, monkeypatch, device, **{"protocol_version": "eg4_ll_s", "address": "1", **values})


def pack(slice_size: int = 20) -> FakeDevice:
    return FakeDevice(responder=lambda request: {0: STATUS, 105: info_frame()}.get(int.from_bytes(request[2:4], "big")), slice_size=slice_size)


def test_the_captured_status_frame_has_a_valid_crc(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded = make(monkeypatch).decode_frame(STATUS)
    assert decoded is not None
    assert len(decoded.payload) == 78


def test_the_status_request_is_a_modbus_read_of_39_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    assert transport.build_frame(0) == with_crc(bytes.fromhex("010300000027"))
    assert transport.build_frame(105) == with_crc(bytes.fromhex("01030069002E"))


def test_the_unit_id_is_the_address_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch, address="7").build_frame(0)[0] == 7


@pytest.mark.parametrize("address", ["0", "248"])
def test_a_unit_id_outside_the_modbus_range_is_refused(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    with pytest.raises(ValueError, match="address"):
        make(monkeypatch, address=address)


@pytest.mark.parametrize("frame", [
    STATUS[:-2] + b"\x00\x00",                        # wrong crc
    with_crc(bytes([2, 3, 2, 0, 1])),                 # another unit
    with_crc(bytes([1, 0x83, 2])),                    # the pack refused the read
    with_crc(bytes([1, 3, 4, 0, 1])),                 # byte count does not match the data
])
def test_an_invalid_reply_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


def test_the_frame_finder_joins_slices_and_skips_noise() -> None:
    buffer: bytearray = bytearray(b"\x00\xff")
    found: bytes | None = None
    for start in range(0, len(STATUS), 20):
        buffer += STATUS[start:start + 20]
        found = serial_bluetooth_eg4.extract_frame(buffer)
        if start + 20 < len(STATUS):
            assert found is None
    assert found == STATUS


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    corrupt: bytes = STATUS[:30] + b"\xaa" + STATUS[31:]
    buffer: bytearray = bytearray(corrupt + STATUS)
    assert serial_bluetooth_eg4.extract_frame(buffer) == STATUS


def test_the_two_windows_are_the_commands_and_the_info_window_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    assert transport._poll_commands() == [0, 105]
    assert transport._startup_commands() == {105}


def test_it_reads_a_pack_over_bluetooth(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack(slice_size=20))
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()

    assert out["pack_voltage"] == approx(13.24)
    assert out["soc"] == 48
    assert out["soh"] == 100
    assert out["cell_01_voltage"] == approx(3.310)
    assert out["cell_04_voltage"] == approx(3.313)
    assert out["model"] == "EG4-LL"
    assert out["serial_no"] == "SN123456"


def test_cells_the_pack_does_not_have_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack())
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()
    assert "cell_04_voltage" in out
    assert "cell_05_voltage" not in out
    assert "cell_16_voltage" not in out


def test_the_info_window_is_not_asked_for_again_each_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack()
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        transport.read_data()
        transport.read_data()
    finally:
        transport.cleanup()
    firsts: list[int] = [int.from_bytes(request[2:4], "big") for request in device.written]
    assert firsts.count(105) == 1
    assert firsts.count(0) == 2


def test_the_eg4_ll_s_protocol_is_reused_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).protocolSettings.protocol == "eg4_ll_s"
    assert serial_bluetooth_eg4.transport_type == "scraper"
    assert (serial_bluetooth_eg4.SERVICE_UUID, serial_bluetooth_eg4.NOTIFY_UUID, serial_bluetooth_eg4.WRITE_UUID) == ("1000", "1002", "1001")
