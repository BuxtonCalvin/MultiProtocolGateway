# Description: Unit tests for the serial_frame_client wire layer in serial_base (per-instance state, single reader thread, close).
# File: test_serial_frame_client.py
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

"""Unit tests for classes.transports.serial_base.serial_frame_client with pyserial mocked out (no hardware)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock, patch

import pytest

from classes.transports.serial_base import serial_frame_client


@pytest.fixture
def serial_port() -> Iterator[MagicMock]:
    """A pyserial port that is open and never has data waiting."""
    with patch("classes.transports.serial_base.serial.Serial") as port_cls:
        port: MagicMock = MagicMock()
        port.is_open = True
        port.read.return_value = b""
        port_cls.return_value = port
        yield port


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline: float = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def reader_of(client: serial_frame_client) -> threading.Thread:
    """The client's async reader thread, which must exist at this point of the test."""
    thread: threading.Thread | None = client.thread
    assert thread is not None, "the client has no reader thread"
    return thread


def test_clients_do_not_share_frame_lists_or_callback_locks(serial_port: MagicMock) -> None:
    """These were class-level, so every client on every port shared one list and one lock."""
    first: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    second: serial_frame_client = serial_frame_client("/dev/ttyUSB1", 9600, b"<", b">")
    assert first.pending_frames is not second.pending_frames
    assert first.callback_lock is not second.callback_lock

    first.pending_frames.append(b"x")
    assert second.pending_frames == []


def test_connect_in_async_mode_starts_exactly_one_reader_thread(serial_port: MagicMock) -> None:
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    client.asynchronous = True
    try:
        assert client.connect() is True
        first_thread: threading.Thread = reader_of(client)
        assert client.connect() is True  # a second section joining the same client
        assert reader_of(client) is first_thread, "a second reader would split the incoming frames between two threads"
    finally:
        client.close()


def test_connect_without_async_starts_no_thread(serial_port: MagicMock) -> None:
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    assert client.connect() is True
    assert client.thread is None


def test_close_stops_the_reader_thread_and_releases_the_port(serial_port: MagicMock) -> None:
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    client.asynchronous = True
    client.connect()
    thread: threading.Thread = reader_of(client)
    assert thread.is_alive()

    client.close()

    assert wait_until(lambda: not thread.is_alive())
    assert client.running is False
    serial_port.close.assert_called_once()


def test_close_is_idempotent_and_skips_a_port_that_is_already_closed(serial_port: MagicMock) -> None:
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    client.close()
    serial_port.close.assert_called_once()

    serial_port.is_open = False
    client.close()
    serial_port.close.assert_called_once()


def test_close_without_an_async_thread_just_closes_the_port(serial_port: MagicMock) -> None:
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    client.close()
    serial_port.close.assert_called_once()


def test_async_reader_delivers_frames_to_the_callback(serial_port: MagicMock) -> None:
    received: list[bytes] = []
    serial_port.read.side_effect = [b"junk<", b"AB", b">", *([b""] * 1000)]
    client: serial_frame_client = serial_frame_client("/dev/ttyUSB0", 9600, b"<", b">")
    client.asynchronous = True
    client.on_message = received.append
    try:
        client.connect()
        assert wait_until(lambda: received == [b"AB"])
    finally:
        client.close()
