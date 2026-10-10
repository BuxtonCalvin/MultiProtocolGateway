# Description: Unit tests for the Home Assistant friendly behaviour of the MQTT bridge (discovery platforms, metadata inference, availability, HA restart handling).
# File: test_mqtt_homeassistant.py
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

"""Tests for the Home Assistant friendly behaviour of ``classes.transports.mqtt``."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from classes.ha_metadata import lookup_ha_unit, repair_mojibake

from classes.protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    registry_map_entry,
)
from classes.transports.mqtt import (
    is_numeric_entry,
    mqtt,
    number_bounds,
)
from classes.transports.transport_base import transport_base

if TYPE_CHECKING:
    from pytests.conftest import DummySettings

SERIAL = "SN1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _entry(
    name: str,
    *,
    reg_type: Registry_Type = Registry_Type.INPUT,
    unit: str = "",
    unit_mod: float = 1.0,
    data_type: Data_Type = Data_Type.USHORT,
    write_mode: WriteMode = WriteMode.READ,
    register: int = 1,
    **extra: Any,
) -> registry_map_entry:
    return registry_map_entry(
        reg_type, register, -1, -1, 0, name, name, "", unit, unit_mod, {}, False, [],  # noqa: FBT003
        [], data_type=data_type, write_mode=write_mode, **extra,
    )


def _bridge(
    dummy_settings: type[DummySettings], **settings: Any
) -> tuple[mqtt, MagicMock]:
    """A real mqtt bridge on a mocked paho client, with discovery throttle off."""
    client = MagicMock()
    client.publish.return_value = SimpleNamespace(rc=0)
    client.is_connected.return_value = True
    with patch("classes.transports.mqtt.MQTTClient", return_value=client):
        out = mqtt(dummy_settings(host="broker", username="u", password="p", **settings))  # noqa: S106
    out._DISCOVERY_THROTTLE = 0.0
    out._HA_REPUBLISH_SETTLE = 0.0
    return out, client


def _source(
    dummy_settings: type[DummySettings],
    entries: list[registry_map_entry],
    codes: dict[str, dict[str, str]] | None = None,
    write_enabled: bool = False,  # noqa: FBT001, FBT002
) -> transport_base:
    src = transport_base(dummy_settings(
        name="transport.src", device_serial_number=SERIAL, device_name="Inverter",
        device_manufacturer="EG4", device_model="18kPV",
    ))
    src.write_enabled = write_enabled
    ps = MagicMock()
    registry_map: dict[Registry_Type, list[registry_map_entry]] = {}
    for e in entries:
        registry_map.setdefault(e.registry_type, []).append(e)
    ps.registry_map = registry_map
    ps.get_registry_map.side_effect = lambda rt: registry_map.get(rt, [])
    ps.get_entry_code_dict.side_effect = lambda e: (codes or {}).get(e.variable_name, {})
    src.protocolSettings = ps
    return src


def _configs(client: MagicMock) -> dict[str, dict[str, Any]]:
    """Discovery config payloads published, keyed by '<component>/<name>'."""
    result: dict[str, dict[str, Any]] = {}
    for call in client.publish.call_args_list:
        topic, payload = call.args[0], call.args[1]
        if topic.startswith("homeassistant/") and topic.endswith("/config") and payload:
            parts = topic.split("/")
            result[f"{parts[1]}/{parts[3]}"] = json.loads(payload)
    return result


def _cleared(client: MagicMock) -> set[str]:
    """Discovery config topics cleared (empty retained payload)."""
    return {
        c.args[0] for c in client.publish.call_args_list
        if c.args[0].endswith("/config") and c.args[1] == ""
    }


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("V", ("V", "voltage", "measurement")),
        ("v", ("V", "voltage", "measurement")),
        ("kWH", ("kWh", "energy", "total_increasing")),
        ("k Wh", ("kWh", "energy", "total_increasing")),
        ("kwh", ("kWh", "energy", "total_increasing")),
        ("H Z", ("Hz", "frequency", "measurement")),
        ("C", ("°C", "temperature", "measurement")),
        ("celsius", ("°C", "temperature", "measurement")),
        ("hour", ("h", "duration", "")),
        ("%", ("%", "", "measurement")),
        ("", ("", "", "")),
    ],
)
def test_lookup_ha_unit_normalises_messy_units(raw: str, expected: tuple[str, str, str]) -> None:
    """Case, spacing and spelling variants found in real protocol CSVs map to one HA unit."""
    assert lookup_ha_unit(raw) == expected


def test_lookup_ha_unit_repairs_latin1_mojibake() -> None:
    """The CSV reader decodes as latin-1, so '°C' arrives as 'Â°C' and must be repaired."""
    assert repair_mojibake("Â°C") == "°C"
    assert lookup_ha_unit("Â°C") == ("°C", "temperature", "measurement")


def test_lookup_ha_unit_never_guesses_ambiguous_or_unknown_units() -> None:
    """Edge: unknown units keep their text but get no class; bare 'S' is never guessed."""
    assert lookup_ha_unit("S") == ("S", "", "")
    assert lookup_ha_unit("cycles") == ("cycles", "", "")


def test_is_numeric_entry() -> None:
    """Enum-mapped, flag and string types are text; ints, floats and bit fields are numeric."""
    assert is_numeric_entry(_entry("a", data_type=Data_Type.USHORT), has_code_map=False)
    assert is_numeric_entry(_entry("a", data_type=Data_Type.FLOAT32), has_code_map=False)
    assert is_numeric_entry(_entry("a", data_type=Data_Type._4BIT), has_code_map=False)
    assert not is_numeric_entry(_entry("a", data_type=Data_Type.USHORT), has_code_map=True)
    assert not is_numeric_entry(_entry("a", data_type=Data_Type.ASCII), has_code_map=False)
    assert not is_numeric_entry(_entry("a", data_type=Data_Type.BIT16_FLAGS), has_code_map=False)


def test_number_bounds_scale_by_unit_mod_and_never_use_ha_defaults() -> None:
    """A 0.1 A register spanning raw 0..1000 is 0..100 A with a 0.1 step."""
    entry = _entry("charge_current", unit_mod=0.1, value_min=0, value_max=1000)
    assert number_bounds(entry) == (0, 100, 0.1)
    # whole-number results lose the float noise / '.0'
    assert number_bounds(_entry("x", unit_mod=1.0, value_min=0, value_max=65535)) == (0, 65535, 1)
    # a negative scale factor swaps the bounds rather than producing min > max
    low, high, step = number_bounds(_entry("y", unit_mod=-1.0, value_min=0, value_max=10))
    assert (low, high, step) == (-10, 0, 1)


def test_number_bounds_limits_unsigned_bit_fields() -> None:
    """A 4-bit field can only hold 0..15 even though the CSV default range is 0..65535."""
    assert number_bounds(_entry("mode", data_type=Data_Type._4BIT)) == (0, 15, 1)


# ---------------------------------------------------------------------------
# discovery: platforms and metadata
# ---------------------------------------------------------------------------
def test_discovery_sensor_gets_unit_device_class_state_class_and_friendly_name(
    dummy_settings: type[DummySettings],
) -> None:
    """A read-only voltage becomes a measurement sensor with a normalised unit."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [_entry("vpv_1", unit="V", unit_mod=0.1)])

    out.mqtt_discovery(src)

    cfg = _configs(client)["sensor/vpv_1"]
    assert cfg["unit_of_measurement"] == "V"
    assert cfg["device_class"] == "voltage"
    assert cfg["state_class"] == "measurement"
    assert cfg["name"] == "Vpv 1"
    assert cfg["unique_id"] == "MPG_SN1_vpv_1"  # unchanged: renaming it would orphan HA entities
    assert cfg["state_topic"] == "home/device/sn1/vpv_1"


