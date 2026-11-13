# Description: Unit tests for the VE.Direct transport (serial_vedirect), its vedirect_client state machine, and the victron_vedirect_serial protocol.
# File: test_serial_vedirect.py
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
# These tests deliberately exercise protected members (_encode_value, _process_raw, _check_shared_client, ...).
"""
Unit tests for classes.transports.serial_vedirect.

The reference block is the BMV-700 example from Victron's VE.Direct documentation, whose checksum
byte is 0xD8; the block builder here is checked against it, then used to make the awkward cases
(a checksum byte that looks like ":" or a line ending, HEX messages in and around a block). No
hardware is involved: the serial port is faked.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from conftest import DummySettings
from serial_support import FakePort, install_serial_layer, unplug

from classes.protocol_settings import Registry_Type, protocol_settings
from classes.transports.serial_base import serial_base
from classes.transports.serial_length_transport import serial_length_client
from classes.transports.serial_vedirect import serial_vedirect, vedirect_client

REPO: Path = Path(__file__).resolve().parent.parent

BMV_700_RECORDS: list[tuple[str, str]] = [
    ("PID", "0x203"), ("V", "26201"), ("I", "0"), ("P", "0"), ("CE", "0"), ("SOC", "1000"),
    ("TTG", "-1"), ("Alarm", "OFF"), ("Relay", "OFF"), ("AR", "0"), ("BMV", "700"), ("FW", "0307"),
]
''' the records of the example block in Victron's documentation '''


def make_block(records: list[tuple[str, str]], label: str = "Checksum") -> bytes:
    """A complete text block, with the checksum byte that makes every byte sum to zero."""
    body: bytes = b"".join(b"\r\n" + key.encode() + b"\t" + value.encode() for key, value in records) + b"\r\n" + label.encode() + b"\t"
    return body + bytes([(-sum(body)) & 0xFF])


def records_of(block: bytes) -> bytes:
    """What the client returns for a block: the records, without the Checksum record."""
    cut: int = block.rindex(b"\r\n")
    return block[:cut]


def block_ending_in(checksum_byte: int) -> bytes:
    """
    A block whose checksum byte is a particular value. A record carrying three printable characters
    (never ':' or a control character, which would change how the block parses) can add any amount
    from 96 to 378 to the sum, which is enough to reach every checksum byte.
    """
    printable: list[int] = [code for code in range(32, 127) if code != 0x3A]
    for first in printable:
        for second in printable:
            for third in printable:
                block: bytes = make_block([("V", "12000"), ("X", bytes([first, second, third]).decode("ascii"))])
                if block[-1] == checksum_byte:
                    return block
    pytest.fail(f"no block with a checksum byte of {checksum_byte:#04x}")


BMV_700_BLOCK: bytes = make_block(BMV_700_RECORDS)
HEX_MESSAGE: bytes = b":7F0ED0004E4\n"


def assert_values(out: dict[str, Any], expected: dict[str, float]) -> None:
    """Every named variable is present and equal to its expected number, to nine significant figures."""
    for name, value in expected.items():
        assert name in out, f"{name} was not decoded"
        assert math.isclose(float(out[name]), value, rel_tol=1e-9), f"{name}: got {out[name]!r}, expected {value}"


def make_client(monkeypatch: pytest.MonkeyPatch, port: FakePort | None = None) -> vedirect_client:
    install_serial_layer(monkeypatch, port)
    client: vedirect_client = vedirect_client("/dev/ttyUSB0", 19200)
    client.timeout = 0.2
    return client


def feed_all(client: vedirect_client, data: bytes) -> list[bytes]:
    """Every block the state machine completes while being fed these bytes."""
    return [records for byte in data if (records := client.feed(byte)) is not None]


def make(monkeypatch: pytest.MonkeyPatch, port: FakePort | None = None, **values: Any) -> Any:
    install_serial_layer(monkeypatch, port)
    merged: dict[str, Any] = {"port": "/dev/ttyUSB0", "protocol_version": "victron_vedirect_serial", "timeout": "0.2", **values}
    transport: serial_vedirect = serial_vedirect(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


# --------------------------------------------------------------------------- #
# the reference block                                                         #
# --------------------------------------------------------------------------- #

def test_the_builder_reproduces_victrons_published_checksum() -> None:
    assert BMV_700_BLOCK[-1] == 0xD8, "Victron's example block ends in the checksum byte 0xD8"
    assert sum(BMV_700_BLOCK) % 256 == 0


# --------------------------------------------------------------------------- #
# the state machine                                                           #
# --------------------------------------------------------------------------- #

def test_a_valid_block_is_returned_as_its_records(monkeypatch: pytest.MonkeyPatch) -> None:
    assert feed_all(make_client(monkeypatch), BMV_700_BLOCK) == [records_of(BMV_700_BLOCK)]


def test_the_records_are_parsed_by_label() -> None:
    parsed: dict[str, str] = serial_vedirect.parse_records(records_of(BMV_700_BLOCK))
    assert parsed["PID"] == "0x203"
    assert parsed["V"] == "26201"
    assert parsed["TTG"] == "-1"
    assert parsed["ALARM"] == "OFF", "labels are matched case-insensitively, so they are upper-cased"
    assert len(parsed) == len(BMV_700_RECORDS)


def test_a_corrupted_block_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    damaged: bytearray = bytearray(BMV_700_BLOCK)
    damaged[20] ^= 0x01
    assert feed_all(make_client(monkeypatch), bytes(damaged)) == []


def test_a_good_block_after_a_bad_one_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    damaged: bytearray = bytearray(BMV_700_BLOCK)
    damaged[20] ^= 0x01
    assert feed_all(make_client(monkeypatch), bytes(damaged) + BMV_700_BLOCK) == [records_of(BMV_700_BLOCK)]


def test_consecutive_blocks_each_come_back(monkeypatch: pytest.MonkeyPatch) -> None:
    other: bytes = make_block([("V", "12500"), ("SOC", "950")])
    assert feed_all(make_client(monkeypatch), BMV_700_BLOCK + other + BMV_700_BLOCK) == [records_of(BMV_700_BLOCK), records_of(other), records_of(BMV_700_BLOCK)]


def test_joining_the_stream_in_the_middle_of_a_block_resynchronizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first block is partial and fails its checksum; the next whole one is returned."""
    tail: bytes = BMV_700_BLOCK[len(BMV_700_BLOCK) // 2:]
    assert feed_all(make_client(monkeypatch), tail + BMV_700_BLOCK) == [records_of(BMV_700_BLOCK)]


def test_a_hex_message_between_blocks_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    stream: bytes = BMV_700_BLOCK + HEX_MESSAGE + BMV_700_BLOCK
    assert feed_all(make_client(monkeypatch), stream) == [records_of(BMV_700_BLOCK), records_of(BMV_700_BLOCK)]


def test_a_hex_message_in_the_middle_of_a_block_spoils_that_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """As in Victron's reference handler: the checksum restarts after the HEX message, so the block does not validate."""
    middle: int = BMV_700_BLOCK.index(b"\r\nSOC")
    interrupted: bytes = BMV_700_BLOCK[:middle] + HEX_MESSAGE + BMV_700_BLOCK[middle:]
    assert feed_all(make_client(monkeypatch), interrupted) == []
    assert feed_all(make_client(monkeypatch), interrupted + BMV_700_BLOCK) == [records_of(BMV_700_BLOCK)], "and the next block is fine"


@pytest.mark.parametrize("checksum_byte", [0x3A, 0x0D, 0x0A, 0x09, 0x00], ids=["colon", "carriage return", "newline", "tab", "zero"])
def test_a_checksum_byte_that_looks_like_something_else_is_still_a_checksum(monkeypatch: pytest.MonkeyPatch, checksum_byte: int) -> None:
    """A ':' there is not the start of a HEX message, and line endings there do not end a record."""
    block: bytes = block_ending_in(checksum_byte)
    assert feed_all(make_client(monkeypatch), block + BMV_700_BLOCK) == [records_of(block), records_of(BMV_700_BLOCK)]


def test_the_checksum_label_is_matched_without_regard_to_case(monkeypatch: pytest.MonkeyPatch) -> None:
    block: bytes = make_block(BMV_700_RECORDS, label="CHECKSUM")
    assert feed_all(make_client(monkeypatch), block) == [records_of(block)]


def test_noise_that_never_ends_a_block_does_not_grow_without_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client: vedirect_client = make_client(monkeypatch)
    assert feed_all(client, b"\r\nV\t1" * 2000) == []
    assert len(client._frame) <= vedirect_client.MAX_FRAME
    # the checksum has been accumulating over the noise, so the first block after it fails; the next is found
    assert feed_all(client, BMV_700_BLOCK + BMV_700_BLOCK) == [records_of(BMV_700_BLOCK)]


# --------------------------------------------------------------------------- #
# reading from the port                                                       #
# --------------------------------------------------------------------------- #

def test_a_block_arriving_in_small_pieces_is_assembled(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = FakePort(chunks=[], chunk_size=5)
    port.queue(BMV_700_BLOCK)
    assert make_client(monkeypatch, port).read(reset_buffer=False) == records_of(BMV_700_BLOCK)


def test_it_times_out_when_the_device_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    client: vedirect_client = make_client(monkeypatch, FakePort())
    client.timeout = 0.05
    assert client.read() is None


def test_a_fresh_read_discards_what_was_buffered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The block returned should be the next one, not seconds old."""
    stale: FakePort = FakePort(chunks=[BMV_700_BLOCK])
    client: vedirect_client = make_client(monkeypatch, stale)
    client.timeout = 0.05
    assert client.read() is None
    assert stale.input_resets == 1


def test_bytes_after_a_block_are_kept_for_the_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    other: bytes = make_block([("V", "12500")])
    port: FakePort = FakePort(chunks=[BMV_700_BLOCK + other])
    client: vedirect_client = make_client(monkeypatch, port)
    assert client.read(reset_buffer=False) == records_of(BMV_700_BLOCK)
    assert client.read(reset_buffer=False) == records_of(other)


def test_close_is_idempotent_and_connect_needs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = FakePort()
    client: vedirect_client = make_client(monkeypatch, port)
    assert client.connect() is True
    client.close()
    client.close()
    assert port.close_calls == 1


# --------------------------------------------------------------------------- #
# encoding a value for the protocol map                                       #
# --------------------------------------------------------------------------- #

def entry_for(monkeypatch: pytest.MonkeyPatch, key: str) -> Any:
    transport: Any = make(monkeypatch)
    for entry in transport._registry_map():
        if entry.register_key == key and not entry.description_source:
            return entry
    pytest.fail(f"the protocol has no {key} entry")


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        ("V", "12830", (12830).to_bytes(4, "big", signed=True)),          # a signed integer
        ("I", "-1500", (-1500).to_bytes(4, "big", signed=True)),          # negative current
        ("AR", "3", (3).to_bytes(4, "big")),                              # an unsigned integer
        ("PID", "0x203", b"\x02\x03"),                                    # hex text becomes its number
        ("BMV", "BMV-712 Smart", b"BMV-712 Smart"),                       # text stays text
        ("Alarm", "OFF", b"OFF"),
    ],
)
def test_a_value_is_encoded_by_its_declared_type(monkeypatch: pytest.MonkeyPatch, key: str, text: str, expected: bytes) -> None:
    transport: Any = make(monkeypatch)
    assert transport._encode_value(entry_for(monkeypatch, key), text) == expected


@pytest.mark.parametrize(("key", "text"), [("V", "---"), ("I", ""), ("AR", "-5"), ("PID", "zz"), ("V", "99999999999")])
def test_a_value_that_is_not_a_number_of_its_type_is_skipped(monkeypatch: pytest.MonkeyPatch, key: str, text: str) -> None:
    assert make(monkeypatch)._encode_value(entry_for(monkeypatch, key), text) is None


# --------------------------------------------------------------------------- #
# the protocol definition, decoded through the real loader                     #
# --------------------------------------------------------------------------- #

def test_the_protocol_loads_with_text_keys_and_names_its_transport() -> None:
    settings = protocol_settings("victron_vedirect_serial")
    assert settings.settings["transport"] == "serial_vedirect"
    entries = settings.get_registry_map(Registry_Type.HOLDING)
    keys: list[str] = [entry.register_key.upper() for entry in entries if entry.register_key and not entry.description_source]
    assert len(keys) == len(set(keys)), "each label may appear once (the derived _desc entries share their source's label by design)"
    derived: list[str] = [entry.variable_name for entry in entries if entry.description_source]
    assert derived, "coded fields get a derived description entry, which the transport must not encode as the number itself"
    assert {"V", "I", "SOC", "PID", "CS", "ERR"} <= set(keys)


def test_the_reference_block_decodes_to_its_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0: records_of(BMV_700_BLOCK)})

    assert_values(out, {"battery_voltage": 26201, "battery_current": 0, "battery_power": 0, "consumed_energy": 0,
                        "state_of_charge": 100, "time_to_go": -1, "alarm_reason": 0})
    assert out["alarm_state"] == "OFF"
    assert out["alarm_state_desc"] == "No alarm"
    assert out["relay_state"] == "OFF"
    assert out["bmv_model"] == "700"
    assert out["firmware_version"] == "0307"
    assert out["product_id"] == "0203"


def test_a_charger_block_decodes_its_state_through_the_code_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[tuple[str, str]] = [("PID", "0xA042"), ("FW", "159"), ("V", "13250"), ("I", "4100"), ("VPV", "20000"),
                                      ("PPV", "54"), ("CS", "5"), ("MPPT", "2"), ("ERR", "0"), ("LOAD", "ON"), ("OR", "0x00000000")]
    transport: Any = make(monkeypatch)
    out: dict[str, Any] = transport._process_raw({0: records_of(make_block(records))})

    assert_values(out, {"battery_voltage": 13250, "battery_current": 4100, "charge_state": 5, "mppt_mode": 2, "error_code": 0})
    codes: dict[str, str] = cast(dict[str, str], protocol_settings("victron_vedirect_serial").codes["charge_state_codes"])

    assert out["charge_state_desc"] == codes["5"], "the numeric state is described from the protocol's own table"


def test_labels_the_protocol_does_not_know_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0: records_of(make_block([("V", "12000"), ("ZZZ", "1"), ("QQQ", "x")]))})
    assert list(out) == ["battery_voltage"]


def test_a_field_that_reads_as_dashes_is_left_out_but_the_rest_decodes(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0: records_of(make_block([("V", "12000"), ("SOC", "---")]))})
    assert out["battery_voltage"] == 12000
    assert "state_of_charge" not in out


def test_labels_are_matched_without_regard_to_case(monkeypatch: pytest.MonkeyPatch) -> None:
    out: dict[str, Any] = make(monkeypatch)._process_raw({0: records_of(make_block([("v", "12000")]))})
    assert out["battery_voltage"] == 12000


def test_nothing_is_decoded_without_a_block_or_a_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    assert transport._process_raw({}) == {}
    assert transport._process_raw({0: b""}) == {}
    transport.protocolSettings = None
    assert transport._process_raw({0: records_of(BMV_700_BLOCK)}) == {}


# --------------------------------------------------------------------------- #
# the whole stack: real client, real state machine, real protocol              #
# --------------------------------------------------------------------------- #

def test_a_full_poll_of_a_simulated_device(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = FakePort(chunks=[BMV_700_BLOCK])
    transport: Any = make(monkeypatch, port)

    out: dict[str, Any] = transport.read_data()

    assert port.written == [], "VE.Direct text is listen-only: nothing is ever sent"
    assert_values(out, {"battery_voltage": 26201, "state_of_charge": 100})
    assert out["bmv_model"] == "700"


def test_a_hex_message_after_the_block_does_not_disturb_the_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakePort(chunks=[BMV_700_BLOCK + HEX_MESSAGE]))
    assert_values(transport.read_data(), {"battery_voltage": 26201})


def test_two_polls_in_a_row_each_get_a_fresh_block(monkeypatch: pytest.MonkeyPatch) -> None:
    port: FakePort = FakePort(chunks=[BMV_700_BLOCK, BMV_700_BLOCK])
    transport: Any = make(monkeypatch, port)
    assert_values(transport.read_data(), {"battery_voltage": 26201})
    assert_values(transport.read_data(), {"battery_voltage": 26201})
    assert port.input_resets == 2


def test_a_silent_device_gives_an_empty_incomplete_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakePort())
    transport._start_cycle_tracking()
    assert transport.read_data() == {}
    assert transport.get_cycle_result().is_complete is False


def test_a_corrupted_block_is_dropped_not_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    damaged: bytearray = bytearray(BMV_700_BLOCK)
    damaged[20] ^= 0x01
    assert make(monkeypatch, FakePort(chunks=[bytes(damaged)])).read_data() == {}


def test_the_interleaved_read_takes_a_single_step(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakePort(chunks=[BMV_700_BLOCK]))
    assert list(transport.read_data_iter()) == [True]
    assert transport.get_partial_data()["battery_voltage"] == 26201


def test_a_port_that_is_not_there_yet_returns_nothing_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, FakePort(chunks=[BMV_700_BLOCK]))
    unplug(monkeypatch)
    assert transport.read_data() == {}


# --------------------------------------------------------------------------- #
# settings and structure                                                      #
# --------------------------------------------------------------------------- #

def test_the_baud_rate_defaults_to_19200(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch).baudrate == 19200
    assert make(monkeypatch, baudrate=9600).baudrate == 9600


def test_vedirect_has_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="0-0"):
        make(monkeypatch, address=1)


def test_the_timeout_setting_reaches_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch, timeout="2.5")
    transport.connect()
    assert transport._client.timeout == 2.5
    assert transport._client_timeout() == 2.5


def test_a_listen_only_read_has_a_single_pseudo_command(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(monkeypatch)._poll_commands() == [0]


def test_a_port_already_opened_for_another_framing_cannot_be_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    transport: Any = make(monkeypatch)
    foreign: Any = serial_length_client("/dev/ttyUSB0", 19200, b"\xdd", 3, 1, 4, 128)
    with pytest.raises(ValueError, match="cannot share"):
        transport._check_shared_client(foreign)
    with pytest.raises(TypeError, match="needs a vedirect_client"):
        transport._prepare_client(foreign)


def test_it_is_a_scraper_directly_on_serial_base(monkeypatch: pytest.MonkeyPatch) -> None:
    assert issubclass(serial_vedirect, serial_base)
    assert serial_vedirect.__bases__ == (serial_base,), "no framing family fits text blocks, so it stands on serial_base itself"
    assert "transport_type" in vars(serial_vedirect), "the web UI scanner reads it with ast; it cannot be inherited"
    assert serial_vedirect.transport_type == "scraper"
    assert make(monkeypatch).scrape_target != ""


def test_the_protocol_json_names_this_transport() -> None:
    meta: dict[str, Any] = json.loads((REPO / "protocols/victron/victron_vedirect_serial.json").read_text(encoding="utf-8"))
    assert meta["transport"] == "serial_vedirect"
    assert meta["register_key_type"] == "text"
