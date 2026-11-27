# Description: A fake Bluetooth device and helpers, so the serial_bluetooth transports can be tested through their real client (no radio)
# File: bluetooth_support.py
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
"""A fake BLE device for the serial_bluetooth tests: the real client, loop thread and frame reassembly run against it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from classes.transports import serial_bluetooth as serial_bluetooth_module
from classes.transports.serial_bluetooth import (
    serial_bluetooth,
    serial_bluetooth_client,
)

ADDRESS: str = "AA:BB:CC:DD:EE:FF"


class StubLink:
    """Stands in for BleakClient once connected."""

    is_connected: bool = True


class FakeDevice:
    """
    Answers every write through `responder`, and delivers the reply the way a radio does: in slices of `slice_size`
    bytes, each as a separate notification. `pushed` is sent unasked after every write, ahead of the reply.
    """

    def __init__(
        self,
        responder: Callable[[bytes], bytes | None] | None = None,
        slice_size: int = 20,
        pushed: bytes = b"",
        found: bool = True,
    ) -> None:
        self.responder: Callable[[bytes], bytes | None] | None = responder
        self.slice_size: int = slice_size
        self.pushed: bytes = pushed
        self.found: bool = found
        self.written: list[bytes] = []
        self.connects: int = 0
        self.disconnects: int = 0

    def notify(self, client: serial_bluetooth_client, data: bytes) -> None:
        for start in range(0, len(data), self.slice_size):
            client._on_notification(None, bytearray(data[start:start + self.slice_size]))  # type: ignore[arg-type]

    def drop(self, client: serial_bluetooth_client) -> None:
        """The device goes away."""
        client._ble = None
        client._on_disconnect(None)  # type: ignore[arg-type]


def install_bluetooth_layer(monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None) -> FakeDevice:
    """Isolates the shared-client registries and replaces the three bleak calls of the client with the fake device."""
    fake: FakeDevice = device if device is not None else FakeDevice()
    monkeypatch.setattr(serial_bluetooth, "clients", {})
    monkeypatch.setattr(serial_bluetooth, "_port_locks", {})

    async def connect(self: serial_bluetooth_client) -> None:
        fake.connects += 1
        if not fake.found:
            msg: str = f"Bluetooth device {self.address} was not found"
            raise OSError(msg)
        with self._condition:
            self._buffer.clear()
            self._request_pending = False
            self._lost = False
        self._ble = StubLink()  # type: ignore[assignment]

    async def disconnect(self: serial_bluetooth_client) -> None:
        fake.disconnects += 1
        self._ble = None

    async def write(self: serial_bluetooth_client, data: bytes) -> None:
        fake.written.append(bytes(data))
        if fake.pushed:
            fake.notify(self, fake.pushed)
        reply: bytes | None = fake.responder(bytes(data)) if fake.responder is not None else None
        if reply:
            fake.notify(self, reply)

    monkeypatch.setattr(serial_bluetooth_client, "_connect", connect)
    monkeypatch.setattr(serial_bluetooth_client, "_disconnect", disconnect)
    monkeypatch.setattr(serial_bluetooth_client, "_write", write)
    return fake


def make_transport(cls: type[Any], monkeypatch: pytest.MonkeyPatch, device: FakeDevice | None = None, **values: Any) -> Any:
    """A transport of `cls` on a fake device; settings override the defaults."""
    from unittest.mock import MagicMock

    from conftest import DummySettings

    install_bluetooth_layer(monkeypatch, device)
    merged: dict[str, Any] = {"bluetooth_address": ADDRESS, "timeout": "0.3", **values}
    transport: Any = cls(DummySettings(**merged))
    transport._log = MagicMock()
    return transport


__all__: list[str] = ["ADDRESS", "FakeDevice", "StubLink", "install_bluetooth_layer", "make_transport", "serial_bluetooth_module"]