def test_discovery_energy_is_total_increasing(dummy_settings: type[DummySettings]) -> None:
    """Lifetime/daily energy counters must use total_increasing for the Energy dashboard."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [_entry("e_total", unit="kWH")])

    out.mqtt_discovery(src)

    cfg = _configs(client)["sensor/e_total"]
    assert (cfg["device_class"], cfg["state_class"], cfg["unit_of_measurement"]) == (
        "energy", "total_increasing", "kWh",
    )


def test_discovery_text_sensor_has_no_unit_or_state_class(dummy_settings: type[DummySettings]) -> None:
    """Enum-mapped values are text: a unit/state_class would make HA reject the state."""
    out, client = _bridge(dummy_settings)
    entry = _entry("state", unit="ASCII")
    src = _source(dummy_settings, [entry], codes={"state": {"0": "Standby", "1": "Grid"}})

    out.mqtt_discovery(src)

    cfg = _configs(client)["sensor/state"]
    for key in ("unit_of_measurement", "state_class", "device_class"):
        assert key not in cfg


def test_discovery_availability_requires_device_and_bridge_online(
    dummy_settings: type[DummySettings],
) -> None:
    """A crashed gateway (LWT -> bridge_status offline) must mark entities unavailable."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [_entry("soc", unit="%")])

    out.mqtt_discovery(src)

    cfg = _configs(client)["sensor/soc"]
    assert cfg["availability_mode"] == "all"
    assert cfg["availability"] == [
        {"topic": "home/device/sn1/availability"},
        {"topic": "home/device/bridge_status"},
    ]


