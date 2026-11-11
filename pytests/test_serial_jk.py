# Description: Unit tests for the JK BMS transport (serial_jk) and its jk_bms_4g_gps_serial protocol definition.
# File: test_serial_jk.py
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
# These tests deliberately exercise protected members (_parse_items, _normalize, _process_raw, ...).
"""
Unit tests for classes.transports.serial_jk.

One frame here is a real capture: the read-all request that the esphome-jk-bms project sends to JK
hardware. The replies are built from the frame layout in the JK protocol document by an independent
builder in this file, so they check the transport against the specification, not against itself;
they are not captures from a battery. No hardware is involved: the serial port is faked.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from conftest import DummySettings
from serial_support import FakePort, install_serial_layer, unplug

from classes.protocol_settings import protocol_settings
from classes.transports.serial_jk import serial_jk
from classes.transports.serial_length_transport import serial_length_transport

REPO: Path = Path(__file__).resolve().parent.parent

ESPHOME_READ_ALL_REQUEST: bytes = bytes.fromhex("4E5700130000000006020000000000006800000128")
''' the request esphome-jk-bms sends: terminal 0, command 06 (read all), source 02, identifier 00, record 0 '''


def checksum(data: bytes) -> bytes:
    return (sum(data) & 0xFFFF).to_bytes(2, "big")


def jk_response(items: bytes, terminal: int = 0, command: int = 0x06, source: int = 0, kind: int = 1, record: int = 1) -> bytes:
    """A JK frame built from the document's layout, independently of the transport."""
    length: int = 2 + 4 + 1 + 1 + 1 + len(items) + 4 + 1 + 4
    body: bytes = (b"\x4e\x57" + length.to_bytes(2, "big") + terminal.to_bytes(4, "big") + bytes([command, source, kind])
                   + items + record.to_bytes(4, "big") + b"\x68")
    return body + b"\x00\x00" + checksum(body)


def item(identifier: int, value: int, size: int = 2) -> bytes:
    return bytes([identifier]) + value.to_bytes(size, "big")


def cells(millivolts: list[int]) -> bytes:
    """Identifier 0x79: a length byte, then groups of (cell number, millivolts)."""
    groups: bytes = b"".join(bytes([number + 1]) + mv.to_bytes(2, "big") for number, mv in enumerate(millivolts))
    return bytes([0x79, len(groups)]) + groups


def sample_items(protocol_version: int = 1, current_raw: int = 0x87D0) -> bytes:
    """A four-cell pack reporting everything the map reads, with some parameter items in between."""
    return (cells([3300, 3301, 3302, 3299])
            + item(0x80, 25) + item(0x81, 26) + item(0x82, 101)           # 101 means -1 degree
            + item(0x83, 5280) + item(0x84, current_raw)                  # 52.80 V
            + item(0x85, 85, 1) + item(0x86, 3, 1) + item(0x87, 10)
            + item(0x89, 100, 4) + item(0x8A, 4)
            + item(0x8B, 0b0101) + item(0x8C, 0b0111)
            + item(0x8E, 5800) + item(0x9D, 1, 1) + item(0xB2, 0, 10)    # parameters the map does not read
            + item(0xB7, int.from_bytes(b"NW_1_0_0_200428", "big"), 15)
            + item(0xC0, protocol_version, 1))


def assert_values(out: dict[str, Any], expected: dict[str, float]) -> None:
    """Every named variable is present and equal to its expected number, to nine significant figures."""
    for name, value in expected.items():
        assert name in out, f"{name} was not decoded"
        assert math.isclose(float(out[name]), value, rel_tol=1e-9), f"{name}: got {out[name]!r}, expected {value}"


def make(monkeypatch: pytest.MonkeyPatch, port: FakePort | None = None, **values: Any) -> Any:
    install_serial_layer(monkeypatch, port)
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "protocol_version": "jk_bms_4g_gps_serial", "timeout": "0.05", **values}
    transport: serial_jk = serial_jk(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


# --------------------------------------------------------------------------- #
# frames                                                                      #
# --------------------------------------------------------------------------- #

def test_the_read_all_request_matches_the_captured_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).build_frame(0x06) == ESPHOME_READ_ALL_REQUEST


def test_the_captured_requests_checksum_follows_the_documents_rule() -> None:
    """The plain sum of every byte from the start marker through the end marker, in the low two bytes of the last four."""
    assert checksum(ESPHOME_READ_ALL_REQUEST[:-4]) == ESPHOME_READ_ALL_REQUEST[-2:]
    assert ESPHOME_READ_ALL_REQUEST[-4:-2] == b"\x00\x00"


