# Description: Tests for the serial_bluetooth_jbd transport: the JBD frames over Bluetooth, checked against the manufacturer's document and against the serial_jbd transport
# File: test_serial_bluetooth_jbd.py
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
Unit tests for classes.transports.serial_bluetooth_jbd.

The vectors are the worked examples of the JBD protocol document, the same ones test_serial_jbd.py uses, so the
Bluetooth transport is held to the numbers the UART transport is, and to byte-for-byte the same frames.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bluetooth_support import FakeDevice, make_transport
from conftest import approx

from classes.protocol_settings import protocol_settings
from classes.transports.serial_bluetooth_jbd import serial_bluetooth_jbd
from classes.transports.serial_jbd import serial_jbd

REQUEST_03: bytes = bytes.fromhex("DDA50300FFFD77")
REQUEST_04: bytes = bytes.fromhex("DDA50400FFFC77")
REQUEST_05: bytes = bytes.fromhex("DDA50500FFFB77")
RESPONSE_03_EXAMPLE_1: bytes = bytes.fromhex("DD03001B1700000002D003E8000020780000000000001048030F020B760B82FBFF77")
RESPONSE_04_15_CELLS: bytes = bytes.fromhex("DD04001E0F660F630F630F640F3E0F630F370F5B0F650F3B0F630F630F3C0F660F3DF9F977")
RESPONSE_05_VERSION: bytes = bytes.fromhex("DD05000A30313233343536373839FDE977")
NARRATIVE_03: bytes = bytes.fromhex("DD03001F19DFF8240DA50FA00002249100000000000012570311040B980BA90B960B97F89A77")
NARRATIVE_04: bytes = bytes.fromhex("DD0400220EC80EC80ECB0ECF0ECA0EC70ECA0ECD0EC90ECA0ECB0ECB0EC80ECC0EC80EC90EC9F18777")