def test_discovery_csv_overrides_win_and_none_suppresses(dummy_settings: type[DummySettings]) -> None:
    """Explicit CSV columns beat inference; 'none' suppresses an inferred value."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [
        _entry("soc", unit="%", ha_device_class="battery"),
        _entry("odd_volts", unit="V", ha_device_class="none", ha_state_class="none"),
        _entry("diag", unit="", ha_entity_category="diagnostic"),
        _entry("bad_cat", unit="", ha_entity_category="nonsense"),
    ])

    out.mqtt_discovery(src)

    cfgs = _configs(client)
    assert cfgs["sensor/soc"]["device_class"] == "battery"
    assert "device_class" not in cfgs["sensor/odd_volts"]
    assert "state_class" not in cfgs["sensor/odd_volts"]
    assert cfgs["sensor/diag"]["entity_category"] == "diagnostic"
    assert "entity_category" not in cfgs["sensor/bad_cat"]  # invalid values would make HA reject the entity


def test_discovery_config_category_not_applied_to_sensors(dummy_settings: type[DummySettings]) -> None:
    """HA only allows entity_category 'config' on controls; on a sensor it is dropped."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [_entry("ro", ha_entity_category="config")])

    out.mqtt_discovery(src)

    assert "entity_category" not in _configs(client)["sensor/ro"]


def test_discovery_expire_after_is_opt_in(dummy_settings: type[DummySettings]) -> None:
    """expire_after is absent by default and applied to sensors when configured."""
    out, client = _bridge(dummy_settings)
    out.mqtt_discovery(_source(dummy_settings, [_entry("soc", unit="%")]))
    assert "expire_after" not in _configs(client)["sensor/soc"]

    out, client = _bridge(dummy_settings, discovery_expire_after="120")
    out.mqtt_discovery(_source(dummy_settings, [_entry("soc", unit="%")]))
    assert _configs(client)["sensor/soc"]["expire_after"] == 120


