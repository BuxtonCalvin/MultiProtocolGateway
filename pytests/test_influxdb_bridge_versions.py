"""Tests for the "Versions" group of the InfluxDB v1/v3 Bridge Health panel (no real InfluxDB or internet needed)."""

from __future__ import annotations

# ruff: noqa: S105, S106, S107  (the "tokens" here are throwaway test values for a local fake server)
# These tests deliberately reach into private helpers and use lightweight fakes.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false, reportAttributeAccessIssue=false
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from influxdb import InfluxDBClient  # pyright: ignore[reportMissingTypeStubs]
from jinja2 import Environment, FileSystemLoader

import classes.WebServer.services.bridge_service as bs
from classes.transports.influxdb3_out import influxdb3_out
from classes.transports.influxdb_out import influxdb_out

TEMPLATES = Path(__file__).resolve().parents[1] / "classes" / "WebServer" / "templates"


# --------------------------------------------------------------------------- a tiny fake InfluxDB /ping
class _PingServer:
    """Serves GET /ping like InfluxDB: v1 = 204 + headers; v3 = 200 + headers + JSON, token required."""

    def __init__(self, mode: str, token: str = "tok", headers: bool = True, status: int | None = None) -> None:
        self.hits: int = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                outer.hits += 1
                if status is not None:
                    self.send_response(status)
                    self.end_headers()
                    return
                if mode == "v3":
                    if self.headers.get("Authorization") != f"Bearer {token}":
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = json.dumps({"version": "3.9.1", "revision": "abc", "process_id": "1"}).encode()
                    self.send_response(200)
                    if headers:
                        self.send_header("x-influxdb-version", "3.9.1")
                        self.send_header("x-influxdb-build", "Core")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(204)
                    self.send_header("X-Influxdb-Version", "1.11.7")
                    self.send_header("X-Influxdb-Build", "OSS")
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server signature
                return None

        self._srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def servers() -> Iterator[list[_PingServer]]:
    made: list[_PingServer] = []
    yield made
    for s in made:
        s.close()


def _serve(servers: list[_PingServer], *args: Any, **kwargs: Any) -> _PingServer:
    s = _PingServer(*args, **kwargs)
    servers.append(s)
    return s


def _v1(port: int, connected: bool = True) -> Any:
    b: Any = influxdb_out.__new__(influxdb_out)
    b._connected = connected
    b.client = InfluxDBClient(host="127.0.0.1", port=port, timeout=3)
    b._log = logging.getLogger("t.v1")
    b._server_info_cache = None
    return b


def _v3(port: int, token: str = "tok", connected: bool = True) -> Any:
    b: Any = influxdb3_out.__new__(influxdb3_out)
    b._connected = connected
    b.host, b.port, b.token, b.connection_timeout = "http://127.0.0.1", str(port), token, 3
    b._log = logging.getLogger("t.v3")
    b._server_info_cache = None
    return b


# --------------------------------------------------------------------------- transports: v1
def test_v1_reports_server_version_build_and_client_library(servers: list[_PingServer]) -> None:
    rows = _v1(_serve(servers, "v1").port).get_version_info()
    assert rows[0] == {"label": "InfluxDB server", "version": "1.11.7", "detail": "OSS"}
    assert rows[1]["label"] == "Python client (influxdb)" and rows[1]["version"]  # installed, so a version string


def test_v1_not_connected_makes_no_request(servers: list[_PingServer]) -> None:
    srv = _serve(servers, "v1")
    row = _v1(srv.port, connected=False).get_version_info()[0]
    assert row["version"] is None and srv.hits == 0


def test_v1_result_is_cached_and_failure_is_cached_briefly(servers: list[_PingServer]) -> None:
    srv = _serve(servers, "v1")
    b = _v1(srv.port)
    b.get_version_info()
    b.get_version_info()
    assert srv.hits == 1  # second call served from cache
    bad = _serve(servers, "v1", status=500)
    b2 = _v1(bad.port)
    assert b2.get_version_info()[0]["version"] is None
    b2.get_version_info()
    assert bad.hits == 1  # the failure is cached too, so a dead server is not hammered


# --------------------------------------------------------------------------- transports: v3
def test_v3_reports_version_and_edition_from_ping(servers: list[_PingServer]) -> None:
    rows = _v3(_serve(servers, "v3").port).get_version_info()
    assert rows[0] == {"label": "InfluxDB 3 server", "version": "3.9.1", "detail": "Core"}
    assert rows[1]["label"] == "Python client (influxdb3-python)"


def test_v3_wrong_token_says_why(servers: list[_PingServer]) -> None:
    row = _v3(_serve(servers, "v3").port, token="wrong").get_version_info()[0]
    assert row["version"] is None and row["detail"] == "/ping needs a valid token"


