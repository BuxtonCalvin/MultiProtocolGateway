# Description: Unit tests for the serial_base transport (shared client, bus locking, scheduling and command polling).
# File: test_serial_base.py
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
# These tests deliberately exercise protected members (_exchange, _log, _parse_version, ...).

"""Unit tests for classes.transports.serial_base, using a minimal fake serial protocol (no hardware)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import DummySettings

from classes.transports.serial_base import serial_base

BASE_MODULE = "classes.transports.serial_base"


class FakeClient:
    """Stands in for serial_frame_client: echoes the request frame back as the response."""

    timeout: float = 5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.connected_calls = 0
        self.last_written: bytes = b""
        self.on_write: Any = None

    def connect(self) -> bool:
        self.connected_calls += 1
        return True

    def write(self, data: bytes) -> None:
        self.last_written = data
        if self.on_write:
            self.on_write()  # runs after the request is stored, i.e. between a write and its read

    def read(self) -> bytes:
        return self.last_written


def _same_port(port: str) -> str:
    """find_usb_serial_port stand-in: every port path resolves to itself."""
    return port


def _new_client(*args: Any, **kwargs: Any) -> FakeClient:
    return FakeClient(*args, **kwargs)


def _process_registery(raw: dict[int, bytes], registry_map: object = None) -> dict[str, str]:
    """protocol_settings.process_registery stand-in: one variable per command, value is the payload in hex."""
    return {f"v{command}": payload.hex() for command, payload in raw.items()}


class FakeSerial(serial_base):
    """Smallest possible concrete serial protocol: frame = 2 hex chars address + 2 hex chars command."""

    transport_type = "scraper"
    SOI = b"<"
    EOI = b">"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.responses: dict[int, bytes | None] = {}
        self.handshake_ok: bool = True

    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        return f"{self.address:02X}{command:02X}".encode()

    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
        return SimpleNamespace(raw=raw_frame)

    def _read_command(self, command: int) -> bytes | None:
        return self.responses.get(command)

    def _handshake(self) -> bool:
        return self.handshake_ok


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(serial_base, "clients", {})
    monkeypatch.setattr(serial_base, "_port_locks", {})
    yield


def make(cls: type[serial_base] = FakeSerial, **values: Any) -> Any:
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "baudrate": 9600, "address": 2, **values}
    settings = DummySettings(**merged)
    with (
        patch(f"{BASE_MODULE}.find_usb_serial_port", side_effect=_same_port),
        patch(f"{BASE_MODULE}.get_usb_serial_port_info", return_value="mock"),
        patch(f"{BASE_MODULE}.serial_frame_client", side_effect=_new_client),
    ):
        transport = cls(settings)  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def fake_protocol(registers: list[int]) -> MagicMock:
    protocol = MagicMock()
    protocol.get_registry_map.return_value = [SimpleNamespace(register=r, variable_name=f"v{r}") for r in registers]
    protocol.process_registery.side_effect = _process_registery
    return protocol


# --------------------------------------------------------------------------- #
# construction                                                                #
# --------------------------------------------------------------------------- #

def test_base_class_is_marked_abstract_for_the_web_ui() -> None:
    assert serial_base.transport_type == "base class"


def test_subclass_without_frame_markers_is_rejected() -> None:
    class NoMarkers(serial_base):
        transport_type = "scraper"

    with pytest.raises(NotImplementedError, match="SOI and EOI"):
        make(NoMarkers)


def test_client_is_created_with_markers_and_default_serial_parameters() -> None:
    transport = make()
    assert transport.client.args == ("/dev/ttyUSB0", 9600, b"<", b">")
    assert transport.client.kwargs["bytesize"] == 8
    assert transport.client.kwargs["stopbits"] == 1
    assert transport.client.kwargs["exclusive"] is True


def test_subclass_can_override_serial_parameters() -> None:
    class Even(FakeSerial):
        def _serial_parameters(self) -> dict[str, Any]:
            return {**super()._serial_parameters(), "parity": "E"}

    assert make(Even).client.kwargs["parity"] == "E"


def test_documented_setting_names_are_accepted() -> None:
    """unit_id/baud are the names used in the device docs; address/baudrate are the canonical ones."""
    settings = DummySettings(port="/dev/ttyUSB0", unit_id=5, baud=115200)
    with (
        patch(f"{BASE_MODULE}.find_usb_serial_port", side_effect=_same_port),
        patch(f"{BASE_MODULE}.get_usb_serial_port_info", return_value="mock"),
        patch(f"{BASE_MODULE}.serial_frame_client", side_effect=_new_client),
    ):
        transport = FakeSerial(settings)  # type: ignore[arg-type]
    assert transport.address == 5
    assert transport.addresses == [5]
    assert transport.baudrate == 115200


@pytest.mark.parametrize("address", [-1, 256])
def test_out_of_range_address_is_rejected(address: int) -> None:
    with pytest.raises(ValueError, match="0-255"):
        make(address=address)


def test_subclass_can_narrow_the_address_range() -> None:
    class OneToTwoForty(FakeSerial):
        MIN_ADDRESS = 1
        MAX_ADDRESS = 247

    assert make(OneToTwoForty, address=247).address == 247
    for bad in (0, 248):
        with pytest.raises(ValueError, match="1-247"):
            make(OneToTwoForty, address=bad)


def test_unresolvable_port_is_rejected() -> None:
    settings = DummySettings(port="/dev/nope", address=2)
    with patch(f"{BASE_MODULE}.find_usb_serial_port", return_value=None), pytest.raises(ValueError, match="not valid"):
        FakeSerial(settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# shared bus                                                                  #
# --------------------------------------------------------------------------- #

def test_sections_on_same_port_share_one_client_and_lock() -> None:
    first, second = make(address=2), make(address=3)
    assert first.client is second.client
    assert first._get_port_lock() is second._get_port_lock()
    assert len(serial_base.clients) == 1


def test_registry_is_shared_across_subclasses() -> None:
    class Other(FakeSerial):
        pass

    assert make(FakeSerial).client is make(Other).client


def test_different_baudrate_gets_its_own_client_and_lock() -> None:
    first, second = make(baudrate=9600), make(baudrate=115200)
    assert first.client is not second.client
    assert first._get_port_lock() is not second._get_port_lock()


def test_exchanges_from_threads_on_a_shared_port_never_interleave() -> None:
    """Without the port lock a thread could read another thread's response."""
    first, second = make(address=2), make(address=3)
    client: FakeClient = first.client
    client.on_write = lambda: time.sleep(0.0005)  # widen the race window between write and read

    mismatches: list[tuple[bytes, bytes | None]] = []

    def hammer(transport: FakeSerial) -> None:
        for _ in range(200):
            sent = transport.build_frame(1)
            got = transport._exchange(1)
            if got != sent:
                mismatches.append((sent, got))

    threads = [threading.Thread(target=hammer, args=(t,)) for t in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mismatches == []


def test_exchange_holds_bus_lock_for_the_round_trip_and_releases_it() -> None:
    transport = make()
    bus_lock = threading.Lock()
    transport.bus_lock = bus_lock
    held_during_write: list[bool] = []
    transport.client.on_write = lambda: held_during_write.append(bus_lock.locked())

    assert transport._exchange(1) == b"0201"
    assert held_during_write == [True]
    assert not bus_lock.locked()


def test_exchange_releases_bus_lock_when_the_client_raises() -> None:
    transport = make()
    bus_lock = threading.Lock()
    transport.bus_lock = bus_lock
    transport.client.on_write = MagicMock(side_effect=OSError("port gone"))

    with pytest.raises(OSError, match="port gone"):
        transport._exchange(1)
    assert not bus_lock.locked()


def test_exchange_without_a_bus_lock_still_works() -> None:
    transport = make()
    assert transport.bus_lock is None
    assert transport._exchange(0x0A) == b"020A"


def test_exchange_joins_list_responses_and_maps_empty_to_none() -> None:
    transport = make()
    transport.client.read = lambda: [b"02", b"01"]  # type: ignore[method-assign]
    assert transport._exchange(1) == b"0201"
    transport.client.read = lambda: b""  # type: ignore[method-assign]
    assert transport._exchange(1) is None


# --------------------------------------------------------------------------- #
# scheduling                                                                  #
# --------------------------------------------------------------------------- #

def test_scrape_target_is_never_empty_so_the_gateway_schedules_the_transport() -> None:
    assert make().scrape_target != ""


def test_scrape_target_identifies_port_protocol_and_address() -> None:
    a2 = make(address=2, protocol_version="proto")
    a3 = make(address=3, protocol_version="proto")
    again = make(address=2, protocol_version="proto")
    other_proto = make(address=2, protocol_version="other")
    assert a2.scrape_target == again.scrape_target
    assert len({a2.scrape_target, a3.scrape_target, other_proto.scrape_target}) == 3
    assert a2.scrape_target == "/dev/ttyUSB0:proto:2"


def test_gateway_builds_one_scrape_group_per_device() -> None:
    """Uses the real Protocol_Gateway grouping: two addresses on one port -> two groups; duplicate sections of one device -> one."""
    from protocol_gateway import Protocol_Gateway

    battery1 = make(address=2, protocol_version="proto")
    battery1_again = make(address=2, protocol_version="proto")
    battery2 = make(address=3, protocol_version="proto")
    for transport in (battery1, battery1_again, battery2):
        transport.read_interval = 10

    gateway = Protocol_Gateway.__new__(Protocol_Gateway)
    gateway._Protocol_Gateway__transports = [battery1, battery1_again, battery2]  # type: ignore[attr-defined]
    groups = gateway._build_scrape_groups()

    assert len(groups) == 2
    assert sorted(len(g.members) for g in groups) == [1, 2]


def test_interleaved_timeout_scales_with_command_count() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol(list(range(1, 4)))
    assert transport.interleaved_cycle_timeout() == 60.0  # 3 commands * 2 * 5s = 30s: default wins

    transport.protocolSettings = fake_protocol(list(range(1, 11)))
    assert transport.interleaved_cycle_timeout() == 100.0  # 10 commands * 2 * 5s


# --------------------------------------------------------------------------- #
# connection                                                                  #
# --------------------------------------------------------------------------- #

def test_connect_opens_client_and_marks_connected_when_handshake_succeeds() -> None:
    transport = make()
    assert transport.connect() is True
    assert transport.client.connected_calls == 1
    assert transport.connected is True


def test_connect_stays_disconnected_when_handshake_fails() -> None:
    transport = make()
    transport.handshake_ok = False
    assert transport.connect() is False
    assert transport.connected is False


def test_default_handshake_trusts_the_port() -> None:
    class Plain(serial_base):
        transport_type = "scraper"
        SOI = b"<"
        EOI = b">"

    assert make(Plain)._handshake() is True


# --------------------------------------------------------------------------- #
# polling                                                                     #
# --------------------------------------------------------------------------- #

def test_poll_commands_are_the_distinct_registers_in_order() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([5, 3, 5, 9, 3])
    assert transport._poll_commands() == [5, 3, 9]


def test_poll_commands_without_protocol_is_empty() -> None:
    transport = make()
    transport.protocolSettings = None
    assert transport._poll_commands() == []
    assert transport.read_data() == {}
    assert list(transport.read_data_iter()) == []


def test_read_data_tries_each_command_once_per_cycle() -> None:
    """Rows sharing a command and no reply must cost one attempt, not one timeout per row."""
    transport = make()
    transport.protocolSettings = fake_protocol([1, 1, 2])
    transport._read_command = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert transport.read_data() == {}
    assert [c.args[0] for c in transport._read_command.call_args_list] == [1, 2]


def test_read_data_decodes_all_payloads_in_one_pass() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2, 3])
    transport.responses = {1: b"\x01", 3: b"\x03"}  # command 2 does not answer
    assert transport.read_data() == {"v1": "01", "v3": "03"}
    transport.protocolSettings.process_registery.assert_called_once()


