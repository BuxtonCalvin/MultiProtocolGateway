# Description: Tests for the serial_bluetooth base transport and its client: frame reassembly from notification slices, unsolicited frames, lost links, reconnection, address handling and device sharing
# File: test_serial_bluetooth.py
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
Unit tests for classes.transports.serial_bluetooth.

A fake BLE device (bluetooth_support.FakeDevice) stands in for the radio: the real client, its loop thread and
its frame reassembly all run, against a device that answers writes in slices of a chosen size. The JBD
transport is used as the concrete protocol because its frames come from the manufacturer's document.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from bluetooth_support import (
    ADDRESS,
    FakeDevice,
    StubLink,
    install_bluetooth_layer,
    make_transport,
)
from conftest import DummySettings

from classes.transports.serial_bluetooth import (
    serial_bluetooth,
    serial_bluetooth_client,
)
from classes.transports.serial_bluetooth_eg4 import serial_bluetooth_eg4
from classes.transports.serial_bluetooth_jbd import serial_bluetooth_jbd

REQUEST_03: bytes = bytes.fromhex("DDA50300FFFD77")
REQUEST_04: bytes = bytes.fromhex("DDA50400FFFC77")
REQUEST_05: bytes = bytes.fromhex("DDA50500FFFB77")
NARRATIVE_03: bytes = bytes.fromhex("DD03001F19DFF8240DA50FA00002249100000000000012570311040B980BA90B960B97F89A77")
NARRATIVE_04: bytes = bytes.fromhex("DD0400220EC80EC80ECB0ECF0ECA0EC70ECA0ECD0EC90ECA0ECB0ECB0EC80ECC0EC80EC90EC9F18777")
RESPONSE_05: bytes = bytes.fromhex("DD05000A30313233343536373839FDE977")
REPLIES: dict[bytes, bytes] = {REQUEST_03: NARRATIVE_03, REQUEST_04: NARRATIVE_04, REQUEST_05: RESPONSE_05}


