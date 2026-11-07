# Description: Tests for the enphase_rest scraper transport (REST key parsing, JSON extraction, value encoding, token handling and polling against a local fake IQ Gateway).
# File: test_enphase_rest.py
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
# ruff: noqa: S105, S106
# The passwords and tokens below are fixtures for the fake gateway.
# These tests deliberately exercise protected members (_log, _get_json, _prepare_auth, ...).

"""
Unit tests for classes.transports.enphase_rest (no gateway required).

The pure helpers (key parsing, JSON extraction, value encoding, JWT expiry) are tested directly.
The transport itself is tested against a real HTTP server on the loopback interface that plays the
IQ Gateway, so the whole path (request, JSON, encoding, protocol_settings decode) is exercised
without mocking requests. Only the Enlighten cloud calls are patched.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import DummySettings
from requests.auth import HTTPDigestAuth

from classes.protocol_settings import Data_Type
from classes.transports import enphase_rest as module
from classes.transports.enphase_rest import (
    RestKey,
    encode_value,
    enphase_rest,
    extract_value,
    jwt_expiry,
    walk_path,
)
from classes.transports.transport_base import transport_base
from classes.WebServer import transport_registry as tr
from classes.WebServer.scanner import _classify_transport  # pyright: ignore[reportPrivateUsage]

REPO: Path = Path(__file__).resolve().parent.parent
PROTOCOL: str = "enphase_iq_gateway_rest"

LIVEDATA: dict[str, Any] = {
    "connection": {"mqtt_state": "connected", "prov_state": "configured", "sc_stream": "enabled"},
    "meters": {
        "last_update": 1700000000,
        "soc": 85,
        "pv": {"agg_p_mw": 3420500},
        "storage": {"agg_p_mw": -500000},
        "grid": {"agg_p_mw": -1250500},
        "load": {"agg_p_mw": 1670000},
    },
}
PRODUCTION: dict[str, Any] = {"wattHoursToday": 8456, "wattHoursSevenDays": 45230, "wattHoursLifetime": 3456789, "wattsNow": 3420}
ENSEMBLE: dict[str, Any] = {
    "devices:": [
        {"serial_num": "1", "temperature": 28, "soc": 85, "encharge_capacity": 3360},
        {"serial_num": "2", "temperature": 31, "soc": 80, "encharge_capacity": 3360},
    ]
}
INVERTERS: list[dict[str, Any]] = [
    {"serialNumber": "482112345678", "lastReportWatts": 285, "maxReportWatts": 349},
    {"serialNumber": "482112345679", "lastReportWatts": 275, "maxReportWatts": 340},
]


def approx(expected: float, *, abs: float | None = None) -> float:  # noqa: A002 - mirrors pytest.approx
    """pytest.approx with a concrete return type; its own signature is partly unannotated, which strict type checking rejects."""
    return cast("float", pytest.approx(expected, abs=abs))  # pyright: ignore[reportUnknownMemberType]


def make_jwt(expires_in: float) -> str:
    """A syntactically valid, unsigned JWT that expires ``expires_in`` seconds from now."""
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': time.time() + expires_in})}.signature"


# --------------------------------------------------------------------------- #
# fake IQ Gateway                                                              #
# --------------------------------------------------------------------------- #

class FakeGateway:
    """A loopback HTTP server that answers like an IQ Gateway and records what it was asked."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, str]] = {
            "/api/v1/production": (200, json.dumps(PRODUCTION)),
            "/ivp/livedata/status": (200, json.dumps(LIVEDATA)),
            "/ivp/ensemble/power": (200, json.dumps(ENSEMBLE)),
            "/api/v1/production/inverters": (200, json.dumps(INVERTERS)),
            "/info": (200, "<envoy_info><device><sn>122112345678</sn><software>D7.6.175</software></device></envoy_info>"),
        }
        self.requests: list[tuple[str, str]] = []
        ''' (path, Authorization header) of every request, in order '''
        self.required_token: str | None = None
        ''' when set, any other bearer token gets a 401 '''
        gateway: FakeGateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                auth: str = self.headers.get("Authorization", "")
                gateway.requests.append((self.path, auth))
                if gateway.required_token is not None and self.path != "/info" and auth != f"Bearer {gateway.required_token}":
                    status, body = 401, "Unauthorized"
                else:
                    status, body = gateway.responses.get(self.path, (404, "not found"))
                payload: bytes = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server API
                pass

        self.server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = int(self.server.server_address[1])
        self.thread: threading.Thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def count(self, path: str) -> int:
        return sum(1 for requested, _ in self.requests if requested == path)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    fake: FakeGateway = FakeGateway()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def isolated_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the connection-state setter away from the repo's backlogs folder and from real notifications."""
    monkeypatch.setattr(transport_base, "_disconnect_marker_file", lambda self: tmp_path / "marker") # type: ignore