def test_read_data_reports_each_command_in_cycle_bookkeeping() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2, 3])
    transport.responses = {1: b"\x01", 3: b"\x03"}
    transport._start_cycle_tracking()
    transport.read_data()
    result = transport.get_cycle_result()
    assert (result.expected_units, result.completed_units, result.skipped_units) == (3, 2, 1)
    assert result.is_complete is False


def test_fully_answered_cycle_is_complete_for_bridges() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2])
    transport.responses = {1: b"\x01", 2: b"\x02"}
    transport._start_cycle_tracking()
    data = transport.read_data()
    transport.finish_cycle_tracking(data)
    assert transport.cycle_is_complete_for_bridge() is True


def test_partially_answered_cycle_is_not_complete_for_bridges() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2])
    transport.responses = {1: b"\x01"}
    transport._start_cycle_tracking()
    data = transport.read_data()
    transport.finish_cycle_tracking(data)
    assert transport.cycle_is_complete_for_bridge() is False


def test_read_data_iter_yields_once_per_command_and_decodes_at_the_end() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2, 3])
    transport.responses = {1: b"\x01", 2: b"\x02", 3: b"\x03"}

    steps = 0
    for _ in transport.read_data_iter():
        steps += 1
        assert transport.get_partial_data() == {}, "payloads are decoded together once the last command is in"
    assert steps == 3
    assert transport.get_partial_data() == {"v1": "01", "v2": "02", "v3": "03"}