def test_the_address_is_the_terminal_number(monkeypatch: pytest.MonkeyPatch) -> None:
    frame: bytes = make(monkeypatch, address=5).build_frame(0x06)
    assert frame[4:8] == b"\x00\x00\x00\x05"
    assert checksum(frame[:-4]) == frame[-2:]
    assert frame != ESPHOME_READ_ALL_REQUEST


def test_a_request_with_data_grows_its_length_field(monkeypatch: pytest.MonkeyPatch) -> None:
    frame: bytes = make(monkeypatch).build_frame(0x03, b"\x83")
    assert int.from_bytes(frame[2:4], "big") == 19 and len(frame) == 21
    assert frame[11] == 0x83
    assert checksum(frame[:-4]) == frame[-2:]


def test_the_frame_layout_matches_what_the_length_client_needs() -> None:
    assert (serial_jk.START, serial_jk.LENGTH_OFFSET, serial_jk.LENGTH_SIZE, serial_jk.LENGTH_ADJUST) == (b"\x4e\x57", 2, 2, 0)
    reply: bytes = jk_response(sample_items())
    assert len(reply) == serial_jk.LENGTH_OFFSET + int.from_bytes(reply[2:4], "big") + serial_jk.LENGTH_ADJUST


def test_a_reply_decodes_to_its_item_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    items: bytes = sample_items()
    decoded: SimpleNamespace | None = make(monkeypatch).decode_frame(jk_response(items, command=0x06, source=0))
    assert decoded is not None
    assert decoded.payload == items
    assert (decoded.command_word, decoded.source, decoded.type) == (0x06, 0, 1)


def test_an_active_upload_is_accepted_like_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded: SimpleNamespace | None = make(monkeypatch).decode_frame(jk_response(sample_items(), kind=2))
    assert decoded is not None
    assert decoded.type == 2


def test_the_echoed_command_word_is_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transport cannot tell from the document that every model echoes it, so it must not reject on it."""
    transport: Any = make(monkeypatch)
    transport._exchange = MagicMock(return_value=jk_response(sample_items(), command=0x03))
    assert transport._read_command(0x06) == sample_items()


def mutated(frame: bytes, index: int, value: int) -> bytes:
    changed: bytearray = bytearray(frame)
    changed[index] = value
    return bytes(changed)


@pytest.mark.parametrize(
    "frame",
    [
        mutated(jk_response(sample_items()), 0, 0x4F),                 # wrong start
        jk_response(sample_items())[:10],                              # too short
        mutated(jk_response(sample_items()), 3, 0x20),                 # the length field disagrees with the size
        mutated(jk_response(sample_items()), -5, 0x69),                # the end marker is not 0x68
        mutated(jk_response(sample_items()), 20, 0xFF),               # a data byte changed: the checksum no longer holds
    ],
    ids=["start", "short", "length", "end", "checksum"],
)
def test_an_invalid_reply_is_rejected_and_logged(monkeypatch: pytest.MonkeyPatch, frame: bytes) -> None:
    transport: Any = make(monkeypatch)
    assert transport.decode_frame(frame) is None
    transport._log.warning.assert_called()


def test_a_request_frame_is_not_taken_for_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that echoes our request back (transport type 0) has sent no data."""
    assert make(monkeypatch).decode_frame(jk_response(sample_items(), kind=0)) is None


# --------------------------------------------------------------------------- #
# the items                                                                   #
# --------------------------------------------------------------------------- #

def test_every_item_is_split_out_with_its_size(monkeypatch: pytest.MonkeyPatch) -> None:
    items: dict[int, bytes] = make(monkeypatch)._parse_items(sample_items())
    assert items[0x79] == bytes([1, 0x0C, 0xE4, 2, 0x0C, 0xE5, 3, 0x0C, 0xE6, 4, 0x0C, 0xE3])
    assert items[0x83] == (5280).to_bytes(2, "big")
    assert items[0x85] == b"\x55"
    assert items[0x89] == (100).to_bytes(4, "big")
    assert items[0xB7] == b"NW_1_0_0_200428"
    assert items[0xC0] == b"\x01"
    assert 0x8E in items and 0xB2 in items, "parameter items are skipped over by size, not dropped"


