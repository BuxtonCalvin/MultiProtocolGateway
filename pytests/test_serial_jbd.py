# Description: Unit tests for the JBD BMS transport (serial_jbd) and its jbd_bms protocol definition.
# File: test_serial_jbd.py
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
# These tests deliberately exercise protected members (_read_command, _process_raw, _poll_commands, ...).
"""
Unit tests for classes.transports.serial_jbd.

The test vectors are the worked examples in the JBD communication protocol document (read requests,
the 0x03/0x04/0x05 replies and the 17-cell narrative example), so decoding is checked against the
manufacturer's own numbers. No hardware is involved: the serial port is faked.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from conftest import DummySettings, approx
from serial_support import FakePort, install_serial_layer, unplug

from classes.protocol_settings import protocol_settings
from classes.transports.serial_jbd import serial_jbd
from classes.transports.serial_length_transport import serial_length_transport

REPO: Path = Path(__file__).resolve().parent.parent

# --- vectors from the JBD protocol document -------------------------------------------------------------
REQUEST_03: bytes = bytes.fromhex("DDA50300FFFD77")
REQUEST_04: bytes = bytes.fromhex("DDA50400FFFC77")
REQUEST_05: bytes = bytes.fromhex("DDA50500FFFB77")
RESPONSE_03_EXAMPLE_1: bytes = bytes.fromhex("DD03001B1700000002D003E8000020780000000000001048030F020B760B82FBFF77")
RESPONSE_04_15_CELLS: bytes = bytes.fromhex("DD04001E0F660F630F630F640F3E0F630F370F5B0F650F3B0F630F630F3C0F660F3DF9F977")
RESPONSE_05_VERSION: bytes = bytes.fromhex("DD05000A30313233343536373839FDE977")
NARRATIVE_03: bytes = bytes.fromhex("DD03001F19DFF8240DA50FA00002249100000000000012570311040B980BA90B960B97F89A77")
NARRATIVE_04: bytes = bytes.fromhex("DD0400220EC80EC80ECB0ECF0ECA0EC70ECA0ECD0EC90ECA0ECB0ECB0EC80ECC0EC80EC90EC9F18777")


def payload_of(frame: bytes) -> bytes:
    return frame[4:4 + frame[3]]


def jbd_frame(command: int, data: bytes, status: int = 0) -> bytes:
    """A JBD response built independently of the transport."""
    body: bytes = bytes([status, len(data)]) + data
    return bytes([0xDD, command]) + body + ((0x10000 - sum(body)) & 0xFFFF).to_bytes(2, "big") + b"\x77"


def make(monkeypatch: pytest.MonkeyPatch, port: FakePort | None = None, **values: Any) -> Any:
    install_serial_layer(monkeypatch, port)
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "protocol_version": "jbd_bms", "timeout": "0.05", **values}
    transport: serial_jbd = serial_jbd(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def simulated_pack(unanswered: tuple[int, ...] = ()) -> FakePort:
    """A JBD pack that answers the three read commands with the document's narrative frames."""
    replies: dict[bytes, bytes] = {REQUEST_03: NARRATIVE_03, REQUEST_04: NARRATIVE_04, REQUEST_05: RESPONSE_05_VERSION}
    for command in unanswered:
        replies = {request: reply for request, reply in replies.items() if request[2] != command}
    return FakePort(responder=replies.get)


# --------------------------------------------------------------------------- #
# frames                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("command", "expected"), [(3, REQUEST_03), (4, REQUEST_04), (5, REQUEST_05)])
def test_requests_match_the_documents_frames(monkeypatch: pytest.MonkeyPatch, command: int, expected: bytes) -> None:
    assert make(monkeypatch).build_frame(command) == expected