def test_discovery_read_only_coil_and_discrete_become_binary_sensors(
    dummy_settings: type[DummySettings],
) -> None:
    """Boolean read-only registers are binary_sensors with 1/0 normalisation."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [
        _entry("fault", reg_type=Registry_Type.DISCRETE),
        _entry("relay", reg_type=Registry_Type.COIL),
    ])

    out.mqtt_discovery(src)

    cfgs = _configs(client)
    for name in ("fault", "relay"):
        cfg = cfgs[f"binary_sensor/{name}"]
        assert (cfg["payload_on"], cfg["payload_off"]) == ("1", "0")
        assert "'true'" in cfg["value_template"]  # tolerates True/False as well as 1/0


# ---------------------------------------------------------------------------
# discovery: controls (gated by the write allowlist)
# ---------------------------------------------------------------------------
def _enable_write(out: mqtt, src: transport_base, allow: set[str]) -> None:
    out._load_writable_allowlist = MagicMock(return_value=allow)  # type: ignore[method-assign]
    src.write_enabled = True
    out.init_bridge(src)


def test_allowlisted_holding_register_becomes_number_with_scaled_bounds(
    dummy_settings: type[DummySettings],
) -> None:
    """A write-enabled numeric holding register is a number wired to the existing /write topic."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry(
        "charge_current", reg_type=Registry_Type.HOLDING, unit="A", unit_mod=0.1,
        write_mode=WriteMode.WRITE, value_min=0, value_max=1000,
    )
    src = _source(dummy_settings, [entry])

    _enable_write(out, src, {"charge_current"})

    cfgs = _configs(client)
    assert "sensor/charge_current" not in cfgs
    cfg = cfgs["number/charge_current"]
    assert cfg["command_topic"] == "home/device/sn1/charge_current/write"
    assert cfg["command_topic"] in out._write_topics  # advertised topic is the subscribed one
    assert (cfg["min"], cfg["max"], cfg["step"], cfg["mode"]) == (0, 100, 0.1, "box")
    assert (cfg["unit_of_measurement"], cfg["device_class"]) == ("A", "current")
    assert "state_class" not in cfg  # not valid for number
    # the old read-only sensor is cleared so HA does not keep a duplicate
    assert "homeassistant/sensor/HN-SN1/charge_current/config" in _cleared(client)


def test_writable_but_not_allowlisted_stays_a_read_only_sensor(
    dummy_settings: type[DummySettings],
) -> None:
    """Discovery must never advertise a control the write gates would reject."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry("charge_current", reg_type=Registry_Type.HOLDING, write_mode=WriteMode.WRITE)
    src = _source(dummy_settings, [entry])

    _enable_write(out, src, set())  # nothing allowlisted

    cfgs = _configs(client)
    assert "sensor/charge_current" in cfgs
    assert "number/charge_current" not in cfgs
    assert "command_topic" not in cfgs["sensor/charge_current"]


def test_enum_holding_register_becomes_select_with_labels(dummy_settings: type[DummySettings]) -> None:
    """Mapped values become a select; options are the labels the write path accepts."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry("work_mode", reg_type=Registry_Type.HOLDING, write_mode=WriteMode.WRITE)
    src = _source(dummy_settings, [entry], codes={"work_mode": {"0": "Self use", "1": "Grid first"}})

    _enable_write(out, src, {"work_mode"})

    cfg = _configs(client)["select/work_mode"]
    assert cfg["options"] == ["Self use", "Grid first"]
    assert cfg["command_topic"] == "home/device/sn1/work_mode/write"
    assert "unit_of_measurement" not in cfg


