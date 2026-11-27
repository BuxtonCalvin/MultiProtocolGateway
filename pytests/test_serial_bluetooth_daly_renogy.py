# Description: Tests for the Modbus-over-Bluetooth family and the Daly and Renogy transports built on it, against the captured frames of those batteries
# File: test_serial_bluetooth_daly_renogy.py
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
Unit tests for classes.transports.serial_bluetooth_modbus, serial_bluetooth_daly and serial_bluetooth_renogy.

The frames are those of the aiobmsble test suite: a Daly board in each of its two variants and a Renogy RBT100LFP12-BT.
Several of the Daly values are synthetic (round numbers), so they prove the register arithmetic rather than a real pack.
"""

from __future__ import annotations

from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.transports.serial_bluetooth import serial_bluetooth_client
from classes.transports.serial_bluetooth_daly import serial_bluetooth_daly
from classes.transports.serial_bluetooth_modbus import serial_bluetooth_modbus
from classes.transports.serial_bluetooth_renogy import serial_bluetooth_renogy

DALY_D2: dict[str, str] = {
    "d2030000003ed7b9": (
        "d2037c101f10291033103d0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003c003d003e003f0000000000000000008c754e0384103d101f0000000000000d80000400040039000100000001102e0141002a0000000000000000a0df"
    ),
    "d203003e0009f7a3": (
        "d203120000000075300000004effffffffffffffff0b4e"
    ),
    "d20300a900208791": (
        "d203405430304b5f3332313034325f3131000048322e305f313033525f33303946394632303234303232390000000000000000000000000000000000000000000000005541"
    ),
}
DALY_X81: dict[str, str] = {
    "8103000000405bfa": (
        "5103800cd10c900ca30cd40000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ff00ff00ff00ff00ff00ff00ff00ff0082752800700048000400020cd400047201"
    ),
    "81030041003e8bce": (
        "51037c0002004400ff000000ff0000000000000000000000c9000000007530000000000000000100010000000000000cba00090000004500ff000000000000000075301a06050922104aec00000000000000000000753000300070000000000000000200008000000075300b7dfffefffe00007ff6ffffffffffffffff0002307a"
    ),
    "81030178004a5a18": (
        "51039431325f3236303132305f4b30305448322e315f313033455f33305846444c5f484b4d535f332e322e45003232314c443031313131313131310000000000000000000000000000323032363035333000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000b1cb"
    ),
}
RENOGY: dict[str, str] = {
    "300313880022455c": (
        "30034400040023002100210021000000000000000000000000000000000000000000000000000200aa00aa000000000000000000000000000000000000000000000000000000002574"
    ),
    "300313f0001c4495": (
        "3003380000000000060000000000c83230323130353236000000000000000020202020202020205242543130304c465031322d4254202030313030552f"
    ),
    "300313b20007a48a": (
        "30030effaa008800017bb0000186a0000fe01e"
    ),
    "300313ec0008855c": (
        "3003100000000000000000000000000000000096a1"
    ),
    "300314020008e41d": (
        "3003105242543130304c465031322d4254202058bb"
    ),
}


def frames(table: dict[str, str]) -> dict[bytes, bytes]:
    return {bytes.fromhex(request): bytes.fromhex(reply) for request, reply in table.items()}


def daly(monkeypatch: pytest.MonkeyPatch, variant: str, table: dict[str, str], slice_size: int = 20) -> Any:
    known: dict[bytes, bytes] = frames(table)
    return make_transport(serial_bluetooth_daly, monkeypatch, FakeDevice(lambda request: known.get(request), slice_size=slice_size), protocol_version=variant)


def renogy(monkeypatch: pytest.MonkeyPatch, slice_size: int = 20) -> Any:
    known: dict[bytes, bytes] = frames(RENOGY)
    return make_transport(serial_bluetooth_renogy, monkeypatch, FakeDevice(lambda request: known.get(request), slice_size=slice_size), protocol_version="renogy_bms")


def read(transport: Any) -> dict[str, Any]:
    try:
        transport.connect()
        return transport.read_data()
    finally:
        transport.cleanup()


def with_crc(body: bytes) -> bytes:
    return body + serial_bluetooth_modbus.calculate_crc(body).to_bytes(2, "little")


# --------------------------------------------------------------------------- #
# the family                                                                  #
# --------------------------------------------------------------------------- #

def test_the_family_is_abstract_and_its_members_are_scrapers() -> None:
    assert serial_bluetooth_modbus.transport_type == "base class"
    assert serial_bluetooth_daly.transport_type == "scraper"
    assert serial_bluetooth_renogy.transport_type == "scraper"


@pytest.mark.parametrize("unit", [1, 0x30, 0x51, 0xD2])
def test_the_frame_finder_accepts_any_unit_and_skips_noise(unit: int) -> None:
    frame: bytes = with_crc(bytes([unit, 3, 4, 0, 1, 0, 2]))
    buffer: bytearray = bytearray(b"\x00\xff" + frame[:5])
    assert serial_bluetooth_modbus.extract_frame(buffer) is None
    buffer += frame[5:]
    assert serial_bluetooth_modbus.extract_frame(buffer) == frame


def test_the_frame_finder_returns_a_refusal_whole() -> None:
    refusal: bytes = with_crc(bytes([0x30, 0x83, 2]))
    assert serial_bluetooth_modbus.extract_frame(bytearray(refusal)) == refusal


def test_the_frame_finder_recovers_from_a_corrupt_frame() -> None:
    good: bytes = with_crc(bytes([0x30, 3, 2, 0, 7]))
    assert serial_bluetooth_modbus.extract_frame(bytearray(good[:3] + b"\x55" + good[4:] + good)) == good


# --------------------------------------------------------------------------- #
# Daly                                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("variant", "table", "request_frame", "unit"), [
    ("daly_bms_d2", DALY_D2, "d2030000003ed7b9", 0xD2),
    ("daly_bms_x81", DALY_X81, "810300000040" "5bfa", 0x51),
])
def test_daly_requests_and_units(monkeypatch: pytest.MonkeyPatch, variant: str, table: dict[str, str], request_frame: str, unit: int) -> None:
    transport: Any = daly(monkeypatch, variant, table)
    assert transport.build_frame(0).hex() == request_frame
    assert transport._response_unit() == unit
    assert request_frame in table


def test_daly_d2_reads_the_captured_board(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(daly(monkeypatch, "daly_bms_d2", DALY_D2))

    assert out["voltage"] == approx(14.0)
    assert out["current"] == approx(3.0)
    assert out["state_of_charge"] == approx(90.0)
    assert out["cycle_count"] == 57
    assert out["remaining_capacity"] == approx(345.6)
    assert out["cell_count"] == 4
    assert [out[f"cell_{i}_voltage"] for i in (1, 2, 3, 4)] == [approx(4.127), approx(4.137), approx(4.147), approx(4.157)]
    assert [out[f"temperature_{i}"] for i in (1, 2, 3, 4)] == [20, 21, 22, 23]
    assert out["mosfet_temperature"] == 38
    assert out["balancer"] == 1
    assert out["charge_mosfet"] == 0
    assert out["discharge_mosfet"] == 1
    assert out["problem_code"] == 0
    assert out["software_version"] == "T00K_321042_11"
    assert out["hardware_version"] == "H2.0_103R_309F9F"


def test_daly_d2_does_not_invent_cells_or_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(daly(monkeypatch, "daly_bms_d2", DALY_D2))
    assert "cell_5_voltage" not in out
    assert "cell_32_voltage" not in out
    assert "temperature_5" not in out


def test_daly_x81_reads_the_captured_board(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(daly(monkeypatch, "daly_bms_x81", DALY_X81))

    assert out["voltage"] == approx(13.0)
    assert out["current"] == approx(-0.8)
    assert out["state_of_charge"] == approx(11.2)
    assert out["remaining_capacity"] == approx(20.1)
    assert out["cycle_count"] == 0
    assert out["cell_count"] == 4
    assert [out[f"cell_{i}_voltage"] for i in (1, 2, 3, 4)] == [approx(3.281), approx(3.216), approx(3.235), approx(3.284)]
    assert out["temperature_count"] == 2
    assert "temperature_3" not in out
    assert out["charge_mosfet"] == 1
    assert out["software_version"] == "12_260120_K00TH2.1_103E_30XF"
    assert out["hardware_version"] == "DL_HKMS_3.2.E"
    assert out["serial_number"] == "221LD011111111"


@pytest.mark.parametrize("size", [1, 20, 244])
def test_daly_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(daly(monkeypatch, "daly_bms_d2", DALY_D2, size))["voltage"] == approx(14.0)


def test_daly_d2_does_not_publish_a_mosfet_sensor_the_board_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    table: dict[str, str] = dict(DALY_D2)
    mos_request: str = with_crc(bytes([0xD2, 3, 0, 0x3E, 0, 9])).hex()
    mos_reply: bytearray = bytearray(bytes.fromhex(table[mos_request]))
    mos_reply[11:13] = b"\xff\xff"
    table[mos_request] = with_crc(bytes(mos_reply[:-2])).hex()
    assert "mosfet_temperature" not in read(daly(monkeypatch, "daly_bms_d2", table))


def test_daly_d2_board_is_not_mistaken_for_x81(monkeypatch: pytest.MonkeyPatch) -> None:
    """A board of the other variant does not answer, and the transport says so instead of decoding garbage."""
    transport: Any = daly(monkeypatch, "daly_bms_x81", DALY_D2)
    try:
        transport.connect()
        assert transport.read_data() == {}
    finally:
        transport.cleanup()


# --------------------------------------------------------------------------- #
# Renogy                                                                      #
# --------------------------------------------------------------------------- #

def test_renogy_requests_are_the_ones_the_module_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = renogy(monkeypatch)
    for command in transport._poll_commands():
        assert transport.build_frame(command).hex() in RENOGY


def test_renogy_reads_the_captured_battery(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(renogy(monkeypatch))

    assert out["current"] == approx(-0.86)
    assert out["voltage"] == approx(13.6)
    assert out["remaining_capacity"] == approx(97.2)
    assert out["design_capacity"] == approx(100.0)
    assert out["cycle_count"] == 15
    assert out["cell_count"] == 4
    assert [out[f"cell_{i}_voltage"] for i in (1, 2, 3, 4)] == [approx(3.5), approx(3.3), approx(3.3), approx(3.3)]
    assert out["temperature_1"] == approx(17.0)
    assert out["temperature_2"] == approx(17.0)
    assert out["serial_number"] == "20210526"
    assert out["device_name"] == "RBT100LFP12-BT"
    assert out["software_version"] == "0100"
    assert out["charge_mosfet"] == 1
    assert out["heater"] == 0


def test_renogy_does_not_invent_cells_or_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = read(renogy(monkeypatch))
    assert "cell_5_voltage" not in out
    assert "cell_16_voltage" not in out
    assert "temperature_3" not in out


@pytest.mark.parametrize("size", [1, 20, 244])
def test_renogy_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    assert read(renogy(monkeypatch, size))["voltage"] == approx(13.6)


def test_renogy_uses_both_of_its_services(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = renogy(monkeypatch)
    client: serial_bluetooth_client = transport._create_client()
    assert (client.service_uuid, client.extra_services) == ("ffd0", ("fff0",))
    assert (client.notify_uuid, client.write_uuid) == ("fff1", "ffd1")
    assert transport._request_unit() == 0x30
