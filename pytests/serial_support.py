# Description: Shared test support for the serial transports: a scriptable fake serial port and registry isolation.
# File: serial_support.py
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

"""A fake serial port and helpers, so the serial transports can be tested through their real client (no hardware)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import serial

from classes.transports import serial_base as serial_base_module
from classes.transports.serial_base import serial_base


class FakePort:
    """
    Stands in for serial.Serial. Bytes arrive from `chunks`, and a `responder` can queue a reply
    whenever the transport writes a request, so a device can be simulated end to end.
    """
    def __init__(
        self,
        responder: Callable[[bytes], bytes | None] | None = None,
        chunks: list[bytes] | None = None,
        chunk_size: int | None = None,
        arrives_after_reset: bytes | None = None,
    ) -> None:
        self.responder: Callable[[bytes], bytes | None] | None = responder
        self.chunks: list[bytes] = list(chunks or [])
        self.chunk_size: int | None = chunk_size
        self.arrives_after_reset: bytes | None = arrives_after_reset
        self.written: list[bytes] = []
        self.is_open: bool = True
        self.close_calls: int = 0
        self.input_resets: int = 0
        self.timeout: float | None = None

    @property
    def in_waiting(self) -> int:
        return len(self.chunks[0]) if self.chunks else 0

    def queue(self, data: bytes) -> None:
        """Make bytes available to read, split into chunk_size pieces if set."""
        if self.chunk_size:
            self.chunks += [data[i:i + self.chunk_size] for i in range(0, len(data), self.chunk_size)]
        else:
            self.chunks.append(data)

    def read(self, size: int = 1) -> bytes:
        if not self.chunks:
            return b""
        chunk: bytes = self.chunks[0]
        data: bytes = chunk[:size]
        rest: bytes = chunk[size:]
        if rest:
            self.chunks[0] = rest
        else:
            self.chunks.pop(0)
        return data

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        if self.responder is not None:
            reply: bytes | None = self.responder(bytes(data))
            if reply:
                self.queue(reply)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.input_resets += 1
        self.chunks.clear()
        if self.arrives_after_reset is not None:
            self.queue(self.arrives_after_reset)

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False


def same_port(port: str) -> str:
    """find_usb_serial_port stand-in: every port path resolves to itself."""
    return port


def no_port(port: str) -> str | None:
    """find_usb_serial_port stand-in for a port that is not plugged in."""
    return None


def port_info(port: str) -> str:
    """get_usb_serial_port_info stand-in."""
    return "mock"


def install_serial_layer(monkeypatch: pytest.MonkeyPatch, port: FakePort | None = None, **extra: Any) -> FakePort:
    """
    Isolates the shared-client registries and fakes port resolution. If a FakePort is given, every
    serial.Serial() opens it, so the real client classes run against it.
    """
    monkeypatch.setattr(serial_base, "clients", {})
    monkeypatch.setattr(serial_base, "_port_locks", {})
    monkeypatch.setattr(serial_base_module, "find_usb_serial_port", same_port)
    monkeypatch.setattr(serial_base_module, "get_usb_serial_port_info", port_info)

    opened: FakePort = port if port is not None else FakePort(**extra)

    def open_port(*args: Any, **kwargs: Any) -> FakePort:
        opened.timeout = kwargs.get("timeout")
        return opened

    monkeypatch.setattr(serial, "Serial", open_port)
    return opened


def unplug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the configured port stop resolving, as if the adapter were unplugged."""
    monkeypatch.setattr(serial_base_module, "find_usb_serial_port", no_port)