def test_v3_falls_back_to_json_body_when_headers_missing(servers: list[_PingServer]) -> None:
    row = _v3(_serve(servers, "v3", headers=False).port).get_version_info()[0]
    assert row["version"] == "3.9.1" and row["detail"] is None


def test_v3_other_http_status_and_unreachable_server(servers: list[_PingServer]) -> None:
    srv = _serve(servers, "v3", status=503)
    assert _v3(srv.port).get_version_info()[0]["detail"] == "/ping returned HTTP 503"
    port = srv.port
    srv.close()  # nothing listening any more
    row = _v3(port).get_version_info()[0]
    assert row["version"] is None and row["detail"] == "no response"


def test_v3_not_connected_makes_no_request(servers: list[_PingServer]) -> None:
    srv = _serve(servers, "v3")
    row = _v3(srv.port, connected=False).get_version_info()[0]
    assert row["version"] is None and row["detail"] == "not connected" and srv.hits == 0


# --------------------------------------------------------------------------- service: latest-release lookup
@pytest.fixture(autouse=True)
def _fresh_release_cache() -> Iterator[None]:
    bs._latest_release_cache = None
    yield
    bs._latest_release_cache = None


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload, self.status = payload, status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            msg = f"HTTP {self.status}"
            raise RuntimeError(msg)

    def json(self) -> Any:
        return self._payload


def _fake_get(payload: Any) -> Any:
    def get(*_args: object, **_kwargs: object) -> _Resp:
        return _Resp(payload)

    return get


def _fake_latest(*, table: dict[int, tuple[int, int, int]] | None = None, seen: list[int] | None = None) -> Any:
    """Stand-in for get_latest_influxdb_release; fails the test if called when `table` is None."""

    def latest(major: int) -> tuple[int, int, int] | None:
        if table is None:
            pytest.fail("get_latest_influxdb_release must not be called")
        if seen is not None:
            seen.append(major)
        return table.get(major)

    return latest


def _release(tag: str, prerelease: bool = False, draft: bool = False) -> dict[str, Any]:
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


def test_parse_version_tuple() -> None:
    assert bs.parse_version_tuple("1.11.7") == (1, 11, 7)
    assert bs.parse_version_tuple("v3.9.1") == (3, 9, 1)
    assert bs.parse_version_tuple("3.9.1-rc1") == (3, 9, 1)
    assert bs.parse_version_tuple("nightly") is None and bs.parse_version_tuple(None) is None


def test_fetch_takes_the_maximum_per_major_and_skips_prereleases(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [  # newest-first, with a 3.10 maintenance release published after 3.11.x
        _release("v3.11.4"), _release("v3.10.6"), _release("v1.13.1"), _release("v2.9.1"),
        _release("v3.12.0-rc1", prerelease=True), _release("v3.99.0", draft=True), _release("v1.12.4"), _release("weird"),
    ]
    monkeypatch.setattr(bs.requests, "get", _fake_get(payload))
    assert bs._fetch_latest_influxdb_releases() == {3: (3, 11, 4), 1: (1, 13, 1), 2: (2, 9, 1)}


def test_latest_lookup_is_cached_and_failure_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fetch() -> dict[int, tuple[int, int, int]]:
        calls.append(1)
        return {1: (1, 13, 1)}

    monkeypatch.setattr(bs, "_fetch_latest_influxdb_releases", fetch)
    assert bs.get_latest_influxdb_release(1) == (1, 13, 1) and bs.get_latest_influxdb_release(3) is None
    assert len(calls) == 1  # one request serves every major and every bridge

    bs._latest_release_cache = None

    def boom() -> dict[int, tuple[int, int, int]]:
        calls.append(1)
        raise RuntimeError("offline")

    monkeypatch.setattr(bs, "_fetch_latest_influxdb_releases", boom)
    assert bs.get_latest_influxdb_release(1) is None
    assert bs.get_latest_influxdb_release(1) is None
    assert len(calls) == 2  # the failure is remembered, not retried on every page load


# --------------------------------------------------------------------------- service: row building
class _Bridge:
    def __init__(self, rows: list[dict[str, Any]], check: bool, boom: bool = False) -> None:
        self._rows, self.check_latest_release, self._boom = rows, check, boom
        self.transport_name = "transport.influx"

    def get_version_info(self) -> list[dict[str, Any]]:
        if self._boom:
            raise RuntimeError("probe exploded")
        return self._rows

    def get_health_snapshot(self) -> dict[str, Any]:
        return {"last_periodic_reconnect_attempt": 0.0}


def _rows(version: str | None) -> list[dict[str, Any]]:
    return [{"label": "InfluxDB server", "version": version, "detail": "OSS"},
            {"label": "Python client (influxdb)", "version": "5.3.2", "detail": None}]


def test_rows_without_the_opt_in_make_no_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "get_latest_influxdb_release", _fake_latest())
    rows, enabled = bs._build_influxdb_version_rows(_Bridge(_rows("1.11.7"), check=False))
    assert enabled is False and "latest" not in rows[0] and rows[0]["version"] == "1.11.7"


