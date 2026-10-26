# Description: Unit tests for the serial_frame_transport (listen-only SOI/EOI scraper built on serial_base).
# File: test_serial_frame_transport.py
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
# These tests deliberately exercise protected members (_client, _decode_frame, _on_async_frame, ...).
"""Unit tests for classes.transports.serial_frame_transport, with the serial port faked out (no hardware)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import serial
from conftest import DummySettings

from classes.transports import serial_base as serial_base_module
from classes.transports import serial_frame_transport as frame_module
from classes.transports.serial_base import SerialClient, serial_base
from classes.transports.serial_frame_transport import serial_frame_transport
from protocol_gateway import Protocol_Gateway, ScrapeGroup


class FakeClient:
    """Stands in for serial_frame_client. Class-level switches are reset by the `env` fixture."""

    timeout: float = 5
    created: list[FakeClient] = []
    fail_with: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        self.args: tuple[Any, ...] = args
        self.kwargs: dict[str, Any] = kwargs
        self.soi: bytes = args[2]
        self.eoi: bytes = args[3]
        self.asynchronous = False
        self.on_message: Any = None
        self.connected_calls = 0
        self.close_calls = 0
        self.read_calls: list[tuple[bool, int]] = []
        self.written: list[bytes] = []
        self.next_frame: bytes | list[bytes] | None = None
        self.read_hook: Any = None
        self.read_error: Exception | None = None
        FakeClient.created.append(self)

    def connect(self) -> bool:
        self.connected_calls += 1
        return True

    def close(self) -> None:
        self.close_calls += 1

    def read(self, reset_buffer: bool = True, frames: int = 1) -> list[bytes] | bytes | None:
        self.read_calls.append((reset_buffer, frames))
        if self.read_hook:
            self.read_hook()
        if self.read_error is not None:
            raise self.read_error
        return self.next_frame

    def write(self, data: bytes) -> None:
        self.written.append(data)


def _port_info(port: str) -> str:
    """get_usb_serial_port_info stand-in."""
    return "mock"


def log_of(transport: serial_frame_transport) -> MagicMock:
    """The transport's logger, which make() replaced with a MagicMock."""
    return cast(MagicMock, transport._log)


@dataclass
class Env:
    missing_ports: set[str] = field(default_factory=set[str])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    """Fake serial layer for the whole test (the port opens lazily, so patches must outlive construction)."""
    state = Env()
    FakeClient.created = []
    FakeClient.fail_with = None

    def find_port(port: str = "", *args: Any, **kwargs: Any) -> str | None:
        return None if port in state.missing_ports else port

    monkeypatch.setattr(serial_base, "clients", {})
    monkeypatch.setattr(serial_base, "_port_locks", {})
    monkeypatch.setattr(serial_base_module, "find_usb_serial_port", find_port)
    monkeypatch.setattr(serial_base_module, "get_usb_serial_port_info", _port_info)
    monkeypatch.setattr(frame_module, "serial_frame_client", FakeClient)
    yield state