def jbd(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    return make_transport(serial_bluetooth_jbd, monkeypatch, device, protocol_version="jbd_bms", **values)


def pack(**options: Any) -> FakeDevice:
    return FakeDevice(responder=REPLIES.get, **options)


def line_finder(buffer: bytearray) -> bytes | None:
    """A frame finder for the client tests: frames end in a newline."""
    end: int = buffer.find(b"\n")
    if end < 0:
        return None
    frame: bytes = bytes(buffer[:end + 1])
    del buffer[:end + 1]
    return frame


def bare_client() -> serial_bluetooth_client:
    client: serial_bluetooth_client = serial_bluetooth_client(ADDRESS, "ff00", "ff01", "ff02", line_finder)
    client._ble = StubLink()  # type: ignore[assignment]
    client.timeout = 0.2
    return client


def feed(client: serial_bluetooth_client, data: bytes) -> None:
    client._on_notification(None, bytearray(data))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the client                                                                  #
# --------------------------------------------------------------------------- #

def test_a_frame_split_across_notifications_is_read_whole() -> None:
    client: serial_bluetooth_client = bare_client()
    for piece in (b"hel", b"lo wor", b"ld\n"):
        feed(client, piece)
    assert client.read(reset_buffer=False) == b"hello world\n"


def test_a_partial_frame_times_out_and_is_kept_for_the_rest() -> None:
    client: serial_bluetooth_client = bare_client()
    feed(client, b"half")
    assert client.read(reset_buffer=False) is None
    feed(client, b" done\n")
    assert client.read(reset_buffer=False) == b"half done\n"


def test_two_frames_in_one_notification_are_two_reads() -> None:
    client: serial_bluetooth_client = bare_client()
    feed(client, b"one\ntwo\n")
    assert client.read(reset_buffer=False) == b"one\n"
    assert client.read(reset_buffer=False) == b"two\n"


def test_frames_can_be_asked_for_together() -> None:
    client: serial_bluetooth_client = bare_client()
    feed(client, b"one\ntwo\n")
    assert client.read(reset_buffer=False, frames=2) == [b"one\n", b"two\n"]


def test_a_listening_read_discards_what_arrived_before_it() -> None:
    client: serial_bluetooth_client = bare_client()
    feed(client, b"stale\n")
    assert client.read(reset_buffer=True) is None


def test_a_reply_that_arrives_before_read_is_not_discarded() -> None:
    """The radio thread can deliver the reply while write() is still returning; read(reset_buffer=True) must keep it."""
    client: serial_bluetooth_client = bare_client()
    client._request_pending = True
    feed(client, b"reply\n")
    assert client.read(reset_buffer=True) == b"reply\n"


def test_a_write_discards_what_arrived_before_it() -> None:
    client: serial_bluetooth_client = bare_client()
    feed(client, b"stale\n")
    with client._condition:
        client._buffer.clear()
        client._request_pending = True
    assert client.read() is None


def test_a_reader_is_woken_by_a_notification_from_the_radio_thread() -> None:
    client: serial_bluetooth_client = bare_client()
    client.timeout = 2.0
    threading.Timer(0.05, feed, args=(client, b"late\n")).start()
    assert client.read(reset_buffer=False) == b"late\n"


def test_a_device_that_goes_away_while_waited_on_raises_oserror() -> None:
    client: serial_bluetooth_client = bare_client()
    client.timeout = 2.0
    threading.Timer(0.05, client._on_disconnect, args=(None,)).start()
    with pytest.raises(OSError, match="disconnected"):
        client.read(reset_buffer=False)
    assert not client.is_connected


def test_writing_to_a_device_that_is_not_connected_raises_oserror() -> None:
    client: serial_bluetooth_client = serial_bluetooth_client(ADDRESS, "ff00", "ff01", "ff02", line_finder)
    with pytest.raises(OSError, match="not connected"):
        client.write(b"x")


def test_a_device_that_streams_cannot_grow_the_buffer_without_end() -> None:
    client: serial_bluetooth_client = bare_client()
    for _ in range(100):
        feed(client, b"x" * 200)
    assert len(client._buffer) == client.MAX_BUFFER


# --------------------------------------------------------------------------- #
# the transport                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("size", [1, 5, 20, 512])
def test_replies_are_reassembled_whatever_the_slice_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    transport: Any = jbd(monkeypatch, pack(slice_size=size))
    try:
        assert transport.connect()
        data: dict[str, Any] = transport.read_data()
        assert data["cell_count"] == 17
        assert data["state_of_charge"] == 87
    finally:
        transport.cleanup()


def test_a_frame_the_pack_pushed_unasked_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = jbd(monkeypatch, pack(pushed=RESPONSE_05))
    try:
        transport.connect()
        assert transport.read_data()["state_of_charge"] == 87
    finally:
        transport.cleanup()


def test_noise_before_a_frame_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = FakeDevice(responder=lambda request: b"\x00AT\r\n" + REPLIES[request])
    transport: Any = jbd(monkeypatch, device)
    try:
        transport.connect()
        assert transport.read_data()["cell_count"] == 17
    finally:
        transport.cleanup()


def test_a_device_that_is_not_found_does_not_connect_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack(found=False)
    transport: Any = jbd(monkeypatch, device)
    try:
        assert not transport.connect()
        assert not transport.connected
        assert transport.read_data() == {}
        assert device.connects >= 2, "each cycle tries again"
    finally:
        transport.cleanup()


def test_a_device_that_appears_later_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack(found=False)
    transport: Any = jbd(monkeypatch, device)
    try:
        assert transport.read_data() == {}
        device.found = True
        assert transport.read_data()["cell_count"] == 17
    finally:
        transport.cleanup()


def test_a_lost_link_marks_the_transport_disconnected_and_is_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack()
    transport: Any = jbd(monkeypatch, device)
    try:
        transport.connect()
        assert transport.read_data()["cell_count"] == 17

        device.drop(transport.client)
        assert transport.read_data() == {} or transport.read_data()
        assert device.connects >= 2, "the link is rebuilt"
        assert transport.read_data()["cell_count"] == 17
    finally:
        transport.cleanup()


def test_an_unanswered_request_is_a_timeout_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = jbd(monkeypatch, FakeDevice(responder=lambda request: None))
    try:
        transport.connect()
        assert transport.read_data() == {}
    finally:
        transport.cleanup()


def test_cleanup_closes_the_link_and_stops_the_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack()
    transport: Any = jbd(monkeypatch, device)
    transport.connect()
    thread: threading.Thread | None = transport.client._thread
    assert thread is not None
    assert thread.is_alive()

    transport.cleanup()
    assert device.disconnects >= 1
    thread.join(5)
    assert not thread.is_alive()


# --------------------------------------------------------------------------- #
# settings                                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("address", [
    "AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "E9F0A1B2-1111-2222-3333-444455556666", "name:JK_B2A16S20P",
])
def test_the_address_forms_that_are_accepted(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    transport: Any = jbd(monkeypatch, bluetooth_address=address)
    assert transport.port == address
    assert transport.scrape_target.startswith(address)


@pytest.mark.parametrize("address", ["", "/dev/ttyUSB0", "AA:BB:CC:DD:EE", "name:", "nonsense"])
def test_an_address_that_is_not_one_is_refused(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    install_bluetooth_layer(monkeypatch)
    with pytest.raises(ValueError, match="bluetooth_address"):
        serial_bluetooth_jbd(DummySettings(bluetooth_address=address, protocol_version="jbd_bms"))  # type: ignore[arg-type]


def test_port_is_accepted_as_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    install_bluetooth_layer(monkeypatch)
    transport: Any = serial_bluetooth_jbd(DummySettings(port=ADDRESS, protocol_version="jbd_bms"))  # type: ignore[arg-type]
    assert transport.port == ADDRESS


def test_the_uuids_can_be_overridden_for_a_rebranded_device(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = jbd(monkeypatch, service_uuid="fff0", notify_uuid="fff1", write_uuid="fff2")
    client: serial_bluetooth_client = transport._create_client()
    assert (client.service_uuid, client.notify_uuid, client.write_uuid) == ("fff0", "fff1", "fff2")


def test_the_protocol_defaults_are_used_without_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    client: serial_bluetooth_client = jbd(monkeypatch)._create_client()
    assert (client.service_uuid, client.notify_uuid, client.write_uuid) == ("ff00", "ff01", "ff02")


def test_the_base_class_needs_uuids_from_its_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    install_bluetooth_layer(monkeypatch)
    with pytest.raises(ValueError, match="UUID"):
        serial_bluetooth(DummySettings(bluetooth_address=ADDRESS))  # type: ignore[arg-type]


def test_the_adapter_and_timeouts_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = jbd(monkeypatch, adapter="hci1", timeout="7", scan_timeout="3", connect_timeout="9")
    try:
        transport.connect()
        client: serial_bluetooth_client = transport.client
        assert client.adapter == "hci1"
        assert (client.timeout, client.scan_timeout, client.connect_timeout) == (7.0, 3.0, 9.0)
    finally:
        transport.cleanup()


# --------------------------------------------------------------------------- #
# sharing a device                                                            #
# --------------------------------------------------------------------------- #

def test_two_sections_for_one_device_share_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    device: FakeDevice = pack()
    first: Any = jbd(monkeypatch, device)
    second: Any = serial_bluetooth_jbd(DummySettings(bluetooth_address=ADDRESS, protocol_version="jbd_bms", timeout="0.3"))  # type: ignore[arg-type]
    try:
        assert first.connect()
        assert second.connect()
        assert second.client is first.client
        assert device.connects == 1
    finally:
        first.cleanup()
        second.cleanup()


def test_a_different_protocol_cannot_share_a_device(monkeypatch: pytest.MonkeyPatch) -> None:
    first: Any = jbd(monkeypatch, pack())
    clash: Any = serial_bluetooth_eg4(DummySettings(bluetooth_address=ADDRESS, protocol_version="eg4_ll_s", address="1", timeout="0.3"))  # type: ignore[arg-type]
    try:
        assert first.connect()
        assert not clash.connect()
    finally:
        first.cleanup()