def test_an_identifier_outside_the_table_stops_parsing_but_keeps_what_came_before(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    items: dict[int, bytes] = transport._parse_items(item(0x83, 5280) + b"\xf0\x01\x02" + item(0x85, 85, 1))
    assert list(items) == [0x83], "an unknown identifier has an unknown size, so nothing after it can be trusted"


def test_a_cut_off_item_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._parse_items(item(0x83, 5280) + b"\x89\x00\x00") == {0x83: (5280).to_bytes(2, "big")}


def test_a_cell_list_with_a_missing_length_byte_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._parse_items(item(0x83, 5280) + b"\x79") == {0x83: (5280).to_bytes(2, "big")}


@pytest.mark.parametrize(("raw", "degrees"), [(0, 0), (25, 25), (100, 100), (101, -1), (115, -15), (140, -40)])
def test_temperatures_above_100_are_negative(raw: int, degrees: int) -> None:
    assert serial_jk._temperature(raw) == degrees


@pytest.mark.parametrize(
    ("version", "raw", "amps"),
    [
        (1, 0x07D0, -20.0),      # 20 A discharging
        (1, 0x87D0, 20.0),       # 20 A charging: the top bit
        (1, 0, 0.0),
        (0, 11000, -10.0),       # the older encoding: offset by 10000, discharge above it
        (0, 9500, 5.0),
        (0, 10000, 0.0),
    ],
)
def test_the_current_is_made_positive_when_charging_under_either_encoding(monkeypatch: pytest.MonkeyPatch, version: int, raw: int, amps: float) -> None:
    transport: Any = make(monkeypatch)
    normalized: dict[int, bytes] = transport._normalize({0x84: raw.to_bytes(2, "big"), 0xC0: bytes([version])})
    assert int.from_bytes(normalized[0x84], "big", signed=True) == round(amps * 100)


def test_a_missing_protocol_version_means_the_older_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    normalized: dict[int, bytes] = make(monkeypatch)._normalize({0x84: (9500).to_bytes(2, "big")})
    assert int.from_bytes(normalized[0x84], "big", signed=True) == 500


def test_an_extreme_current_is_clamped_to_what_fits_in_16_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    normalized: dict[int, bytes] = make(monkeypatch)._normalize({0x84: (65535).to_bytes(2, "big"), 0xC0: b"\x00"})
    assert int.from_bytes(normalized[0x84], "big", signed=True) == -32768


def test_other_items_pass_through_normalization_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    original: dict[int, bytes] = {0x83: (5280).to_bytes(2, "big"), 0x85: b"\x55"}
    assert make(monkeypatch)._normalize(original) == original


# --------------------------------------------------------------------------- #
# the protocol definition, decoded through the real loader                     #
# --------------------------------------------------------------------------- #

def test_the_protocol_loads_and_names_its_transport() -> None:
    settings = protocol_settings("jk_bms_4g_gps_serial")
    assert settings.settings["transport"] == "serial_jk"
    assert int(settings.settings["baud"]) == serial_jk.DEFAULT_BAUDRATE
    registry_type = next(iter(settings.registry_map))
    names: list[str] = [entry.variable_name for entry in settings.get_registry_map(registry_type)]
    assert len(names) == len(set(names)), "variable names must be unique"


def test_one_request_serves_the_whole_map(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._poll_commands() == [0x06]


def test_the_sample_reply_decodes_to_its_values(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0x06: sample_items()})

    assert_values(out, {
        "cell_1_voltage": 3.300, "cell_2_voltage": 3.301, "cell_3_voltage": 3.302, "cell_4_voltage": 3.299,
        "mosfet_temperature": 25, "battery_box_temperature": 26, "battery_temperature": -1,
        "total_voltage": 52.80, "current": 20.00, "state_of_charge": 85, "temperature_sensor_count": 3,
        "cycle_count": 10, "total_cycle_capacity": 100, "cell_count": 4, "protocol_version": 1,
    })
    assert out["software_version"] == "NW_1_0_0_200428"
    assert out["warning_status"] == "low capacity alarm,charge overvoltage alarm"
    assert out["battery_status"] == "charging MOSFET on,discharging MOSFET on,balancing on"


def test_cells_the_pack_does_not_have_are_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0x06: sample_items()})
    assert "cell_4_voltage" in out
    assert "cell_5_voltage" not in out
    assert "cell_32_voltage" not in out


def test_a_discharging_pack_reports_a_negative_current(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0x06: sample_items(current_raw=0x0BB8)})  # 30 A out
    assert_values(out, {"current": -30.0})