def make_transport(gateway: FakeGateway | None = None, **values: Any) -> enphase_rest:
    """Construct an enphase_rest against the fake gateway, with a valid token and no request delay."""
    merged: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": gateway.port if gateway is not None else 1,
        "use_https": False,
        "protocol_version": PROTOCOL,
        "token": make_jwt(180 * 86400),
        "request_delay": 0,
        "timeout": 2,
        "read_interval": 0,          # every row is due every cycle unless the map gives it a slower interval
        **values,
    }
    transport: enphase_rest = enphase_rest(DummySettings(**merged))  # type: ignore[arg-type]
    transport._log = MagicMock()
    return transport


def log_of(transport: enphase_rest) -> MagicMock:
    return cast(MagicMock, transport._log)


# --------------------------------------------------------------------------- #
# RestKey                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("text", "endpoint", "path", "aggregate"),
    [
        ("/api/v1/production#wattsNow", "/api/v1/production", ("wattsNow",), ""),
        ("/ivp/livedata/status#meters.pv.agg_p_mw", "/ivp/livedata/status", ("meters", "pv", "agg_p_mw"), ""),
        ("/ivp/ensemble/power#devices:.*.soc|avg", "/ivp/ensemble/power", ("devices:", "*", "soc"), "avg"),
        ("/api/v1/production/inverters#|count", "/api/v1/production/inverters", (), "count"),
        ("/api/v1/production/inverters#*.lastReportWatts|SUM", "/api/v1/production/inverters", ("*", "lastReportWatts"), "sum"),
        ("/ivp/meters/readings#0.activePower", "/ivp/meters/readings", ("0", "activePower"), ""),
        ("/ivp/meters/readings#-1.voltage", "/ivp/meters/readings", ("-1", "voltage"), ""),
        ("/info", "/info", (), ""),
        ("  /api/v1/production # wattsNow  ", "/api/v1/production", ("wattsNow",), ""),
    ],
)
def test_rest_key_parse(text: str, endpoint: str, path: tuple[str, ...], aggregate: str) -> None:
    assert RestKey.parse(text) == RestKey(endpoint=endpoint, path=path, aggregate=aggregate)


@pytest.mark.parametrize(
    "text",
    [
        "api/v1/production#wattsNow",       # no leading slash
        "",                                  # empty
        "#wattsNow",                         # no endpoint
        "/x#a..b",                           # empty segment
        "/x#a.|sum",                         # trailing empty segment
        "/x#a|median",                       # unknown aggregate
        "/x#*.watts",                        # wildcard without aggregate
    ],
)
def test_rest_key_parse_rejects_malformed_keys(text: str) -> None:
    with pytest.raises(ValueError, match="REST key"):
        RestKey.parse(text)


# --------------------------------------------------------------------------- #
# walk_path / extract_value                                                    #
# --------------------------------------------------------------------------- #

def doc(text: str) -> Any:
    return json.loads(text)


def value_of(document: Any, key: str) -> Any:
    return extract_value(document, RestKey.parse("/x#" + key))


def test_walk_path_object_and_index() -> None:
    document: Any = {"a": {"b": [10, 20, 30]}}
    assert walk_path(document, ("a", "b", "1")) == [20]
    assert walk_path(document, ("a", "b", "-1")) == [30]
    assert walk_path(document, ("a", "b", "3")) == []
    assert walk_path(document, ("a", "b", "x")) == []
    assert walk_path(document, ("a", "missing")) == []
    assert walk_path(document, ("a", "b", "0", "deeper")) == []   # stepping into a scalar