@pytest.mark.parametrize("frame", [RESPONSE_03_EXAMPLE_1, RESPONSE_04_15_CELLS, RESPONSE_05_VERSION, NARRATIVE_03, NARRATIVE_04])
def test_the_documents_replies_decode(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    decoded: SimpleNamespace | None = make(monkeypatch).decode_frame(frame)
    assert decoded is not None
    assert decoded.command == frame[1]
    assert decoded.status == 0
    assert decoded.payload == payload_of(frame)


def test_the_checksum_rule() -> None:
    """0x10000 minus the sum of the bytes from the command/status through the data."""
    for frame in (RESPONSE_03_EXAMPLE_1, NARRATIVE_04, RESPONSE_05_VERSION):
        assert serial_jbd.calculate_checksum(frame[2:4 + frame[3]]) == int.from_bytes(frame[4 + frame[3]:6 + frame[3]], "big")


def test_a_frame_is_seven_bytes_longer_than_its_data() -> None:
    """The rule the client uses to find the end of a frame: 4 header bytes, the data, 2 checksum bytes, the end marker."""
    for frame in (RESPONSE_03_EXAMPLE_1, NARRATIVE_04, RESPONSE_05_VERSION):
        assert len(frame) == frame[3] + serial_jbd.LENGTH_OFFSET + serial_jbd.LENGTH_ADJUST


def test_frame_layout_matches_what_the_length_client_needs() -> None:
    assert (serial_jbd.START, serial_jbd.LENGTH_OFFSET, serial_jbd.LENGTH_SIZE) == (b"\xdd", 3, 1)


def mutated(frame: bytes, index: int, value: int) -> bytes:
    changed: bytearray = bytearray(frame)
    changed[index] = value
    return bytes(changed)


@pytest.mark.parametrize(
    "frame",
    [
        mutated(RESPONSE_05_VERSION, 0, 0xDC),                # wrong start
        mutated(RESPONSE_05_VERSION, -1, 0x76),               # wrong end
        RESPONSE_05_VERSION[:5],                              # too short
        mutated(RESPONSE_05_VERSION, 3, 0x0B),                # length byte disagrees with the size
        mutated(RESPONSE_05_VERSION, 8, 0x58),                # a data byte changed: checksum no longer holds
        jbd_frame(0x03, b"\x00\x01", status=0x80),            # the pack refused the command
    ],
    ids=["start", "end", "short", "length", "checksum", "refused"],
)
def test_an_invalid_reply_is_rejected_and_logged(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    transport: Any = make(monkeypatch)
    assert transport.decode_frame(frame) is None
    transport._log.warning.assert_called()


def test_a_reply_to_a_different_command_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    transport._exchange = MagicMock(return_value=NARRATIVE_04)
    assert transport._read_command(3) is None


# --------------------------------------------------------------------------- #
# the protocol definition, decoded through the real loader                     #
# --------------------------------------------------------------------------- #

def test_the_protocol_loads_and_names_its_transport() -> None:
    settings = protocol_settings("jbd_bms")
    assert settings.settings["transport"] == "serial_jbd"
    assert int(settings.settings["baud"]) == serial_jbd.DEFAULT_BAUDRATE
    registry_type = next(iter(settings.registry_map))
    names: list[str] = [entry.variable_name for entry in settings.get_registry_map(registry_type)]
    assert len(names) == len(set(names)), "variable names must be unique"


def test_it_polls_the_three_read_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._poll_commands() == [3, 4, 5]


def test_the_documents_narrative_example_decodes_to_its_stated_values(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({3: payload_of(NARRATIVE_03), 4: payload_of(NARRATIVE_04), 5: payload_of(RESPONSE_05_VERSION)})

    assert out["total_voltage"] == approx(66.23)
    assert out["current"] == approx(-20.12)
    assert out["residual_capacity"] == approx(34.93)
    assert out["nominal_capacity"] == approx(40.0)
    assert out["cycle_count"] == 2
    assert out["state_of_charge"] == 87
    assert out["cell_count"] == 17
    assert out["temperature_sensor_count"] == 4
    for i, expected in enumerate([23.7, 25.4, 23.5, 23.6], start=1):
        assert out[f"temperature_{i}"] == approx(expected)
    assert out["cell_1_voltage"] == approx(3.784)
    assert out["cell_4_voltage"] == approx(3.791)
    assert out["cell_17_voltage"] == approx(3.785)
    assert out["mosfet_status"] == "charging MOSFET on,discharging MOSFET on"
    assert out["hardware_version"] == "0123456789"


def test_cells_and_sensors_the_pack_does_not_have_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """The map has 32 cell rows and 6 temperature rows; a 17-cell pack with 4 sensors must not get zero-volt phantoms."""
    out: dict[str, Any] = make(monkeypatch)._process_raw({3: payload_of(NARRATIVE_03), 4: payload_of(NARRATIVE_04)})
    assert "cell_17_voltage" in out
    assert "cell_18_voltage" not in out
    assert "cell_32_voltage" not in out
    assert "temperature_4" in out
    assert "temperature_5" not in out
    assert "temperature_6" not in out


def test_a_short_version_string_is_still_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """The map reserves 31 characters for the text, but the pack returns only what it has."""
    out: dict[str, Any] = make(monkeypatch)._process_raw({5: b"JBD-SP04S"})
    assert out["hardware_version"] == "JBD-SP04S"


def test_protection_flags_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    data: bytearray = bytearray(payload_of(NARRATIVE_03))
    data[16:18] = (0b0000_0000_0000_0101).to_bytes(2, "big")  # cell block over voltage, battery over voltage
    out: dict[str, Any] = make(monkeypatch)._process_raw({3: bytes(data)})
    assert out["protection_status"] == "cell block over voltage,battery over voltage"


def test_a_clean_pack_reports_no_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._process_raw({3: payload_of(NARRATIVE_03)})["protection_status"] == ""


def test_balancing_cells_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    data: bytearray = bytearray(payload_of(NARRATIVE_03))
    data[12:14] = (0b0000_0000_0000_0110).to_bytes(2, "big")  # cells 2 and 3
    out: dict[str, Any] = make(monkeypatch)._process_raw({3: bytes(data)})
    assert out["balance_status_1_16"] == "cell 2 balancing,cell 3 balancing"


@pytest.mark.parametrize(("flags", "expected"), [(0b00, ""), (0b01, "charging MOSFET on"), (0b10, "discharging MOSFET on")])
def test_each_mosfet_is_reported_on_its_own(monkeypatch: pytest.MonkeyPatch, flags: int, expected: str) -> None:
    data: bytearray = bytearray(payload_of(NARRATIVE_03))
    data[20] = flags
    assert make(monkeypatch)._process_raw({3: bytes(data)})["mosfet_status"] == expected


def test_a_charging_pack_reports_a_positive_current(monkeypatch: pytest.MonkeyPatch) -> None:
    data: bytearray = bytearray(payload_of(NARRATIVE_03))
    data[2:4] = (1500).to_bytes(2, "big")
    assert make(monkeypatch)._process_raw({3: bytes(data)})["current"] == approx(15.0)


def test_a_command_that_did_not_answer_leaves_its_variables_out(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({3: payload_of(NARRATIVE_03)})
    assert "total_voltage" in out
    assert "cell_1_voltage" not in out
    assert "hardware_version" not in out


# --------------------------------------------------------------------------- #
# the whole stack: real client, real framing, real protocol, a simulated pack  #
# --------------------------------------------------------------------------- #

def test_a_full_poll_of_a_simulated_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = simulated_pack()
    transport: Any = make(monkeypatch, port)

    out: dict[str, Any] = transport.read_data()

    assert port.written == [REQUEST_03, REQUEST_04, REQUEST_05]
    assert out["total_voltage"] == approx(66.23)
    assert out["cell_17_voltage"] == approx(3.785)
    assert out["hardware_version"] == "0123456789"


def test_a_pack_that_stops_answering_one_command_still_gives_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, simulated_pack(unanswered=(5,)))
    transport._start_cycle_tracking()

    out: dict[str, Any] = transport.read_data()

    assert "total_voltage" in out
    assert "hardware_version" not in out
    result = transport.get_cycle_result()
    assert (result.expected_units, result.completed_units) == (3, 2)
    assert result.is_complete is False, "bridges that need a complete cycle must not write this poll"


def test_the_interleaved_read_takes_one_step_per_command(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, simulated_pack())
    assert list(transport.read_data_iter()) == [True, True, True]
    assert transport.get_partial_data()["state_of_charge"] == 87


@pytest.mark.parametrize("noise", [b"\x00\x13\x37", b"\xdd\x03\x00\xff"], ids=["junk", "stray start with an impossible length"])
def test_noise_ahead_of_the_reply_does_not_stop_the_poll(monkeypatch: pytest.MonkeyPatch, noise: bytes) -> None:
    """Junk, and a stray start byte whose length is larger than any JBD reply, are skipped; the exchange still succeeds."""
    def responder(request: bytes) -> bytes | None:
        return noise + NARRATIVE_03 if request == REQUEST_03 else None

    transport: Any = make(monkeypatch, FakePort(responder=responder))
    assert transport._read_command(3) == payload_of(NARRATIVE_03)


def test_a_port_that_is_not_there_yet_returns_nothing_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, simulated_pack())
    unplug(monkeypatch)
    assert transport.read_data() == {}, "no adapter: the poll is empty, the gateway keeps retrying"


# --------------------------------------------------------------------------- #
# settings and structure                                                      #
# --------------------------------------------------------------------------- #

def test_the_baud_rate_defaults_to_the_packs_usual(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).baudrate == 9600
    assert make(monkeypatch, baudrate=19200).baudrate == 19200


def test_jbd_has_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="0-0"):
        make(monkeypatch, address=1)


def test_it_is_a_scraper_on_the_length_family(monkeypatch: pytest.MonkeyPatch) -> None:
    assert issubclass(serial_jbd, serial_length_transport)
    assert "transport_type" in vars(serial_jbd), "the web UI scanner reads it with ast; it cannot be inherited"
    assert serial_jbd.transport_type == "scraper"
    assert make(monkeypatch).scrape_target != ""


def test_the_protocol_files_are_consistent() -> None:
    meta: dict[str, Any] = json.loads((REPO / "protocols/jbd/jbd_bms.json").read_text(encoding="utf-8"))
    assert meta["transport"] == "serial_jbd"
    rows: list[dict[str, str]] = list(csv.DictReader((REPO / "protocols/jbd/jbd_bms.registry_map.csv").open(newline="", encoding="utf-8")))
    registers: set[str] = {row["register"].split(".")[0] for row in rows}
    assert registers == {"3", "4", "5"}, "the map reads only the three read commands"
    # flag tables are found by the entry's normalized documented name, which is not always its variable name
    for name in ("protection_status", "mosfet_status", "balance_status_cells_1-16", "balance_status_cells_17-32"):
        assert f"{name}_codes" in meta