def test_allowlisted_coil_becomes_switch(dummy_settings: type[DummySettings]) -> None:
    """Coils are switches that send 1/0 (the coil write path treats 0/false/off as False)."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry("grid_charge", reg_type=Registry_Type.COIL, write_mode=WriteMode.WRITE)
    src = _source(dummy_settings, [entry])

    _enable_write(out, src, {"grid_charge"})

    cfg = _configs(client)["switch/grid_charge"]
    assert (cfg["payload_on"], cfg["payload_off"], cfg["state_on"], cfg["state_off"]) == ("1", "0", "1", "0")
    assert cfg["command_topic"] == "home/device/sn1/grid_charge/write"


def test_write_only_control_is_optimistic_without_state_topic(dummy_settings: type[DummySettings]) -> None:
    """A write-only register cannot be read back, so HA must not wait for a state."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry("quick_charge", reg_type=Registry_Type.COIL, write_mode=WriteMode.WRITEONLY)
    src = _source(dummy_settings, [entry])

    _enable_write(out, src, {"quick_charge"})

    cfg = _configs(client)["switch/quick_charge"]
    assert cfg["optimistic"] is True
    assert "state_topic" not in cfg


def test_write_only_without_allowlist_publishes_nothing(dummy_settings: type[DummySettings]) -> None:
    """Nothing to read and nothing allowed to write -> no entity, and stale configs are cleared."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    entry = _entry("quick_charge", reg_type=Registry_Type.HOLDING, write_mode=WriteMode.WRITEONLY)
    src = _source(dummy_settings, [entry])

    _enable_write(out, src, set())

    assert "sensor/quick_charge" not in _configs(client)
    assert "homeassistant/sensor/HN-SN1/quick_charge/config" in _cleared(client)


def test_sensor_mode_keeps_legacy_behaviour_and_clears_controls(dummy_settings: type[DummySettings]) -> None:
    """discovery_entity_types = sensor: everything is a sensor again; control configs are cleared."""
    out, client = _bridge(dummy_settings, discovery_enabled="true", discovery_entity_types="sensor")
    entry = _entry("charge_current", reg_type=Registry_Type.HOLDING, unit="A", write_mode=WriteMode.WRITE)
    coil = _entry("fault", reg_type=Registry_Type.DISCRETE)
    src = _source(dummy_settings, [entry, coil])

    _enable_write(out, src, {"charge_current"})

    cfgs = _configs(client)
    assert {k for k in cfgs if k.startswith("sensor/")} == {"sensor/charge_current", "sensor/fault"}
    assert not any(k.split("/")[0] in ("number", "select", "switch", "binary_sensor") for k in cfgs)
    assert "command_topic" not in cfgs["sensor/charge_current"]
    cleared = _cleared(client)
    assert "homeassistant/number/HN-SN1/charge_current/config" in cleared
    assert "homeassistant/binary_sensor/HN-SN1/fault/config" in cleared


def test_unknown_entity_types_value_falls_back_to_auto(dummy_settings: type[DummySettings]) -> None:
    """A typo in the new setting must not silently disable discovery."""
    with pytest.warns(RuntimeWarning, match="discovery_entity_types"):
        out, _ = _bridge(dummy_settings, discovery_entity_types="banana")
    assert out.discovery_entity_types == "auto"


# ---------------------------------------------------------------------------
# JSON mode / topic consistency
# ---------------------------------------------------------------------------
def test_json_mode_discovery_uses_value_template(dummy_settings: type[DummySettings]) -> None:
    """In json mode there are no per-metric topics, so entities read one key of the blob."""
    out, client = _bridge(dummy_settings, json="true")
    src = _source(dummy_settings, [_entry("soc", unit="%")])
    out.mqtt_discovery(src)

    cfg = _configs(client)["sensor/soc"]
    assert cfg["value_template"] == "{{ value_json['soc'] }}"

    out.write_data({"soc": 87}, src)
    blob_topics = {c.args[0] for c in client.publish.call_args_list if c.args[1].lstrip().startswith("{")}
    assert cfg["state_topic"] in blob_topics  # exactly where write_data publishes the blob


def test_discovery_state_topic_matches_what_write_data_publishes(dummy_settings: type[DummySettings]) -> None:
    """Regression: with registry-type prefixes / mixed-case base topics discovery used a different topic."""
    out, client = _bridge(dummy_settings, base_topic="Home/Solar", input_register_prefix="Input")
    entry = _entry("vbat", unit="V")
    src = _source(dummy_settings, [entry])
    out.init_bridge(src)  # builds the variable -> registry-type lookup used by write_data
    out.mqtt_discovery(src)

    out.write_data({"vbat": 52.4}, src)

    state_topic = _configs(client)["sensor/vbat"]["state_topic"]
    published = {c.args[0] for c in client.publish.call_args_list}
    assert state_topic == "home/solar/sn1/input/vbat"
    assert state_topic in published


def test_discovery_skips_duplicate_variable_names(dummy_settings: type[DummySettings]) -> None:
    """Two registry types sharing a name would share one unique_id; only the first is published."""
    out, client = _bridge(dummy_settings)
    src = _source(dummy_settings, [
        _entry("power", reg_type=Registry_Type.INPUT, unit="W"),
        _entry("power", reg_type=Registry_Type.HOLDING, unit="kW"),
    ])

    out.mqtt_discovery(src)

    configs = [c for c in client.publish.call_args_list if c.args[0].endswith("/power/config")]
    assert len(configs) == 1


# ---------------------------------------------------------------------------
# write topics for several devices
# ---------------------------------------------------------------------------
def test_second_device_does_not_drop_first_devices_write_topics(dummy_settings: type[DummySettings]) -> None:
    """Regression: init_bridge used to wipe every device's write topics, so commands for the first were ignored."""
    out, _ = _bridge(dummy_settings)
    out._load_writable_allowlist = MagicMock(return_value={"limit"})  # type: ignore[method-assign]
    sources = []
    for serial in ("AAA", "BBB"):
        src = transport_base(dummy_settings(name=f"transport.{serial}", device_serial_number=serial))
        src.write_enabled = True
        ps = MagicMock()
        e = _entry("limit", reg_type=Registry_Type.HOLDING, write_mode=WriteMode.WRITE)
        ps.get_registry_map.side_effect = lambda rt, e=e: [e] if rt == Registry_Type.HOLDING else []
        src.protocolSettings = ps
        sources.append(src)

    for src in sources:
        out.init_bridge(src)

    assert set(out._write_topics) == {"home/device/aaa/limit/write", "home/device/bbb/limit/write"}
    # re-running one device's init (e.g. after a reload) is still idempotent
    out.init_bridge(sources[0])
    assert set(out._write_topics) == {"home/device/aaa/limit/write", "home/device/bbb/limit/write"}