def test_walk_path_wildcard_over_list_and_object() -> None:
    assert walk_path([{"w": 1}, {"w": 2}, {"x": 3}], ("*", "w")) == [1, 2]
    assert walk_path({"a": {"w": 1}, "b": {"w": 2}}, ("*", "w")) == [1, 2]
    assert walk_path(5, ("*",)) == []


def test_extract_scalar_and_missing() -> None:
    document: Any = {"meters": {"pv": {"agg_p_mw": 3420500}}, "name": "x"}
    assert value_of(document, "meters.pv.agg_p_mw") == 3420500
    assert value_of(document, "meters.storage.agg_p_mw") is None      # no battery
    assert value_of(document, "name") == "x"


def test_extract_numeric_aggregates() -> None:
    document: Any = [{"w": 285}, {"w": 275}, {"w": 300}]
    assert value_of(document, "*.w|sum") == 860
    assert value_of(document, "*.w|min") == 275
    assert value_of(document, "*.w|max") == 300
    assert value_of(document, "*.w|avg") == approx(860 / 3)
    assert value_of(document, "*.w|count") == 3


def test_extract_aggregate_ignores_non_numbers_and_booleans() -> None:
    document: Any = [{"w": 1}, {"w": "n/a"}, {"w": True}, {"w": None}, {"w": 4}]
    assert value_of(document, "*.w|sum") == 5
    assert value_of(document, "*.w|count") == 4     # only null is not counted


def test_extract_aggregate_of_nothing() -> None:
    assert value_of([], "*.w|sum") == 0
    assert value_of([], "*.w|count") == 0
    assert value_of([], "*.w|avg") is None
    assert value_of([], "*.w|min") is None
    assert value_of({}, "devices:.*.soc|max") is None     # absent list: no battery, no state of charge


def test_extract_aggregate_on_a_collection_path() -> None:
    assert extract_value([{"a": 1}, {"a": 2}], RestKey.parse("/x#|count")) == 2
    assert value_of({"devices:": [1, 2, 3]}, "devices:|count") == 3
    assert value_of({"devices:": [1, 2, 3]}, "devices:|sum") == 6
    assert value_of({"devices:": {"a": 4, "b": 5}}, "devices:|sum") == 9


# --------------------------------------------------------------------------- #
# encode_value                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("data_type", "value", "expected"),
    [
        (Data_Type.USHORT, 3420, (3420).to_bytes(2, "big")),
        (Data_Type.USHORT, 3420.4, (3420).to_bytes(2, "big")),       # whole-number types round
        (Data_Type.SHORT, -5, (-5).to_bytes(2, "big", signed=True)),
        (Data_Type.UINT, 8456, (8456).to_bytes(4, "big")),
        (Data_Type.INT, -500000, (-500000).to_bytes(4, "big", signed=True)),
        (Data_Type.ACC32, 7, (7).to_bytes(4, "big")),
        (Data_Type.UINT64, 3456789, (3456789).to_bytes(8, "big")),
        (Data_Type.INT64, -3, (-3).to_bytes(8, "big", signed=True)),
        (Data_Type.BYTE, 200, bytes([200, 0])),
        (Data_Type.FLOAT64, 3420.5, struct.pack(">d", 3420.5)),
        (Data_Type.USHORT, True, (1).to_bytes(2, "big")),            # booleans are flags
        (Data_Type.USHORT, "42", (42).to_bytes(2, "big")),           # numeric strings
        (Data_Type.STRING, "connected", b"connected"),
        (Data_Type.STRING, 12, b"12"),
        (Data_Type.STRING, True, b"true"),
        (Data_Type.HEX, "dead", bytes.fromhex("dead")),
        (Data_Type.BIT16_FLAGS, 5, (5).to_bytes(2, "big")),
        (Data_Type.BIT32_FLAGS, 5, (5).to_bytes(4, "big")),
    ],
)
def test_encode_value(data_type: Data_Type, value: Any, expected: bytes) -> None:
    assert encode_value(data_type, value) == expected


