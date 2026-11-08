# Description: Unit tests for the length-prefixed serial framing family (serial_length_client and serial_length_transport).
# File: test_serial_length_transport.py
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
# These tests deliberately exercise protected members (_read_command, _check_shared_client, ...).
"""Unit tests for classes.transports.serial_length_transport, through a fake serial port (no hardware)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from conftest import DummySettings
from serial_support import FakePort, install_serial_layer, unplug

from classes.transports.serial_base import serial_base
from classes.transports.serial_length_transport import (
    serial_length_client,
    serial_length_transport,
)

JBD_LAYOUT: tuple[bytes, int, int, int, int] = (b"\xdd", 3, 1, 4, 512)
''' start, length offset, length size, length adjust, max frame: the JBD frame shape '''
JK_LAYOUT: tuple[bytes, int, int, int, int] = (b"\x4e\x57", 2, 2, 0, 1024)


def jbd_frame(command: int, data: bytes, status: int = 0) -> bytes:
    """A JBD response, built independently of the transport: DD, command, status, length, data, checksum, 77."""
    body: bytes = bytes([status, len(data)]) + data
    checksum: int = (0x10000 - sum(body)) & 0xFFFF
    return bytes([0xDD, command]) + body + checksum.to_bytes(2, "big") + b"\x77"


def make_client(port: FakePort, layout: tuple[bytes, int, int, int, int] = JBD_LAYOUT, timeout: float = 0.2) -> serial_length_client:
    client: serial_length_client = serial_length_client("/dev/ttyUSB0", 9600, *layout)
    client.timeout = timeout
    assert client.client is port  # pyright: ignore[reportUnnecessaryComparison]
    return client


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePort]:
    yield install_serial_layer(monkeypatch)


# --------------------------------------------------------------------------- #
# the client                                                                  #
# --------------------------------------------------------------------------- #

def test_a_frame_is_read_by_its_length(port: FakePort) -> None:
    frame: bytes = bytes.fromhex("DD 05 00 0A 30 31 32 33 34 35 36 37 38 39 FD E9 77".replace(" ", ""))
    port.queue(frame)
    assert make_client(port).read() == frame


def test_a_frame_that_contains_the_end_byte_is_not_cut_short(port: FakePort) -> None:
    """The reason this client exists: a 2.935 V cell is the two bytes 0B 77, and scanning for 77 would stop there."""
    frame: bytes = jbd_frame(0x04, bytes.fromhex("0B77 0EC8 0EC9"))
    assert frame.index(0x77) < len(frame) - 1, "the test frame must contain 77 before its end"
    port.queue(frame)
    assert make_client(port).read() == frame


def test_bytes_before_the_start_marker_are_discarded(port: FakePort) -> None:
    frame: bytes = jbd_frame(0x03, b"\x01\x02\x03")
    port.queue(b"\x00\x13\x37" + frame)
    assert make_client(port).read() == frame


def test_a_frame_split_across_reads_is_assembled(port: FakePort) -> None:
    frame: bytes = jbd_frame(0x04, bytes(range(30)))
    port.chunk_size = 3
    port.queue(frame)
    assert make_client(port).read() == frame


def test_an_implausible_length_is_skipped_and_the_next_frame_found(port: FakePort) -> None:
    """A stray start byte followed by a huge length must not make the client wait for bytes that never come."""
    real: bytes = jbd_frame(0x03, b"\xaa\xbb")
    layout: tuple[bytes, int, int, int, int] = (b"\xdd", 3, 1, 4, 64)  # frames over 64 bytes are noise
    port.queue(b"\xdd\x03\x00\xff" + real)
    assert make_client(port, layout).read() == real


def test_two_frames_in_one_read_are_returned_one_at_a_time(port: FakePort) -> None:
    first: bytes = jbd_frame(0x03, b"\x01")
    second: bytes = jbd_frame(0x04, b"\x02\x03")
    port.queue(first + second)
    client: serial_length_client = make_client(port)
    assert client.read() == first
    assert client.read(reset_buffer=False) == second


def test_resetting_the_buffer_drops_what_was_left_over(port: FakePort) -> None:
    first: bytes = jbd_frame(0x03, b"\x01")
    second: bytes = jbd_frame(0x04, b"\x02")
    port.queue(first + second)
    client: serial_length_client = make_client(port, timeout=0.05)
    assert client.read() == first
    assert client.read() is None, "the default read starts from nothing, as a request/response exchange should"


def test_it_times_out_without_a_frame(port: FakePort) -> None:
    assert make_client(port, timeout=0.05).read() is None


def test_a_partial_frame_that_never_completes_times_out(port: FakePort) -> None:
    port.queue(jbd_frame(0x03, b"\x01\x02\x03\x04")[:-3])
    assert make_client(port, timeout=0.05).read() is None


def test_a_two_byte_length_field_and_a_two_byte_start_marker(port: FakePort) -> None:
    """The JK shape: start 4E 57, a 2-byte length counting everything after the start marker."""
    body: bytes = b"\x00\x0a" + b"\x01\x02\x03\x04\x05\x06\x07\x08"  # length 10 = itself plus 8 more bytes
    frame: bytes = b"\x4e\x57" + body
    port.queue(b"\x4e" + frame)  # a lone 4E before the real marker
    assert make_client(port, JK_LAYOUT).read() == frame


def test_write_sends_the_request_exactly_as_built(port: FakePort) -> None:
    client: serial_length_client = make_client(port)
    client.write(b"\xdd\xa5\x03\x00\xff\xfd\x77")
    assert port.written == [b"\xdd\xa5\x03\x00\xff\xfd\x77"]


def test_close_is_idempotent_and_connect_needs_nothing(port: FakePort) -> None:
    client: serial_length_client = make_client(port)
    assert client.connect() is True
    client.close()
    client.close()
    assert port.close_calls == 1


def test_the_signature_describes_how_frames_are_found(port: FakePort) -> None:
    assert make_client(port).signature == (b"\xdd", 3, 1, 4)
    assert make_client(port, JK_LAYOUT).signature == (b"\x4e\x57", 2, 2, 0)


# --------------------------------------------------------------------------- #
# the family transport                                                        #
# --------------------------------------------------------------------------- #

class Echo(serial_length_transport):
    """A minimal protocol on the family: AA <length> <command> <data...>, the length counting what follows it."""

    transport_type: str = "scraper"
    START: bytes = b"\xaa"
    LENGTH_OFFSET: int = 1
    LENGTH_SIZE: int = 1
    LENGTH_ADJUST: int = 1
    ''' a frame is the start byte and the length byte (2 bytes, of which LENGTH_OFFSET is 1), plus the length '''

    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        return bytes([0xAA, len(info) + 1, command]) + info

    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
        if len(raw_frame) < 3:
            return None
        return SimpleNamespace(command=raw_frame[2], payload=raw_frame[3:])


def make(cls: type[serial_length_transport] = Echo, **values: Any) -> Any:
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "baudrate": 9600, **values}
    transport: serial_length_transport = cls(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def test_the_family_is_an_abstract_base_for_the_web_ui(port: FakePort) -> None:
    assert serial_length_transport.transport_type == "base class"
    assert issubclass(serial_length_transport, serial_base)


def test_a_protocol_must_say_where_its_frames_start(port: FakePort) -> None:
    class NoStart(serial_length_transport):
        transport_type: str = "scraper"

    with pytest.raises(NotImplementedError, match="START"):
        make(NoStart)


def test_the_length_field_must_be_one_or_two_bytes(port: FakePort) -> None:
    class ThreeByteLength(Echo):
        LENGTH_SIZE: int = 3

    with pytest.raises(ValueError, match="LENGTH_SIZE"):
        make(ThreeByteLength)


def test_the_port_is_opened_on_connect_not_at_construction(port: FakePort) -> None:
    transport: Any = make()
    assert transport._client is None
    assert transport.connect() is True
    assert isinstance(transport._client, serial_length_client)
    assert port.timeout == serial_length_client.READ_SLICE


def test_the_client_is_told_how_to_find_frames(port: FakePort) -> None:
    transport: Any = make()
    transport.connect()
    client: serial_length_client = transport._client
    assert (client.start, client.length_offset, client.length_size, client.length_adjust, client.max_frame) == (b"\xaa", 1, 1, 1, 512)


def test_the_timeout_setting_reaches_the_client_and_the_scheduler(port: FakePort) -> None:
    transport: Any = make(timeout="2.5")
    transport.connect()
    assert transport._client.timeout == 2.5
    assert transport._client_timeout() == 2.5


def test_a_missing_port_is_retried_not_fatal(port: FakePort, monkeypatch: pytest.MonkeyPatch) -> None:
    unplug(monkeypatch)
    transport: Any = make()
    assert transport.connect() is False


def test_sections_on_one_port_share_a_client_when_they_frame_the_same_way(port: FakePort) -> None:
    first: Any = make()
    second: Any = make()
    first.connect()
    second.connect()
    assert first._client is second._client


def test_a_section_that_frames_differently_cannot_share_the_port(port: FakePort) -> None:
    class Other(Echo):
        LENGTH_ADJUST: int = 2

    make().connect()
    other: Any = make(Other)
    assert other.connect() is False
    other._log.warning.assert_called()


def test_a_reply_is_decoded_and_its_payload_returned() -> None:
    transport: Any = make_unconnected()
    transport._exchange = MagicMock(return_value=b"\xaa\x03\x07\x01\x02")
    assert transport._read_command(7) == b"\x01\x02"


def test_no_reply_or_an_invalid_one_gives_nothing() -> None:
    transport: Any = make_unconnected()
    transport._exchange = MagicMock(return_value=None)
    assert transport._read_command(7) is None
    transport._exchange = MagicMock(return_value=b"\xaa")  # decode_frame rejects it
    assert transport._read_command(7) is None


def test_a_reply_to_a_different_command_is_dropped() -> None:
    """A stale or foreign frame must not be taken as the answer to this request."""
    transport: Any = make_unconnected()
    transport._exchange = MagicMock(return_value=b"\xaa\x03\x09\x01\x02")  # a reply to command 9
    assert transport._read_command(7) is None
    transport._log.warning.assert_called()


def test_a_protocol_that_does_not_echo_the_command_is_not_checked() -> None:
    class Silent(Echo):
        def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
            return SimpleNamespace(payload=raw_frame[3:])

    transport: Any = make_unconnected(Silent)
    transport._exchange = MagicMock(return_value=b"\xaa\x03\x09\x01\x02")
    assert transport._read_command(7) == b"\x01\x02"


def test_a_request_goes_out_and_the_reply_comes_back_through_the_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: build_frame -> serial port -> client framing -> decode_frame."""
    def responder(request: bytes) -> bytes | None:
        return bytes([0xAA, 4, request[2], 0x11, 0x22, 0x33])  # echo the command with 3 bytes of data

    install_serial_layer(monkeypatch, FakePort(responder=responder))
    transport: Any = make()
    assert transport._read_command(5) == b"\x11\x22\x33"


def test_short_payload_pruning_is_on_for_this_family() -> None:
    assert serial_length_transport.PRUNE_SHORT_PAYLOADS is True
    assert serial_length_transport.OPEN_ON_INIT is False


def make_unconnected(cls: type[serial_length_transport] = Echo) -> Any:
    """A transport that never touches a port, for tests that replace _exchange."""
    transport: Any = cls.__new__(cls)
    transport._log = MagicMock()
    return transport