def test_rows_with_opt_in_get_a_verdict_from_the_servers_own_major(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(bs, "get_latest_influxdb_release", _fake_latest(table={1: (1, 13, 1), 3: (3, 11, 4)}, seen=seen))
    behind, _ = bs._build_influxdb_version_rows(_Bridge(_rows("1.11.7"), check=True))
    assert (behind[0]["latest"], behind[0]["up_to_date"]) == ("1.13.1", False)
    current, _ = bs._build_influxdb_version_rows(_Bridge(_rows("3.11.4"), check=True))
    assert (current[0]["latest"], current[0]["up_to_date"]) == ("3.11.4", True)
    ahead, _ = bs._build_influxdb_version_rows(_Bridge(_rows("3.12.0"), check=True))
    assert ahead[0]["up_to_date"] is True  # newer than the newest stable (e.g. a preview build) is not "outdated"
    assert seen == [1, 3, 3]
    assert "latest" not in behind[1]  # the client library row never gets a verdict


def test_rows_leave_out_the_verdict_when_version_or_latest_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "get_latest_influxdb_release", _fake_latest(table={}))
    assert "latest" not in bs._build_influxdb_version_rows(_Bridge(_rows("1.11.7"), check=True))[0][0]
    monkeypatch.setattr(bs, "get_latest_influxdb_release", _fake_latest())
    assert "latest" not in bs._build_influxdb_version_rows(_Bridge(_rows(None), check=True))[0][0]


def test_a_failing_probe_cannot_break_the_panel() -> None:
    assert bs._build_influxdb_version_rows(_Bridge([], check=True, boom=True)) == ([], True)


def test_get_influxdb_health_adds_the_version_fields() -> None:
    class influxdb_out:  # noqa: N801 - get_influxdb_bridge matches on the class name
        transport_name = "transport.influx"
        check_latest_release = False

        def get_health_snapshot(self) -> dict[str, Any]:
            return {"last_periodic_reconnect_attempt": 0.0}

        def get_version_info(self) -> list[dict[str, Any]]:
            return _rows("1.11.7")

    gateway = SimpleNamespace(_Protocol_Gateway__transports=[influxdb_out()])
    health = bs.get_influxdb_health(cast(Any, gateway), "transport.influx")
    assert health["version_rows"][0]["version"] == "1.11.7"
    assert health["latest_release_check_enabled"] is False
    assert health["last_periodic_reconnect_display"] == "never"  # existing behavior untouched


# --------------------------------------------------------------------------- template
def _render(health: dict[str, Any]) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template("partials/bridge_influxdb_health_panel.html").render(health=health)


def _health(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "batch_pending": 0, "batch_size": 100, "persistent_storage_enabled": True, "backlog_count": 0,
        "max_backlog_size": 10000, "periodic_reconnect_interval": 14400.0, "last_periodic_reconnect_display": "never",
        "stale_transport_count": 0, "tracked_transport_count": 1, "latest_release_check_enabled": False,
        "version_rows": [],
    }
    return {**base, **over}


def test_template_shows_versions_and_the_opt_in_hint() -> None:
    html = _render(_health(version_rows=_rows("1.11.7")))
    assert "Versions" in html and "1.11.7" in html and "OSS" in html and "5.3.2" in html
    assert "check_latest_release = true" in html  # hint while the check is off
    assert "Up to date" not in html and "Latest release" not in html


def test_template_shows_verdicts_when_the_check_is_on() -> None:
    behind = _rows("1.11.7")
    behind[0].update(latest="1.13.1", up_to_date=False)
    html = _render(_health(latest_release_check_enabled=True, version_rows=behind))
    assert "Latest release" in html and "1.13.1" in html and "check_latest_release = true" not in html
    current = _rows("3.11.4")
    current[0].update(latest="3.11.4", up_to_date=True)
    assert "Up to date" in _render(_health(latest_release_check_enabled=True, version_rows=current))


def test_template_explains_an_unavailable_server_version() -> None:
    html = _render(_health(version_rows=[{"label": "InfluxDB 3 server", "version": None, "detail": "/ping needs a valid token"}]))
    assert "Unavailable" in html and "/ping needs a valid token" in html


def test_template_without_version_rows_has_no_versions_group() -> None:
    assert "Versions" not in _render(_health())
