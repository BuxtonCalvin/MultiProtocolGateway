# Description: Unit tests for transport classes with external systems mocked.
# File: test_transports.py
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

"""Unit tests for transport classes with external systems mocked."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch  # noqa: F401

import pytest

from classes.protocol_settings import Registry_Type, WriteMode, registry_map_entry
from classes.transports import modbus_base as modbus_base_module
from classes.transports.canbus import canbus
from classes.transports.influxdb_out import influxdb_out
from classes.transports.json_out import json_out
from classes.transports.modbus_base import (
    RegisterFailureTracker,
    interpret_modbus_exception_code,
)
from classes.transports.mqtt import mqtt
from classes.transports.serial_frame_client import serial_frame_client
from classes.transports.serial_frame_transport import serial_frame_transport
from classes.transports.transport_base import TransportWriteMode, transport_base


def test_transport_base_tracks_cycle_and_emits_messages(dummy_settings) -> None:
    """Happy path: base transport tracks read-cycle completeness and calls the message callback."""
    transport = transport_base(dummy_settings(device_serial_number="ABC", device_manufacturer="EG4"))
    entry = MagicMock()
    callback = MagicMock()
    transport.on_message = callback

    transport._start_cycle_tracking()
    transport._cycle_expect_unit(2)
    transport._cycle_mark_unit_complete()
    transport._cycle_mark_incomplete()
    transport._finish_cycle_tracking({"soc": 99})
    transport._emit_message(entry, 99)

    result = transport.get_cycle_result()
    assert result.has_data is True
    assert result.is_complete is False
    assert result.expected_units == 2
    callback.assert_called_once_with(transport, entry, 99)


def test_transport_write_mode_maps_edge_values() -> None:
    """Edge cases: TransportWriteMode maps empty and unknown strings to READ."""
    assert TransportWriteMode.fromString("yes") is TransportWriteMode.WRITE
    assert TransportWriteMode.fromString("unsafe") is TransportWriteMode.UNSAFE
    assert TransportWriteMode.fromString("") is TransportWriteMode.READ
    assert TransportWriteMode.fromString("surprise") is TransportWriteMode.READ


def test_json_out_writes_structured_payload_to_mock_file(dummy_settings) -> None:
    """Mocks filesystem I/O: json_out writes device metadata, timestamp, and data as JSON."""
    settings = dummy_settings(
        output_file="output/test.json",
        pretty_print="false",
        include_timestamp="false",
        include_device_info="true",
    )
    out = json_out(settings)
    out.connected = True
    handle = MagicMock()
    out.file_handle = handle
    source = transport_base(dummy_settings(name="transport.src", device_serial_number="SN1", device_name="inverter"))

    out.write_data({"voltage": 52.5}, source)

    written = handle.write.call_args.args[0].strip()
    payload = json.loads(written)
    assert payload["data"] == {"voltage": 52.5}
    assert payload["device"]["serial_number"] == "SN1"


@patch("classes.transports.mqtt.MQTTClient")
def test_mqtt_requires_host_and_publishes_values(mock_mqtt_client: MagicMock, dummy_settings) -> None:
    """Happy path and error handling: MQTT requires a host and publishes scalar values via a mocked client."""
    with pytest.raises(ValueError, match="Host"):
        mqtt(dummy_settings(host=""))

    publish_info = SimpleNamespace(rc=0)
    client = MagicMock()
    client.publish.return_value = publish_info
    client.is_connected.return_value = True
    mock_mqtt_client.return_value = client
    out = mqtt(dummy_settings(host="broker", username="user", password="pw"))  # noqa: S106
    out.connected = True
    source = transport_base(dummy_settings(name="transport.src", device_serial_number="SN1"))

    out.write_data({"Voltage": 52.567}, source)

    client.publish.assert_any_call("home/device/sn1/voltage", "52.57")


def test_mqtt_init_bridge_subscribes_only_allowlisted_write_topics(dummy_settings) -> None:
    """Edge case: MQTT write topics are subscribed only for writable registers present in the override allowlist."""
    out = mqtt.__new__(mqtt)
    out.client = MagicMock()
    out.base_topic = "base"
    out.discovery_enabled = False
    out._log = MagicMock()
    out._load_override_write_allowlist = MagicMock(return_value={"charge_limit"})
    source = transport_base(dummy_settings(name="transport.src", device_serial_number="SN1"))
    source.write_enabled = True
    writable = registry_map_entry(
        Registry_Type.HOLDING, 10, -1, -1, 0, "charge_limit", "charge_limit", "", "", 1.0, {}, False, [],  # noqa: FBT003
        [], write_mode=WriteMode.WRITE
    )
    blocked = registry_map_entry(
        Registry_Type.HOLDING, 11, -1, -1, 0, "other", "other", "", "", 1.0, {}, False, [],  # noqa: FBT003
        [], write_mode=WriteMode.WRITE
    )
    source.protocolSettings = MagicMock()
    source.protocolSettings.get_registry_map.return_value = [writable, blocked]

    out.init_bridge(source)

    out.client.subscribe.assert_called_once_with("base/sn1/write/charge_limit")


def test_register_failure_tracker_disables_then_resets() -> None:
    """Happy path: repeated failures disable a range and success clears the disabled state."""
    tracker = RegisterFailureTracker((1, 2), Registry_Type.INPUT)
    assert tracker.record_failure(max_failures=2, disable_duration_hours=1) is False
    assert tracker.record_failure(max_failures=2, disable_duration_hours=1) is True
    assert tracker.is_disabled() is True
    tracker.record_success()
    assert tracker.is_disabled() is False
    assert tracker.failure_count == 0


def test_modbus_helpers_interpret_exceptions_and_validate_client_presence(dummy_settings) -> None:
    """Error handling: Modbus helpers describe exception codes and require an initialized client."""
    assert "Modbus Exception" in interpret_modbus_exception_code(0x83)
    base = modbus_base_module.modbus_base.__new__(modbus_base_module.modbus_base)
    base.client = None
    base.transport_name = "transport.modbus"
    with pytest.raises(RuntimeError, match="no client"):
        base._get_correct_device_arg({"unit": 1})


def test_modbus_base_register_word_helpers_handle_little_endian() -> None:
    """Edge case: register/byte helper methods preserve expected word order for little endian writes."""
    base = modbus_base_module.modbus_base.__new__(modbus_base_module.modbus_base)
    assert base._register_words_to_bytes([0x1122, 0x3344], "big") == b"\x11\x22\x33\x44"
    assert base._register_words_to_bytes([0x1122, 0x3344], "little") == b"\x33\x44\x11\x22"
    assert base._bytes_to_register_words(b"\x11\x22\x33\x44", "little") == [0x3344, 0x1122]
    with pytest.raises(ValueError, match="even byte"):
        base._bytes_to_register_words(b"\x01", "big")


@patch("classes.transports.influxdb_out.InfluxDBClient")
def test_influxdb_out_connect_and_builds_points_with_mock_client(mock_client_cls: MagicMock, dummy_settings) -> None:
    """Mocks database API: InfluxDB output connects, creates missing DBs, and builds typed points."""
    client = MagicMock()
    client.get_list_database.return_value = []
    mock_client_cls.return_value = client
    out = influxdb_out(dummy_settings(enable_persistent_storage="false", batch_size=1))
    out.connect()
    source = transport_base(dummy_settings(name="transport.src", device_serial_number="SN1"))

    out.write_data({"soc": "99", "status": "online"}, source)

    client.create_database.assert_called_once_with("solar")
    client.write_points.assert_called_once()
    fields = client.write_points.call_args.args[0][0]["fields"]
    assert fields == {"soc": 99.0, "status": "online"}


@patch("classes.transports.serial_frame_client.serial.Serial")
def test_serial_frame_client_reads_and_writes_framed_data(mock_serial_cls: MagicMock) -> None:
    """Mocks serial I/O: serial_frame_client frames writes and extracts complete reads."""
    serial = MagicMock()
    serial.read.side_effect = [b"x", b"<", b"O", b"K", b">"]
    mock_serial_cls.return_value = serial
    client = serial_frame_client("COM1", 9600, b"<", b">")

    assert client.read() == b"OK"
    client.write(b"PING")
    serial.write.assert_called_once_with(b"<PING>")


def test_serial_frame_transport_marker_parsing_and_raw_decode(dummy_settings) -> None:
    """Happy path: serial_frame_transport parses hex/literal markers and returns raw hex without a protocol map."""
    assert serial_frame_transport._parse_frame_marker("0x7E") == b"~"
    assert serial_frame_transport._parse_frame_marker("END") == b"END"
    transport = serial_frame_transport.__new__(serial_frame_transport)
    transport.protocolSettings = None
    assert transport._decode_frame(b"\x01\x02") == {"raw_frame": "0102"}


def test_canbus_serial_number_scoring_and_cache_cleanup(dummy_settings) -> None:
    """Happy path and edge case: CAN serial-number heuristics score ASCII payloads and drop stale cache."""
    instance = canbus.__new__(canbus)
    instance._SN_ASCII_RATIO = canbus._SN_ASCII_RATIO
    instance._SN_MIN_ALNUM = canbus._SN_MIN_ALNUM
    instance._log = MagicMock()
    score, decoded = instance._sn_score_frame(0x351, b"SN123456")
    assert score > 0
    assert decoded == "SN123456"

    import threading
    from collections import OrderedDict

    instance.lock = threading.Lock()
    instance.cacheTimeout = 1
    instance.cache = OrderedDict({1: (b"old", time.time() - 99), 2: (b"new", time.time())})
    instance.clean_cache()
    assert list(instance.cache.keys()) == [2]