@pytest.mark.parametrize(
    ("data_type", "value"),
    [
        (Data_Type.USHORT, None),
        (Data_Type.USHORT, 70000),            # out of range
        (Data_Type.USHORT, -1),
        (Data_Type.SHORT, 40000),
        (Data_Type.UINT, 2**32),
        (Data_Type.BYTE, 256),
        (Data_Type.USHORT, "n/a"),            # not numeric
        (Data_Type.USHORT, float("nan")),
        (Data_Type.USHORT, float("inf")),
        (Data_Type.USHORT, [1]),
        (Data_Type.STRING, {"a": 1}),
        (Data_Type.HEX, "zz"),
        (Data_Type.FLOAT32, 1e300),           # overflows a 32-bit float
        (Data_Type.BIT16_FLAGS, -1),
        (Data_Type._3BIT, 1),                 # bit fields need a register bit; a REST key has none
    ],
)
def test_encode_value_rejects_what_it_cannot_represent(data_type: Data_Type, value: Any) -> None:
    assert encode_value(data_type, value) is None


# --------------------------------------------------------------------------- #
# jwt_expiry                                                                   #
# --------------------------------------------------------------------------- #

def test_jwt_expiry_reads_exp_claim() -> None:
    expiry: float | None = jwt_expiry(make_jwt(1000))
    assert expiry is not None
    assert expiry == approx(time.time() + 1000, abs=5)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.b.c", "a.!!!.c", f"a.{base64.urlsafe_b64encode(b'[1]').decode()}.c"])
def test_jwt_expiry_returns_none_for_unusable_tokens(token: str) -> None:
    assert jwt_expiry(token) is None


def test_jwt_expiry_requires_a_numeric_exp() -> None:
    payload: str = base64.urlsafe_b64encode(json.dumps({"exp": "tomorrow"}).encode()).decode().rstrip("=")
    assert jwt_expiry(f"h.{payload}.s") is None


# --------------------------------------------------------------------------- #
# construction                                                                 #
# --------------------------------------------------------------------------- #

def test_requires_a_host() -> None:
    with pytest.raises(ValueError, match="Host is not set"):
        make_transport(host="")


def test_rejects_unknown_auth_type() -> None:
    with pytest.raises(ValueError, match="auth_type"):
        make_transport(auth_type="oauth")


def test_requires_a_protocol() -> None:
    with pytest.raises(ValueError, match="protocol_version"):
        make_transport(protocol_version="")


def test_rejects_a_numeric_protocol() -> None:
    with pytest.raises(ValueError, match="text-keyed"):
        make_transport(protocol_version="enphase_iq_gateway_sunspec")