def make(**values: Any) -> serial_frame_transport:
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "baud": 9600, **values}
    transport = serial_frame_transport(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def _process_registery(raw: dict[int, bytes], registry_map: object = None) -> dict[str, str]:
    """protocol_settings.process_registery stand-in: one variable per command, value is the payload in hex."""
    return {f"v{command}": payload.hex() for command, payload in raw.items()}


def fake_protocol(rows: int) -> MagicMock:
    protocol = MagicMock()
    protocol.get_registry_map.return_value = [MagicMock(register=i, variable_name=f"v{i}") for i in range(rows)]
    return protocol


# --------------------------------------------------------------------------- #
# structure and settings                                                      #
# --------------------------------------------------------------------------- #

def test_is_a_serial_base_scraper_that_opens_lazily(env: Env) -> None:
    assert issubclass(serial_frame_transport, serial_base)
    assert "transport_type" in vars(serial_frame_transport), "the web UI scanner reads it with ast; it cannot be inherited"
    assert serial_frame_transport.transport_type == "scraper"
    assert serial_frame_transport.OPEN_ON_INIT is False


def test_default_and_configured_frame_markers(env: Env) -> None:
    default: serial_frame_transport = make()
    assert (default._soi, default._eoi) == (b"~", b"\r")
    hexed: serial_frame_transport = make(soi="0x02", eoi="03")
    assert (hexed._soi, hexed._eoi) == (b"\x02", b"\x03")
    literal: serial_frame_transport = make(soi="<<", eoi=">>")
    assert (literal._soi, literal._eoi) == (b"<<", b">>")


def test_empty_frame_marker_is_rejected(env: Env) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        make(soi="")


@pytest.mark.parametrize("values", [{"baud": 19200}, {"baudrate": 19200}])
def test_both_baud_setting_names_are_accepted(env: Env, values: dict[str, Any]) -> None:
    transport = serial_frame_transport(DummySettings(port="/dev/ttyUSB0", **values))  # type: ignore[arg-type]
    assert transport.baudrate == 19200
    assert transport._baud == 19200


def test_timeout_async_and_port_attributes(env: Env) -> None:
    transport: serial_frame_transport = make(timeout="2.5", async_mode="true")
    assert transport._timeout == 2.5
    assert transport._async_mode is True
    assert transport._port == transport.port == "/dev/ttyUSB0"


# --------------------------------------------------------------------------- #
# lazy open                                                                   #
# --------------------------------------------------------------------------- #

def test_construction_does_not_open_the_port(env: Env) -> None:
    transport: serial_frame_transport = make()
    assert FakeClient.created == []
    assert transport._client is None
    assert transport.connected is False


def test_missing_port_does_not_stop_start_up_and_is_retried(env: Env) -> None:
    env.missing_ports.add("/dev/ttyUSB0")
    transport: serial_frame_transport = make()  # eager transports raise here; this one must not

    assert transport.connect() is False
    assert transport.connected is False
    assert FakeClient.created == []

    env.missing_ports.clear()
    assert transport.connect() is True
    assert transport.connected is True
    assert len(FakeClient.created) == 1


def test_open_failure_is_logged_and_retried_not_raised(env: Env) -> None:
    transport: serial_frame_transport = make()
    FakeClient.fail_with = OSError("device busy")
    assert transport.connect() is False
    log_of(transport).warning.assert_called()

    FakeClient.fail_with = None
    assert transport.connect() is True


def test_connect_applies_the_timeout_to_the_client(env: Env) -> None:
    transport: serial_frame_transport = make(timeout="2.5")
    assert transport.connect() is True
    client: FakeClient = FakeClient.created[0]
    assert client.timeout == 2.5
    assert client.asynchronous is False
    assert client.on_message is None
    assert client.args[:4] == ("/dev/ttyUSB0", 9600, b"~", b"\r")
    assert client.kwargs == {"bytesize": 8, "parity": serial.PARITY_NONE, "stopbits": 1}, "no exclusive lock: nothing else is stopped from opening the port, as before"


def test_async_mode_wires_the_frame_callback(env: Env) -> None:
    transport: serial_frame_transport = make(async_mode="true")
    assert transport.connect() is True
    client: FakeClient = FakeClient.created[0]
    assert client.asynchronous is True
    assert client.on_message == transport._on_async_frame


def test_an_async_listener_cannot_share_its_port_but_sync_sections_can(env: Env) -> None:
    first: serial_frame_transport = make(async_mode="true")
    second: serial_frame_transport = make(async_mode="true")
    assert first.connect() is True
    assert second.connect() is False, "its frame callback would replace the first section's"
    assert FakeClient.created[0].on_message == first._on_async_frame

    env.missing_ports.clear()
    sync_a, sync_b = make(port="/dev/ttyUSB1"), make(port="/dev/ttyUSB1")
    assert sync_a.connect() is True
    assert sync_b.connect() is True
    assert sync_a._client is sync_b._client


def test_sections_with_different_markers_cannot_share_a_port(env: Env) -> None:
    assert make(soi="7E").connect() is True
    clash: serial_frame_transport = make(soi="02")
    assert clash.connect() is False
    log_of(clash).warning.assert_called()


# --------------------------------------------------------------------------- #
# reading and writing                                                         #
# --------------------------------------------------------------------------- #

def test_read_data_listens_only_and_decodes_one_frame(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.protocolSettings = None
    transport.connect()
    FakeClient.created[0].next_frame = b"\x01\x02"

    assert transport.read_data() == {"raw_frame": "0102"}
    client: FakeClient = FakeClient.created[0]
    assert client.read_calls == [(True, 1)]
    assert client.written == [], "a listen-only transport never transmits"


def test_read_data_connects_on_first_use(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.protocolSettings = None
    assert FakeClient.created == []
    # nothing has connected yet; the first read opens the port itself
    assert transport.read_data() == {}
    assert len(FakeClient.created) == 1


def test_read_data_with_no_frame_or_a_frame_list_returns_empty(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.connect()
    client: FakeClient = FakeClient.created[0]
    client.next_frame = None
    assert transport.read_data() == {}
    client.next_frame = [b"a", b"b"]
    assert transport.read_data() == {}


def test_read_error_disconnects_and_the_next_read_reconnects(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.protocolSettings = None
    transport.connect()
    client: FakeClient = FakeClient.created[0]
    client.read_error = OSError("unplugged")

    assert transport.read_data() == {}
    assert transport.connected is False

    client.read_error = None
    client.next_frame = b"\x07"
    assert transport.read_data() == {"raw_frame": "07"}
    assert transport.connected is True


def test_read_holds_the_port_lock(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.protocolSettings = None
    transport.connect()
    client: FakeClient = FakeClient.created[0]
    client.next_frame = b"\x01"
    lock_was_free_for_others: list[bool] = []

    def probe() -> None:
        def try_lock() -> None:
            lock: RLock = transport._get_port_lock()
            acquired: bool = lock.acquire(blocking=False)
            lock_was_free_for_others.append(acquired)
            if acquired:
                lock.release()

        worker = threading.Thread(target=try_lock)
        worker.start()
        worker.join()

    client.read_hook = probe
    transport.read_data()
    assert lock_was_free_for_others == [False]


def test_write_is_skipped_by_default_without_raising(env: Env) -> None:
    """write_mode only exists when write_type is configured; the default config used to raise AttributeError."""
    transport: serial_frame_transport = make()
    assert not hasattr(transport, "write_mode")
    transport.write_data({"x": 1}, transport)
    assert FakeClient.created == [], "a skipped write must not even open the port"


def test_write_sends_the_encoded_frame_when_write_mode_is_enabled(env: Env) -> None:
    transport: serial_frame_transport = make(write_type="write")
    transport._encode_frame = MagicMock(return_value=b"\x10\x20")  # type: ignore[method-assign]
    transport.write_data({"x": 1}, transport)
    assert FakeClient.created[0].written == [b"\x10\x20"]


def test_write_with_nothing_to_encode_sends_nothing(env: Env) -> None:
    transport: serial_frame_transport = make(write_type="write")
    transport.write_data({}, transport)
    assert FakeClient.created[0].written == []


def test_async_frame_callback_decodes_the_frame(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport._decode_frame = MagicMock(return_value={})  # type: ignore[method-assign]
    transport._on_async_frame(b"\x01")
    transport._decode_frame.assert_called_once_with(b"\x01")


# --------------------------------------------------------------------------- #
# scheduling                                                                  #
# --------------------------------------------------------------------------- #

def test_gateway_schedules_the_transport(env: Env) -> None:
    """The empty transport_base scrape_target meant the gateway never polled this transport."""
    from protocol_gateway import Protocol_Gateway

    on_ttyusb0_a, on_ttyusb0_b = make(), make()
    on_ttyusb1: serial_frame_transport = make(port="/dev/ttyUSB1")
    for transport in (on_ttyusb0_a, on_ttyusb0_b, on_ttyusb1):
        transport.read_interval = 10
        assert transport.scrape_target != ""

    gateway: Protocol_Gateway = Protocol_Gateway.__new__(Protocol_Gateway)
    gateway._Protocol_Gateway__transports = [on_ttyusb0_a, on_ttyusb0_b, on_ttyusb1]  # type: ignore[attr-defined]
    groups: list[ScrapeGroup] = gateway._build_scrape_groups()

    assert sorted(len(g.members) for g in groups) == [1, 2], "same port is one device; another port is another"


def test_interleaved_read_is_one_block_that_collects_the_frame(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.protocolSettings = None
    transport.connect()
    FakeClient.created[0].next_frame = b"\x01\x02"

    assert list(transport.read_data_iter()) == [True]
    assert transport.get_partial_data() == {"raw_frame": "0102"}
    assert transport.cycle_is_complete_for_bridge() is True


def test_interleaved_timeout_follows_the_read_timeout_not_the_registry_size(env: Env) -> None:
    """serial_base scales with the command count; a listen-only read is one frame however big the map is."""
    transport: serial_frame_transport = make()
    transport.protocolSettings = fake_protocol(100)
    assert transport.interleaved_cycle_timeout() == 60.0

    slow: serial_frame_transport = make(timeout="40")
    slow.protocolSettings = fake_protocol(100)
    assert slow.interleaved_cycle_timeout() == 80.0


# --------------------------------------------------------------------------- #
# shutdown                                                                    #
# --------------------------------------------------------------------------- #

def test_cleanup_closes_the_port_and_evicts_the_client(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.connect()
    client: FakeClient = FakeClient.created[0]

    transport.cleanup()

    assert client.close_calls == 1
    assert transport._client is None
    assert transport.connected is False
    assert serial_base.clients == {}


def test_a_replacement_transport_after_cleanup_opens_a_fresh_client(env: Env) -> None:
    """A gateway reload: stop() cleans every old transport, then new ones are built for the same port."""
    old: serial_frame_transport = make()
    old.connect()
    old.cleanup()

    new: serial_frame_transport = make()
    assert new.connect() is True
    assert len(FakeClient.created) == 2
    assert new._client is FakeClient.created[1]
    assert new._client is not FakeClient.created[0]


def test_cleanup_before_connect_is_safe(env: Env) -> None:
    transport: serial_frame_transport = make()
    transport.cleanup()
    assert FakeClient.created == []


# --------------------------------------------------------------------------- #
# the listen-only switch: command/response protocols build on this class      #
# --------------------------------------------------------------------------- #

class Asker(serial_frame_transport):
    """A command/response protocol on the SOI/EOI family, the way serial_pylon is."""

    transport_type: str = "scraper"
    LISTEN_ONLY: bool = False
    OPEN_ON_INIT: bool = True
    SOI: bytes = b"<"
    EOI: bytes = b">"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.replies: dict[int, bytes | None] = {}

    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        return f"{command:02X}".encode()

    def _read_command(self, command: int) -> bytes | None:
        return self.replies.get(command)


def make_asker(**values: Any) -> Asker:
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "baud": 9600, **values}
    transport: Asker = Asker(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def test_listen_only_is_the_default_for_the_generic_transport(env: Env) -> None:
    assert serial_frame_transport.LISTEN_ONLY is True
    assert make()._poll_commands() == [serial_frame_transport.LISTEN]


def test_a_command_response_subclass_polls_the_registry_not_the_listen_path(env: Env) -> None:
    transport: Asker = make_asker()
    transport.protocolSettings = fake_protocol(3)
    assert transport._poll_commands() == [0, 1, 2]

    transport.replies = {0: b"\x0a", 2: b"\x0c"}
    transport.protocolSettings.process_registery.side_effect = _process_registery
    assert transport.read_data() == {"v0": "0a", "v2": "0c"}
    assert FakeClient.created[0].read_calls == [], "nothing is listened for: the port is only read through the command loop"


def test_a_command_response_subclass_does_not_use_the_listen_decoder(env: Env) -> None:
    """The listen decoder would turn this frame into {"raw_frame": "01"}; a command/response protocol decodes via its map only."""
    transport: Asker = make_asker()
    transport.protocolSettings = None
    assert transport._process_raw({1: b"\x01"}) == {}
    assert make()._process_raw({serial_frame_transport.LISTEN: b"\x01"}) == {"raw_frame": "01"}


def test_a_subclass_with_fixed_markers_ignores_the_soi_eoi_settings(env: Env) -> None:
    transport: Asker = make_asker(soi="02", eoi="03")
    assert transport._markers == (b"<", b">")


def test_a_command_response_subclass_opens_eagerly_when_it_asks_to(env: Env) -> None:
    transport: Asker = make_asker()
    assert transport._client is not None
    assert len(FakeClient.created) == 1


def test_async_mode_is_refused_for_a_command_response_subclass(env: Env) -> None:
    with pytest.raises(ValueError, match="async_mode cannot be used"):
        make_asker(async_mode="true")


def test_a_foreign_client_is_refused_by_the_frame_transport(env: Env) -> None:
    transport: serial_frame_transport = make()
    with pytest.raises(TypeError, match="needs a serial_frame_client"):
        transport._prepare_client(cast(SerialClient, object()))