# ---------------------------------------------------------------------------
# Home Assistant restart handling
# ---------------------------------------------------------------------------
def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_on_connect_subscribes_to_ha_status_only_when_discovery_enabled(
    dummy_settings: type[DummySettings],
) -> None:
    """The HA birth topic is only subscribed when discovery is on."""
    out, client = _bridge(dummy_settings, discovery_enabled="true", discovery_topic="ha")
    out._subscribe_ha_status()
    client.subscribe.assert_called_with("ha/status")

    out2, client2 = _bridge(dummy_settings)
    out2._subscribe_ha_status()
    client2.subscribe.assert_not_called()


def test_ha_online_republishes_discovery_and_replays_last_values(dummy_settings: type[DummySettings]) -> None:
    """After HA restarts, entities must reappear with values — not sit at 'unknown'."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    src = _source(dummy_settings, [_entry("soc", unit="%")])
    out.init_bridge(src)
    out.write_data({"soc": 87}, src)
    client.publish.reset_mock()

    out.client_on_message(client, None, SimpleNamespace(topic="homeassistant/status", payload=b"online"))

    assert _wait_for(lambda: any(c.args[:2] == ("home/device/sn1/soc", "87") for c in client.publish.call_args_list))
    assert "sensor/soc" in _configs(client)  # discovery re-announced first


def test_ha_status_ignores_offline_and_debounces_repeats(dummy_settings: type[DummySettings]) -> None:
    """'offline' is ignored and a burst of 'online' messages triggers one republish."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    out._republish_for_ha = MagicMock()  # type: ignore[method-assign]

    out._handle_ha_status("offline")
    assert not _wait_for(lambda: out._republish_for_ha.called, timeout=0.2)

    out._handle_ha_status("online")
    out._handle_ha_status("online")
    assert _wait_for(lambda: out._republish_for_ha.call_count >= 1)
    time.sleep(0.1)
    assert out._republish_for_ha.call_count == 1