def test_read_data_iter_owns_cycle_tracking_when_standalone() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2])
    transport.responses = {1: b"\x01", 2: b"\x02"}
    list(transport.read_data_iter())
    assert transport.cycle_is_complete_for_bridge() is True
    assert getattr(transport, "_cycle_active", False) is False


def test_read_data_iter_marks_a_cycle_with_a_silent_command_incomplete() -> None:
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2])
    transport.responses = {1: b"\x01"}
    list(transport.read_data_iter())
    assert transport.get_partial_data() == {"v1": "01"}
    assert transport.cycle_is_complete_for_bridge() is False


def test_read_data_iter_leaves_tracking_to_the_group_iterator() -> None:
    """read_group_data_iter starts the cycle and falls back to each member's read_data_iter."""
    transport = make()
    transport.protocolSettings = fake_protocol([1])
    transport.responses = {1: b"\x01"}
    transport._start_cycle_tracking()
    list(transport.read_data_iter())
    assert transport._cycle_active is True, "a non-owner must not finish the cycle"
    assert transport.get_partial_data() == {"v1": "01"}


def test_read_data_iter_releases_the_bus_between_commands() -> None:
    """bus_lock is taken per exchange, never held across a yield, so other transports can take a turn."""
    transport = make()
    transport.protocolSettings = fake_protocol([1, 2, 3])
    transport._read_command = lambda command: transport._exchange(command)  # type: ignore[method-assign]
    bus_lock = threading.Lock()
    transport.bus_lock = bus_lock

    for _ in transport.read_data_iter():
        assert not bus_lock.locked()
