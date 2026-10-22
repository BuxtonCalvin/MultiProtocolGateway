# Description: Unit tests for the serial_pylon scraper transport (version handling, checksum validation, shared RS485 bus).
# File: test_serial_pylon.py
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

"""Unit tests for classes.transports.serial_pylon (no serial hardware required).

Port handling, the shared client, locking and polling are tested in test_serial_base.py;
this module covers the Pylon wire format: version negotiation, framing and checksums.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from conftest import DummySettings

from classes.transports.serial_base import serial_base
from classes.transports.serial_pylon import serial_pylon

BASE_MODULE = "classes.transports.serial_base"


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets empty shared-client/lock registries so tests cannot leak into each other."""
    monkeypatch.setattr(serial_base, "clients", {})
    monkeypatch.setattr(serial_base, "_port_locks", {})
    yield


def _same_port(port: str) -> str:
    """find_usb_serial_port stand-in: every port path resolves to itself."""
    return port


def _new_client(*args: Any, **kwargs: Any) -> MagicMock:
    return MagicMock(name="client")


def _process_registery(raw: dict[int, bytes], registry_map: object = None) -> dict[str, str]:
    """protocol_settings.process_registery stand-in: one variable per command, value is the payload in hex."""
    return {f"v{command}": payload.hex() for command, payload in raw.items()}


def log_of(transport: serial_pylon) -> MagicMock:
    """The transport's logger, which make_transport replaced with a MagicMock."""
    return cast(MagicMock, transport._log)


def make_transport(**values: Any) -> serial_pylon:
    """Construct a serial_pylon with the serial layer mocked out."""
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "baudrate": 9600, "address": 2, **values}
    settings = DummySettings(**merged)
    with (
        patch(f"{BASE_MODULE}.find_usb_serial_port", side_effect=_same_port),
        patch(f"{BASE_MODULE}.get_usb_serial_port_info", return_value="mock"),
        patch(f"{BASE_MODULE}.serial_frame_client", side_effect=_new_client),
    ):
        transport = serial_pylon(settings)  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


# --------------------------------------------------------------------------- #
# protocol version                                                             #
# --------------------------------------------------------------------------- #

def test_build_frame_uses_default_version_until_negotiated() -> None:
    transport = make_transport()
    assert transport.ver == b"\x00"
    frame = transport.build_frame(0x4642)
    assert frame.startswith(b"2002" + b"4642")  # VER 0x20, ADR 0x02, CID1/CID2
    assert transport.ver == b"\x00", "building a frame must not change the stored version"


def test_build_frame_uses_negotiated_version() -> None:
    transport = make_transport()
    transport.ver = b"\x25"
    assert transport.build_frame(0x4642).startswith(b"2502" + b"4642")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"20", b"\x20"),
        (b"2A", b"\x2a"),
        (b"00", None),      # zero means "unknown"
        (b"2", None),       # wrong length
        (b"ZZ", None),      # not hex
        (None, None),
        (32, None),
    ],
)
def test_parse_version(value: Any, expected: bytes | None) -> None:
    assert serial_pylon._parse_version(value) == expected


def test_connect_stores_negotiated_version_as_raw_byte() -> None:
    transport = make_transport()
    # the battery reports its version as ASCII hex; it must be stored raw so build_frame hex-encodes it once
    transport.read_variable = MagicMock(side_effect=[b"20", {"battery_name": "US2000"}])  # type: ignore[method-assign]
    transport.connect()
    assert transport.ver == b"\x20"
    assert transport.connected is True
    assert transport.build_frame(0x4642).startswith(b"2002")


def test_connect_with_unusable_version_keeps_default() -> None:
    transport = make_transport()
    transport.read_variable = MagicMock(side_effect=[b"ZZ", None])  # type: ignore[method-assign]
    transport.connect()
    assert transport.ver == b"\x00"
    assert transport.build_frame(0x4642).startswith(b"2002")


# --------------------------------------------------------------------------- #
# checksum validation                                                          #
# --------------------------------------------------------------------------- #

def test_decode_frame_round_trip_with_info() -> None:
    transport = make_transport()
    # build_frame() output has no SOI/EOI, i.e. it looks like what serial_frame_client.read() returns
    frame = transport.build_frame(0x4642, b"\x02")
    decoded = transport.decode_frame(frame)
    assert decoded is not None
    assert decoded.ver == b"20"
    assert decoded.adr == b"02"
    assert decoded.cid1 == b"46"
    assert decoded.info == b"02"


def test_decode_frame_rejects_bad_checksum() -> None:
    transport = make_transport()
    frame = transport.build_frame(0x4642, b"\x02")
    corrupted = frame[:-1] + (b"0" if frame[-1:] != b"0" else b"1")
    assert transport.decode_frame(corrupted) is None
    log = log_of(transport)
    log.warning.assert_called()
    message = str(log.warning.call_args.args[0])
    assert "expected" in message and "got" in message


def test_decode_frame_checksum_comparison_is_case_insensitive() -> None:
    transport = make_transport()
    frame = transport.build_frame(0x4642)
    assert transport.decode_frame(frame[:-4] + frame[-4:].lower()) is not None


def test_decode_frame_can_ignore_checksum_errors() -> None:
    transport = make_transport(ignore_checksum_errors=True)
    frame = transport.build_frame(0x4642)
    corrupted = frame[:-1] + (b"0" if frame[-1:] != b"0" else b"1")
    decoded = transport.decode_frame(corrupted)
    assert decoded is not None
    assert decoded.cid1 == b"46"


def test_decode_frame_rejects_truncated_frame() -> None:
    transport = make_transport()
    assert transport.decode_frame(b"2002464") is None
    assert transport.decode_frame(b"") is None


# --------------------------------------------------------------------------- #
# polling (payload extraction; the polling loop itself is in test_serial_base)  #
# --------------------------------------------------------------------------- #

def fake_protocol(registers: list[int]) -> MagicMock:
    protocol = MagicMock()
    protocol.get_registry_map.return_value = [SimpleNamespace(register=r, variable_name=f"v{r}") for r in registers]
    protocol.process_registery.side_effect = _process_registery
    return protocol


def test_read_data_skips_corrupt_frames_and_keeps_good_ones() -> None:
    transport = make_transport()
    transport.protocolSettings = fake_protocol([0x4642, 0x4644])
    good = transport.build_frame(0x4642, b"\x01\x02")
    bad = transport.build_frame(0x4644, b"\x01\x02")
    bad = bad[:-1] + (b"0" if bad[-1:] != b"0" else b"1")
    transport._exchange = MagicMock(side_effect=[good, bad])  # type: ignore[method-assign]
    assert transport.read_data() == {"v17986": "0102"}


def test_adr_byte_follows_address_setting() -> None:
    assert make_transport(address=5).adr == b"\x05"
    assert make_transport(address=0).adr == b"\x00"


def test_is_a_serial_base_scraper_with_literal_transport_type() -> None:
    """The web UI scanner reads transport_type with ast, so it must be declared in serial_pylon itself."""
    assert issubclass(serial_pylon, serial_base)
    assert "transport_type" in vars(serial_pylon)
    assert serial_pylon.transport_type == "scraper"