def test_ha_status_message_is_not_treated_as_unknown_write_topic(dummy_settings: type[DummySettings]) -> None:
    """The HA birth message must not hit the 'not in _write_topics' warning path."""
    out, client = _bridge(dummy_settings, discovery_enabled="true")
    out._log = MagicMock()
    out.client_on_message(client, None, SimpleNamespace(topic="homeassistant/status", payload=b"offline"))
    out._log.warning.assert_not_called()


def test_health_snapshot_reports_discovery_settings_and_entity_count(dummy_settings: type[DummySettings]) -> None:
    """The Bridge Health panel can show how many HA entities were announced."""
    out, _ = _bridge(dummy_settings, discovery_enabled="true")
    out.mqtt_discovery(_source(dummy_settings, [_entry("a"), _entry("b")]))

    snap = out.get_health_snapshot()

    assert snap["discovery_entity_types"] == "auto"
    assert snap["discovery_entity_count"] == 2


# ---------------------------------------------------------------------------
# end to end: protocol CSV/JSON files -> real parser -> discovery
# ---------------------------------------------------------------------------
def test_csv_columns_flow_from_protocol_files_into_discovery(
    dummy_settings: type[DummySettings], tmp_path: Any
) -> None:
    """The optional 'ha ...' CSV columns survive the real parser and drive discovery output."""
    from classes.protocol_settings import protocol_settings

    proto_dir = tmp_path / "acme"
    proto_dir.mkdir()
    (proto_dir / "acme_x.json").write_text(
        json.dumps({"transport": "modbus_tcp", "work_mode_codes": {"0": "Self use", "1": "Grid first"}})
    )
    (proto_dir / "acme_x.input_registry_map.csv").write_text(
        "register,variable_name,documented_name,unit,data_type,values,ha_device_class,ha_state_class,ha_entity_category\n"
        "1,vpv1,Vpv1,0.1V,,0-65535,,,\n"
        "2,soc,Soc,%,,0-100,battery,,\n"
        "3,fw,Firmware,ASCII,ASCII,0-65535,,none,diagnostic\n"
    )
    (proto_dir / "acme_x.holding_registry_map.csv").write_text(
        "register,variable_name,documented_name,unit,data_type,values,writable\n"
        "10,work_mode,Work Mode,,,0-1,RW\n"
        "11,charge_limit,Charge Limit,0.1A,,0-1000,RW\n"
    )

    out, client = _bridge(dummy_settings, discovery_enabled="true")
    src = transport_base(dummy_settings(
        name="transport.src", device_serial_number=SERIAL, device_name="Inverter",
        device_manufacturer="Acme", device_model="X",
    ))
    src.protocolSettings = protocol_settings("acme_x", settings_dir=str(tmp_path))
    _enable_write(out, src, {"work_mode", "charge_limit"})

    cfgs = _configs(client)
    assert (cfgs["sensor/vpv1"]["unit_of_measurement"], cfgs["sensor/vpv1"]["device_class"]) == ("V", "voltage")
    assert cfgs["sensor/soc"]["device_class"] == "battery"
    assert cfgs["sensor/fw"]["entity_category"] == "diagnostic"
    assert "state_class" not in cfgs["sensor/fw"]
    assert cfgs["select/work_mode"]["options"] == ["Self use", "Grid first"]
    number = cfgs["number/charge_limit"]
    assert (number["min"], number["max"], number["step"]) == (0, 100, 0.1)