def test_rejects_a_missing_ca_cert(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ca_cert"):
        make_transport(ca_cert=str(tmp_path / "missing.pem"))


def test_default_ports() -> None:
    assert enphase_rest(DummySettings(host="h", protocol_version=PROTOCOL, token="t")).port == 443  # type: ignore[arg-type]
    assert enphase_rest(DummySettings(host="h", protocol_version=PROTOCOL, token="t", use_https=False)).port == 80  # type: ignore[arg-type]


def test_scrape_target_identifies_gateway_and_protocol() -> None:
    transport: enphase_rest = make_transport(host="192.168.1.50", port=443, use_https=True)
    assert transport.scrape_target == f"192.168.1.50:443:{PROTOCOL}"
    assert make_transport(host="10.0.0.2").scrape_target != transport.scrape_target


@pytest.mark.parametrize(
    ("host", "port", "https", "expected"),
    [
        ("gw", 443, True, "https://gw/x"),
        ("gw", 8443, True, "https://gw:8443/x"),
        ("gw", 80, False, "http://gw/x"),
        ("gw", 8080, False, "http://gw:8080/x"),
        ("fe80::1", 443, True, "https://[fe80::1]/x"),
    ],
)
def test_url(host: str, port: int, https: bool, expected: str) -> None:
    assert make_transport(host=host, port=port, use_https=https)._url("/x") == expected


def test_token_is_read_from_token_file(tmp_path: Path) -> None:
    token: str = make_jwt(86400)
    token_file: Path = tmp_path / "token.txt"
    token_file.write_text(token + "\n", encoding="utf-8")
    transport: enphase_rest = make_transport(token="", token_file=str(token_file))
    assert transport.token == token


def test_every_readable_row_is_bound_to_an_endpoint() -> None:
    transport: enphase_rest = make_transport()
    assert list(transport._bindings) == [
        "/api/v1/production",
        "/ivp/livedata/status",
        "/ivp/ensemble/power",
        "/api/v1/production/inverters",
    ]
    assert sum(len(rows) for rows in transport._bindings.values()) == 16


# --------------------------------------------------------------------------- #
# connect and polling against the fake gateway                                 #
# --------------------------------------------------------------------------- #

def test_connect_and_read_decodes_every_value(gateway: FakeGateway) -> None:
    token: str = make_jwt(180 * 86400)
    transport: enphase_rest = make_transport(gateway, token=token)
    gateway.required_token = token

    assert transport.connect() is True
    assert transport.connected is True

    data: dict[str, int | float | str] = transport.read_data()

    assert data == {
        "production_power": 3420,
        "energy_today": 8456,
        "energy_seven_days": 45230,
        "energy_lifetime": 3456789,
        "live_stream_state": "enabled",
        "gateway_mqtt_state": "connected",
        "pv_power": approx(3420.5),
        "grid_power": approx(-1250.5),
        "load_power": approx(1670.0),
        "battery_power": approx(-500.0),
        "battery_soc": 85,
        "battery_count": 2,
        "battery_temperature_max": 31,
        "battery_capacity": 6720,
        "inverter_count": 2,
        "inverter_power_total": 560,
    }
    transport.cleanup()


def test_bearer_token_is_sent_on_every_request(gateway: FakeGateway) -> None:
    token: str = make_jwt(180 * 86400)
    transport: enphase_rest = make_transport(gateway, token=token)
    transport.connect()
    transport.read_data()
    authenticated: list[str] = [auth for path, auth in gateway.requests if path != "/info"]
    assert authenticated
    assert set(authenticated) == {f"Bearer {token}"}
    transport.cleanup()


def test_each_endpoint_is_fetched_once_per_cycle(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    gateway.requests.clear()

    transport.read_data()

    for path in ("/api/v1/production", "/ivp/livedata/status", "/ivp/ensemble/power", "/api/v1/production/inverters"):
        assert gateway.count(path) == 1, path     # six rows read /ivp/livedata/status, one request
    transport.cleanup()


def test_slow_rows_are_not_refetched_until_due(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    first: dict[str, int | float | str] = transport.read_data()
    gateway.requests.clear()

    second: dict[str, int | float | str] = transport.read_data()

    assert "inverter_count" in first
    assert gateway.count("/api/v1/production/inverters") == 0     # 300s interval
    assert "inverter_count" not in second
    assert gateway.count("/ivp/livedata/status") == 1             # transport interval: due every cycle
    assert second["pv_power"] == approx(3420.5)

    transport._bindings["/api/v1/production/inverters"][0].entry.next_read_timestamp = 0.0
    assert "inverter_count" in transport.read_data()
    transport.cleanup()


def test_reconnect_makes_every_row_due_again(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    transport.read_data()

    transport.connect()

    assert "inverter_count" in transport.read_data()
    transport.cleanup()


def test_absent_fields_are_omitted_and_reported_once(gateway: FakeGateway) -> None:
    no_battery: dict[str, Any] = {"connection": LIVEDATA["connection"], "meters": {"pv": LIVEDATA["meters"]["pv"], "grid": LIVEDATA["meters"]["grid"], "load": LIVEDATA["meters"]["load"]}}
    gateway.responses["/ivp/livedata/status"] = (200, json.dumps(no_battery))
    gateway.responses["/ivp/ensemble/power"] = (200, json.dumps({"devices:": []}))
    transport: enphase_rest = make_transport(gateway)
    transport.connect()

    first: dict[str, int | float | str] = transport.read_data()
    second: dict[str, int | float | str] = transport.read_data()

    for data in (first, second):
        assert "battery_power" not in data
        assert "battery_soc" not in data
        assert "battery_temperature_max" not in data
        assert data["pv_power"] == approx(3420.5)
    assert first["battery_count"] == 0
    messages: list[str] = [str(call.args[0]) for call in log_of(transport).info.call_args_list]
    assert sum("'battery_soc'" in message for message in messages) == 1, "a missing field is reported once, not every cycle"
    transport.cleanup()


def test_a_failing_endpoint_leaves_the_rest_and_marks_the_cycle_incomplete(gateway: FakeGateway) -> None:
    gateway.responses["/ivp/ensemble/power"] = (503, "busy")
    transport: enphase_rest = make_transport(gateway)
    transport.connect()

    transport._start_cycle_tracking()
    data: dict[str, int | float | str] = transport.read_data()
    transport.finish_cycle_tracking(data)

    assert transport.connected is True            # a throttled gateway is still a connected gateway
    assert "battery_count" not in data
    assert data["pv_power"] == approx(3420.5)
    result = transport.get_cycle_result()
    assert result.is_complete is False
    assert result.skipped_units == 1
    assert result.expected_units == 4 and result.completed_units == 3
    assert not transport.cycle_is_complete_for_bridge()
    transport.cleanup()


def test_non_json_response_is_a_failed_endpoint(gateway: FakeGateway) -> None:
    gateway.responses["/api/v1/production"] = (200, "<html>login</html>")
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    assert "production_power" not in transport.read_data()
    transport.cleanup()


def test_cycle_is_complete_when_every_endpoint_answers(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    transport._start_cycle_tracking()
    data: dict[str, int | float | str] = transport.read_data()
    transport.finish_cycle_tracking(data)
    assert transport.cycle_is_complete_for_bridge()
    transport.cleanup()


def test_read_data_iter_yields_per_endpoint_and_fills_partial_data(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()

    steps: int = sum(1 for _ in transport.read_data_iter())

    assert steps == 4
    assert transport.get_partial_data()["pv_power"] == approx(3420.5)
    assert transport.get_cycle_result().completed_units == 4
    transport.cleanup()


def test_read_data_iter_yields_once_when_nothing_is_due(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    for _ in transport.read_data_iter():
        pass
    for bindings in transport._bindings.values():
        for binding in bindings:
            binding.entry.next_read_timestamp = time.time() * 1000 + 3_600_000

    assert sum(1 for _ in transport.read_data_iter()) == 1
    transport.cleanup()


def test_request_delay_spaces_requests(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, request_delay=0.15)
    transport.connect()
    started: float = time.monotonic()
    transport.read_data()
    assert time.monotonic() - started >= 3 * 0.15 * 0.9      # four endpoints, three gaps
    transport.cleanup()


def test_serial_number_is_discovered_from_info(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    assert transport.device_serial_number == ""
    transport.connect()
    assert transport.device_serial_number == "122112345678"
    assert transport.device_identifier == "122112345678"
    transport.cleanup()


def test_configured_serial_number_wins_over_info(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, device_serial_number="999")
    transport.connect()
    assert transport.device_serial_number == "999"
    transport.cleanup()


def test_info_failure_does_not_stop_connect(gateway: FakeGateway) -> None:
    del gateway.responses["/info"]
    transport: enphase_rest = make_transport(gateway)
    assert transport.connect() is True
    transport.cleanup()


# --------------------------------------------------------------------------- #
# failures and connection state                                                #
# --------------------------------------------------------------------------- #

def test_unreachable_gateway_fails_connect_and_read() -> None:
    transport: enphase_rest = make_transport(port=1, timeout=1)     # nothing listens on port 1
    assert transport.connect() is False
    assert transport.connected is False
    assert transport.read_data() == {}


def test_a_dropped_connection_marks_the_transport_disconnected(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    assert transport.connected is True

    gateway.stop()
    transport._start_cycle_tracking()
    data: dict[str, int | float | str] = transport.read_data()
    transport.finish_cycle_tracking(data)

    assert data == {}
    assert transport.connected is False
    result = transport.get_cycle_result()
    assert result.skipped_units == 4, "the endpoints after the first failure are counted as skipped, not tried"
    transport.cleanup()


def test_wrong_token_fails_connect(gateway: FakeGateway) -> None:
    gateway.required_token = "the-right-token"
    transport: enphase_rest = make_transport(gateway)
    assert transport.connect() is False
    assert transport.connected is False
    assert any("401" in str(call.args[0]) for call in log_of(transport).error.call_args_list)


def test_token_rejected_mid_session_disconnects_so_the_gateway_reconnects(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    gateway.required_token = "rotated"

    assert transport.read_data() == {}
    assert transport.connected is False
    transport.cleanup()


def test_missing_token_is_reported_not_raised(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, token="")
    assert transport.connect() is False
    assert any("no JWT token" in str(call.args[0]) for call in log_of(transport).error.call_args_list)


def test_expired_token_without_credentials_is_refused(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, token=make_jwt(-60))
    assert transport.connect() is False
    assert any("expired" in str(call.args[0]) for call in log_of(transport).error.call_args_list)


def test_auth_none_sends_no_authorization_header(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, auth_type="none", token="")
    assert transport.connect() is True
    assert all(auth == "" for _, auth in gateway.requests)
    transport.cleanup()


def test_write_mode_is_refused() -> None:
    transport: enphase_rest = make_transport()
    transport.write_enabled = True
    transport.enable_write()
    assert transport.write_enabled is False
    log_of(transport).warning.assert_called()


def test_cleanup_disconnects_and_closes_the_session(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway)
    transport.connect()
    transport.cleanup()
    assert transport.connected is False
    assert transport._session is None


# --------------------------------------------------------------------------- #
# digest authentication                                                        #
# --------------------------------------------------------------------------- #

def test_digest_password_defaults_to_the_last_six_digits_of_the_serial() -> None:
    transport: enphase_rest = make_transport(auth_type="digest", token="", device_serial_number="122112345678")
    assert transport._prepare_auth() is True
    assert isinstance(transport._digest, HTTPDigestAuth)
    assert transport._digest.username == "envoy"
    assert transport._digest.password == "345678"


def test_digest_uses_a_configured_password() -> None:
    transport: enphase_rest = make_transport(auth_type="digest", token="", password="secret1", username="installer")
    assert transport._prepare_auth() is True
    assert isinstance(transport._digest, HTTPDigestAuth)
    assert (transport._digest.username, transport._digest.password) == ("installer", "secret1")


def test_digest_without_password_or_serial_is_refused() -> None:
    transport: enphase_rest = make_transport(auth_type="digest", token="")
    assert transport._prepare_auth() is False


def test_digest_requests_carry_no_bearer_header(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, auth_type="digest", token="ignored", password="123456")
    assert transport.connect() is True
    assert all(not auth.startswith("Bearer") for _, auth in gateway.requests)
    transport.cleanup()


# --------------------------------------------------------------------------- #
# token renewal                                                                #
# --------------------------------------------------------------------------- #

def ok_response(*, json_body: Any = None, text: str = "") -> MagicMock:
    response: MagicMock = MagicMock(spec=requests.Response)
    response.raise_for_status.return_value = None
    response.json.return_value = json_body
    response.text = text
    return response


def test_token_is_renewed_from_enlighten_when_missing(gateway: FakeGateway) -> None:
    new_token: str = make_jwt(365 * 86400)
    gateway.required_token = new_token
    transport: enphase_rest = make_transport(
        gateway, token="", enlighten_username="me@example.com", enlighten_password="hunter2hunter2",
    )

    with patch.object(module.requests, "post", side_effect=[ok_response(json_body={"session_id": "S1"}), ok_response(text=new_token + "\n")]) as post:
        assert transport.connect() is True

    login_call, token_call = post.call_args_list
    assert login_call.args[0] == module.ENLIGHTEN_LOGIN_URL
    assert login_call.kwargs["data"] == {"user[email]": "me@example.com", "user[password]": "hunter2hunter2"}
    assert token_call.args[0] == module.ENTREZ_TOKEN_URL
    assert token_call.kwargs["json"] == {"session_id": "S1", "serial_num": "122112345678", "username": "me@example.com"}
    assert transport.token == new_token
    transport.cleanup()


def test_token_close_to_expiry_is_renewed_at_connect(gateway: FakeGateway) -> None:
    new_token: str = make_jwt(365 * 86400)
    transport: enphase_rest = make_transport(
        gateway, token=make_jwt(2 * 86400), enlighten_username="u", enlighten_password="p" * 8, device_serial_number="1",
    )
    with patch.object(module.requests, "post", side_effect=[ok_response(json_body={"session_id": "S"}), ok_response(text=new_token)]):
        assert transport.connect() is True
    assert transport.token == new_token
    transport.cleanup()


def test_healthy_token_is_not_renewed(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(gateway, enlighten_username="u", enlighten_password="p" * 8)
    with patch.object(module.requests, "post") as post:
        assert transport.connect() is True
    post.assert_not_called()
    transport.cleanup()


def test_a_401_triggers_one_renewal_and_a_retry(gateway: FakeGateway) -> None:
    new_token: str = make_jwt(365 * 86400)
    transport: enphase_rest = make_transport(
        gateway, enlighten_username="u", enlighten_password="p" * 8, device_serial_number="1",
    )
    transport.connect()
    gateway.required_token = new_token     # the gateway now wants a different token

    with patch.object(module.requests, "post", side_effect=[ok_response(json_body={"session_id": "S"}), ok_response(text=new_token)]) as post:
        document: Any = transport._get_json("/api/v1/production")

    assert document == PRODUCTION
    assert post.call_count == 2
    assert transport.connected is True
    transport.cleanup()


def test_failed_renewal_is_not_retried_every_cycle(gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(
        gateway, token="", enlighten_username="u", enlighten_password="p" * 8, device_serial_number="1",
    )
    with patch.object(module.requests, "post", side_effect=requests.ConnectionError("no route")) as post:
        assert transport.connect() is False
        assert transport.connect() is False
    assert post.call_count == 1, "the second connect is inside the retry back-off"
    assert any("could not renew" in str(call.args[0]) for call in log_of(transport).error.call_args_list)


@pytest.mark.parametrize(
    "responses",
    [
        [ok_response(json_body={"no": "session"})],                                        # login without a session id
        [ok_response(json_body={"session_id": "S"}), ok_response(text="not a jwt")],      # token service returned junk
    ],
)
def test_malformed_enlighten_replies_fail_the_renewal(responses: list[MagicMock], gateway: FakeGateway) -> None:
    transport: enphase_rest = make_transport(
        gateway, token="", enlighten_username="u", enlighten_password="p" * 8, device_serial_number="1",
    )
    with patch.object(module.requests, "post", side_effect=responses):
        assert transport._refresh_token() is False
    assert transport.token == ""


def test_renewal_needs_a_serial_number() -> None:
    transport: enphase_rest = make_transport(token="", enlighten_username="u", enlighten_password="p" * 8)
    with patch.object(module.requests, "post") as post:
        assert transport._refresh_token() is False
    post.assert_not_called()
    assert any("serial number" in str(call.args[0]) for call in log_of(transport).error.call_args_list)


def test_renewed_token_is_registered_for_log_redaction(gateway: FakeGateway) -> None:
    new_token: str = make_jwt(365 * 86400)
    transport: enphase_rest = make_transport(
        gateway, token="", enlighten_username="u", enlighten_password="p" * 8, device_serial_number="1",
    )
    with (
        patch.object(module.requests, "post", side_effect=[ok_response(json_body={"session_id": "S"}), ok_response(text=new_token)]),
        patch.object(module, "register_secret") as register,
    ):
        assert transport._refresh_token() is True
    register.assert_called_once_with(new_token)


# --------------------------------------------------------------------------- #
# web UI contract                                                              #
# --------------------------------------------------------------------------- #

def test_transport_is_classified_as_a_scraper_by_the_web_ui_scanner() -> None:
    assert _classify_transport("transport.gw", {"transport": "enphase_rest"}, REPO / "classes" / "transports") == "scraper"


def test_transport_has_defaults_and_descriptions_for_every_setting() -> None:
    defaults: dict[str, dict[str, str]] = tr.get_known_transport_keys()
    assert "enphase_rest" in defaults
    descriptions: dict[str, str] = tr.get_setting_descriptions()

    from classes.WebServer.scanner import _extract_settings_keys_from_ast
    used: set[str] = set(_extract_settings_keys_from_ast(REPO / "classes" / "transports" / "enphase_rest.py"))

    assert {"host", "port", "use_https", "verify_tls", "ca_cert", "auth_type", "token", "token_file",
            "enlighten_username", "enlighten_password", "username", "password", "timeout", "request_delay"} <= used
    missing_defaults: set[str] = {key for key in used if key not in defaults["enphase_rest"]}
    missing_descriptions: set[str] = {key for key in used if not descriptions.get(key)}
    assert not missing_defaults, f"add to transport_defaults.json: {sorted(missing_defaults)}"
    assert not missing_descriptions, f"add to setting_descriptions.json: {sorted(missing_descriptions)}"


def test_protocol_json_names_this_transport() -> None:
    from classes.protocol_settings import protocol_settings
    assert protocol_settings.get_transport_type(PROTOCOL) == "enphase_rest"