def test_the_older_protocol_version_decodes_its_current(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0x06: sample_items(protocol_version=0, current_raw=9500)})
    assert_values(out, {"current": 5.0, "protocol_version": 0})


def test_a_pack_without_a_battery_connected_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0x06: sample_items()})
    assert "battery connected" not in out["battery_status"], "bit 3 is clear in the sample"


def test_nothing_is_decoded_without_a_reply_or_a_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    assert transport._process_raw({}) == {}
    transport.protocolSettings = None
    assert transport._process_raw({0x06: sample_items()}) == {}


# --------------------------------------------------------------------------- #
# the whole stack: real client, real framing, real protocol, a simulated pack  #
# --------------------------------------------------------------------------- #

def pack_replying_with(frame: bytes | None) -> FakePort:
    def responder(request: bytes) -> bytes | None:
        return frame if request == ESPHOME_READ_ALL_REQUEST else None

    return FakePort(responder=responder)


def test_a_full_poll_of_a_simulated_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = pack_replying_with(jk_response(sample_items()))
    transport: Any = make(monkeypatch, port)

    out: dict[str, Any] = transport.read_data()

    assert port.written == [ESPHOME_READ_ALL_REQUEST]
    assert_values(out, {"total_voltage": 52.80, "current": 20.0, "state_of_charge": 85})
    assert out["software_version"] == "NW_1_0_0_200428"


def test_the_reply_may_arrive_in_small_pieces(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = pack_replying_with(jk_response(sample_items()))
    port.chunk_size = 7
    assert_values(make(monkeypatch, port).read_data(), {"total_voltage": 52.80})


def test_a_pack_that_does_not_answer_gives_an_empty_incomplete_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack_replying_with(None))
    transport._start_cycle_tracking()
    assert transport.read_data() == {}
    assert transport.get_cycle_result().is_complete is False


def test_a_corrupted_reply_is_discarded_not_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    corrupted: bytes = mutated(jk_response(sample_items()), 30, 0x00)
    transport: Any = make(monkeypatch, pack_replying_with(corrupted))
    assert transport.read_data() == {}


def test_noise_ahead_of_the_reply_does_not_stop_the_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack_replying_with(b"\x00\x4e\x00\x13" + jk_response(sample_items())))
    assert_values(transport.read_data(), {"total_voltage": 52.80})


def test_the_interleaved_read_takes_a_single_step(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack_replying_with(jk_response(sample_items())))
    assert list(transport.read_data_iter()) == [True]
    assert transport.get_partial_data()["state_of_charge"] == 85


def test_a_port_that_is_not_there_yet_returns_nothing_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, pack_replying_with(jk_response(sample_items())))
    unplug(monkeypatch)
    assert transport.read_data() == {}


# --------------------------------------------------------------------------- #
# settings and structure                                                      #
# --------------------------------------------------------------------------- #

def test_the_baud_rate_defaults_to_115200(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).baudrate == 115200
    assert make(monkeypatch, baudrate=9600).baudrate == 9600


def test_the_terminal_number_uses_up_to_24_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch, address=0xFFFFFF).address == 0xFFFFFF
    with pytest.raises(ValueError, match="address must be"):
        make(monkeypatch, address=0x1000000)


def test_it_is_a_scraper_on_the_length_family(monkeypatch: pytest.MonkeyPatch) -> None:
    assert issubclass(serial_jk, serial_length_transport)
    assert "transport_type" in vars(serial_jk), "the web UI scanner reads it with ast; it cannot be inherited"
    assert serial_jk.transport_type == "scraper"
    assert make(monkeypatch).scrape_target != ""
    assert make(monkeypatch, address=1).scrape_target != make(monkeypatch, address=2).scrape_target


def test_the_protocol_files_are_consistent() -> None:
    meta: dict[str, Any] = json.loads((REPO / "protocols/jk/jk_bms_4g_gps_serial.json").read_text(encoding="utf-8"))
    assert meta["transport"] == "serial_jk"
    rows: list[dict[str, str]] = list(csv.DictReader((REPO / "protocols/jk/jk_bms_4g_gps_serial.registry_map.csv").open(newline="", encoding="utf-8")))
    identifiers: set[int] = {int(row["register"].split(".")[0]) for row in rows}
    known: set[int] = {serial_jk.CELL_VOLTAGES, *serial_jk.ITEM_SIZES}
    assert identifiers <= known, f"the map reads identifiers the transport cannot parse: {sorted(identifiers - known)}"
    for name in ("warning_status", "battery_status"):
        assert f"{name}_codes" in meta