def make(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    return make_transport(serial_bluetooth_jbd, monkeypatch, device, protocol_version="jbd_bms", **values)


def payload_of(frame: bytes) -> bytes:
    return frame[4:4 + frame[3]]


def finder(*chunks: bytes) -> list[bytes | None]:
    """Feeds notification slices to the frame finder one at a time and returns what each call produced."""
    buffer: bytearray = bytearray()
    found: list[bytes | None] = []
    for chunk in chunks:
        buffer += chunk
        found.append(serial_bluetooth_jbd.extract_frame(buffer))
    return found


@pytest.mark.parametrize(("command", "expected"), [(3, REQUEST_03), (4, REQUEST_04), (5, REQUEST_05)])
def test_requests_match_the_documents_frames(monkeypatch: pytest.MonkeyPatch, command: int, expected: bytes) -> None:
    assert make(monkeypatch).build_frame(command) == expected


@pytest.mark.parametrize("command", [3, 4, 5])
def test_requests_are_the_same_bytes_the_uart_transport_sends(command: int) -> None:
    body: bytes = bytes([command, 0])
    assert serial_bluetooth_jbd.calculate_checksum(body) == serial_jbd.calculate_checksum(body)


@pytest.mark.parametrize("frame", [RESPONSE_03_EXAMPLE_1, RESPONSE_04_15_CELLS, RESPONSE_05_VERSION, NARRATIVE_03, NARRATIVE_04])
def test_the_documents_replies_decode(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    decoded: SimpleNamespace | None = make(monkeypatch).decode_frame(frame)
    assert decoded is not None
    assert decoded.command == frame[1]
    assert decoded.payload == payload_of(frame)


@pytest.mark.parametrize("frame", [
    RESPONSE_03_EXAMPLE_1[:-3] + b"\x00\x00\x77",    # wrong checksum
    RESPONSE_03_EXAMPLE_1[:-1] + b"\x00",            # wrong end marker
    RESPONSE_03_EXAMPLE_1[:10] + RESPONSE_03_EXAMPLE_1[11:],  # a byte is missing
    bytes.fromhex("DD0380" "00" "FFFD77"),           # the pack refused the command
])
def test_an_invalid_reply_is_rejected(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    assert make(monkeypatch).decode_frame(frame) is None


# --------------------------------------------------------------------------- #
# the frame finder                                                            #
# --------------------------------------------------------------------------- #

def test_a_frame_is_found_only_when_all_of_it_has_arrived() -> None:
    pieces: list[bytes] = [NARRATIVE_03[i:i + 20] for i in range(0, len(NARRATIVE_03), 20)]
    found: list[bytes | None] = finder(*pieces)
    assert found[:-1] == [None] * (len(pieces) - 1)
    assert found[-1] == NARRATIVE_03


def test_the_end_marker_inside_the_data_does_not_end_the_frame() -> None:
    """0x77 occurs in the data of this reply (a 0x0F77 cell); only the length byte says where the frame ends."""
    frame: bytes = bytes.fromhex("DD04000477770F77") + bytes.fromhex("0000")
    body: bytes = frame[2:8]
    complete: bytes = frame[:8] + ((0x10000 - sum(body)) & 0xFFFF).to_bytes(2, "big") + b"\x77"
    assert finder(complete[:6], complete[6:]) == [None, complete]


def test_noise_and_a_stray_start_byte_before_a_frame_are_skipped() -> None:
    assert finder(b"\x00\x41\x54\xdd\x01\xff" + RESPONSE_05_VERSION) == [RESPONSE_05_VERSION]


def test_two_frames_in_the_buffer_come_out_one_per_call() -> None:
    buffer: bytearray = bytearray(RESPONSE_05_VERSION + NARRATIVE_03)
    assert serial_bluetooth_jbd.extract_frame(buffer) == RESPONSE_05_VERSION
    assert serial_bluetooth_jbd.extract_frame(buffer) == NARRATIVE_03
    assert serial_bluetooth_jbd.extract_frame(buffer) is None


def test_a_start_byte_with_an_absurd_length_is_not_waited_for() -> None:
    buffer: bytearray = bytearray(b"\xdd\x03\x00\xff" + RESPONSE_05_VERSION)
    assert serial_bluetooth_jbd.extract_frame(buffer) == RESPONSE_05_VERSION


# --------------------------------------------------------------------------- #
# the whole path                                                              #
# --------------------------------------------------------------------------- #

def test_it_reads_a_pack_over_bluetooth_and_decodes_the_documents_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: dict[bytes, bytes] = {REQUEST_03: NARRATIVE_03, REQUEST_04: NARRATIVE_04, REQUEST_05: RESPONSE_05_VERSION}
    device: FakeDevice = FakeDevice(responder=replies.get, slice_size=20)
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        out: dict[str, Any] = transport.read_data()
    finally:
        transport.cleanup()

    assert [request[2] for request in device.written] == [5, 3, 4] or sorted(request[2] for request in device.written) == [3, 4, 5]
    assert out["total_voltage"] == approx(66.23)
    assert out["current"] == approx(-20.12)
    assert out["residual_capacity"] == approx(34.93)
    assert out["state_of_charge"] == 87
    assert out["cell_count"] == 17
    assert out["cell_17_voltage"] == approx(3.785)
    assert "cell_18_voltage" not in out
    assert out["hardware_version"] == "0123456789"


def test_the_jbd_protocol_is_reused_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = protocol_settings("jbd_bms")
    assert settings.settings["transport"] == "serial_jbd", "the UART protocol file is shared, so a config names this transport itself"
    assert make(monkeypatch)._poll_commands() == [3, 4, 5]


def test_the_transport_declares_itself_a_scraper() -> None:
    assert serial_bluetooth_jbd.transport_type == "scraper"
    assert (serial_bluetooth_jbd.SERVICE_UUID, serial_bluetooth_jbd.NOTIFY_UUID, serial_bluetooth_jbd.WRITE_UUID) == ("ff00", "ff01", "ff02")


# --------------------------------------------------------------------------- #
# the connection password                                                     #
# --------------------------------------------------------------------------- #

def auth_reply(status: int) -> bytes:
    body: bytes = bytes([0x15, 0x01, status])
    return b"\xff\xaa" + body + bytes([sum(body) & 0xFF])


def locked_pack(password: str, slice_size: int = 20) -> FakeDevice:
    """A pack that answers nothing until it has been given its password."""
    state: dict[str, bool] = {"unlocked": False}
    replies: dict[bytes, bytes] = {REQUEST_03: NARRATIVE_03, REQUEST_04: NARRATIVE_04, REQUEST_05: RESPONSE_05_VERSION}

    def respond(request: bytes) -> bytes | None:
        if request[:3] == b"\xff\xaa\x15":
            state["unlocked"] = request[4:4 + request[3]] == password.encode()
            return auth_reply(0 if state["unlocked"] else 1)
        return replies.get(request) if state["unlocked"] else None

    return FakeDevice(responder=respond, slice_size=slice_size)


def test_the_password_frame_is_ff_aa_15_with_a_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    frame: bytes = make(monkeypatch).build_frame(0x15, b"123456")
    assert frame == bytes.fromhex("FFAA1506313233343536") + bytes([(0x15 + 6 + sum(b"123456")) & 0xFF])


def test_a_pack_that_asks_for_a_password_is_read_once_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = locked_pack("123456")
    transport: Any = make(monkeypatch, device, password="123456")  # noqa: S106
    try:
        assert transport.connect()
        assert transport.read_data()["cell_count"] == 17
    finally:
        transport.cleanup()
    assert device.written[0][:3] == b"\xff\xaa\x15", "the password is the first thing sent"


def test_the_secret_setting_is_an_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, locked_pack("abc"), secret="abc")  # noqa: S106
    try:
        assert transport.connect()
    finally:
        transport.cleanup()


def test_a_pack_that_does_not_ask_for_one_is_not_sent_one(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = FakeDevice(responder={REQUEST_03: NARRATIVE_03, REQUEST_04: NARRATIVE_04, REQUEST_05: RESPONSE_05_VERSION}.get)
    transport: Any = make(monkeypatch, device)
    try:
        transport.connect()
        transport.read_data()
    finally:
        transport.cleanup()
    assert all(request[:2] == b"\xdd\xa5" for request in device.written)


def test_a_wrong_password_is_refused_and_the_link_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = locked_pack("123456")
    transport: Any = make(monkeypatch, device, password="000000")  # noqa: S106
    try:
        assert not transport.connect()
        assert not transport.connected
        assert transport.read_data() == {}
        assert not transport._link_up()
    finally:
        transport.cleanup()
    assert any("refused the password" in str(call) for call in transport._log.error.call_args_list)


def test_a_pack_that_does_not_answer_the_password_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakeDevice(responder=lambda request: None), password="123456")  # noqa: S106
    try:
        assert not transport.connect()
    finally:
        transport.cleanup()


def test_the_password_is_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, locked_pack("s3cr3tpw"), password="wrongpass")  # noqa: S106
    try:
        transport.connect()
    finally:
        transport.cleanup()
    logged: str = " ".join(str(call) for call in transport._log.mock_calls)
    assert "s3cr3tpw" not in logged
    assert "wrongpass" not in logged


@pytest.mark.parametrize("password", ["pässword", "x" * 33])
def test_a_password_that_cannot_be_sent_is_refused_at_start(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    with pytest.raises(ValueError, match="password"):
        make(monkeypatch, password=password)


def test_the_password_reply_is_found_among_noise_and_split_across_slices() -> None:
    reply: bytes = auth_reply(0)
    buffer: bytearray = bytearray(b"\x00\x01\xff")
    assert serial_bluetooth_jbd.extract_frame(buffer) is None
    buffer += reply[1:]
    assert serial_bluetooth_jbd.extract_frame(buffer) == reply


def test_a_data_frame_containing_ff_aa_is_not_mistaken_for_a_password_reply() -> None:
    body: bytes = bytes([0x04, 0x00, 0x04]) + b"\xff\xaa\xff\xaa"
    frame: bytes = b"\xdd" + body + ((0x10000 - sum(body[1:])) & 0xFFFF).to_bytes(2, "big") + b"\x77"
    assert serial_bluetooth_jbd.extract_frame(bytearray(frame)) == frame
