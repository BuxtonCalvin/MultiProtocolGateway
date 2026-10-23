# Description: Tests that the web UI discovers, classifies, lists and edits serial transports (serial_base and its subclasses) correctly.
# File: test_serial_transport_webui.py
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

"""
Web UI structure tests for the serial transport hierarchy.

The WebServer scanner reads each transport module with ast and never follows Python
inheritance, so the class hierarchy only reaches the UI through two explicit contracts:

  * every concrete transport declares its own literal ``transport_type = "scraper"``
  * every serial transport extends the ``_serial_base`` template in transport_defaults.json

These tests pin both, so the next serial protocol cannot silently fall out of the UI.
The last group boots the real FastAPI app against a temporary config and registry.
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from classes.transports.serial_base import serial_base
from classes.transports.serial_frame_transport import serial_frame_transport
from classes.transports.serial_pylon import serial_pylon
from classes.WebServer import transport_registry as tr
from classes.WebServer.scanner import (
    _classify_transport,
    _extract_settings_keys_from_ast,
    scan_transport_library,
)

REPO: Path = Path(__file__).resolve().parent.parent
WEBSERVER_DIR: Path = REPO / "classes" / "WebServer"
TRANSPORTS_DIR: Path = REPO / "classes" / "transports"


def raw_defaults() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((WEBSERVER_DIR / "transport_defaults.json").read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


def serial_subclasses() -> list[type[serial_base]]:
    found: list[type[serial_base]] = []
    pending: list[type[serial_base]] = list(serial_base.__subclasses__())
    while pending:
        cls: type[serial_base] = pending.pop()
        found.append(cls)
        pending.extend(cls.__subclasses__())
    return [c for c in found if c.__module__.startswith("classes.transports.")]


# --------------------------------------------------------------------------- #
# scanner / library                                                           #
# --------------------------------------------------------------------------- #

def test_scanner_classifies_serial_pylon_sections_as_scrapers() -> None:
    assert _classify_transport("transport.battery", {"transport": "serial_pylon"}, TRANSPORTS_DIR) == "scraper"


def test_library_lists_serial_base_as_a_base_class_like_modbus_base() -> None:
    library: dict[str, tr.TransportLibraryEntry] = scan_transport_library(TRANSPORTS_DIR)
    assert library["serial_base"]["classification"] == "base class"
    assert library["serial_base"]["classification"] == library["modbus_base"]["classification"]
    assert library["serial_pylon"]["classification"] == "scraper"


def test_serial_base_keys_reach_the_ui_through_the_template() -> None:
    """Keys read in serial_base.py are not visible in serial_pylon.py's AST; the template must carry them."""
    base_keys: set[str] = set(_extract_settings_keys_from_ast(TRANSPORTS_DIR / "serial_base.py"))
    assert {"port", "baudrate", "baud", "address", "unit_id"} <= base_keys

    resolved: dict[str, str] = tr._resolve_transport("serial_pylon", raw_defaults())
    for key in ("port", "baudrate", "address"):
        assert key in resolved, f"serial_pylon would lose the '{key}' setting in the UI"
    assert "ignore_checksum_errors" in _extract_settings_keys_from_ast(TRANSPORTS_DIR / "serial_pylon.py")
    assert "ignore_checksum_errors" in resolved


# --------------------------------------------------------------------------- #
# registry structure                                                          #
# --------------------------------------------------------------------------- #

def test_serial_template_chain_mirrors_modbus() -> None:
    raw: dict[str, Any] = raw_defaults()
    assert raw["_serial_base"]["$extends"] == raw["_modbus_base"]["$extends"] == "_base"
    assert raw["serial_base"]["$extends"] == "_serial_base"
    assert raw["modbus_base"]["$extends"] == "_modbus_base"


def test_serial_pylon_defaults_are_unchanged_by_the_refactor() -> None:
    resolved: dict[str, str] = tr._resolve_transport("serial_pylon", raw_defaults())
    expected: dict[str, str] = {
        "address": "0",
        "baudrate": "9600",
        "port": "/dev/ttyUSB0",
        "batch_size": "40",
        "enable_synthetic_metrics": "True",
        "ignore_checksum_errors": "False",
        "protocol_version": "",
        "variable_mask": "",
        "variable_screen": "",
    }
    for key, value in expected.items():
        assert resolved.get(key) == value, key
    # the serial_base-only alias keys must not show up as duplicate fields on a concrete transport
    assert "baud" not in resolved
    assert "unit_id" not in resolved


@pytest.mark.parametrize("cls", serial_subclasses(), ids=lambda c: c.__name__)
def test_every_serial_transport_is_wired_into_the_web_ui(cls: type[serial_base]) -> None:
    name: str = cls.__name__
    source: str = (TRANSPORTS_DIR / f"{cls.__module__.rsplit('.', 1)[-1]}.py").read_text(encoding="utf-8")

    assert re.search(r'^\s+transport_type\s*(:\s*str\s*)?=\s*"scraper"', source, re.MULTILINE), (
        f"{name} must declare its own literal transport_type (AST scanner); inheriting it is not enough"
    )
    assert _classify_transport("transport.x", {"transport": name}, TRANSPORTS_DIR) == "scraper"
    assert raw_defaults()[name]["$extends"] == "_serial_base", f"{name} must extend _serial_base in transport_defaults.json"


def test_known_serial_transports() -> None:
    """If this fails you have added a serial transport: good. Add its tests, then update this list."""
    assert serial_pylon in serial_subclasses()
    assert serial_frame_transport in serial_subclasses()
    assert sorted(c.__name__ for c in serial_subclasses()) == ["serial_frame_transport", "serial_pylon"]


def test_serial_frame_transport_defaults_keep_its_own_settings() -> None:
    resolved: dict[str, str] = tr._resolve_transport("serial_frame_transport", raw_defaults())
    for key, value in (("soi", "7E"), ("eoi", "0D"), ("timeout", "5.0"), ("async_mode", "False"),
                       ("port", "/dev/ttyUSB0"), ("baudrate", "9600")):
        assert resolved.get(key) == value, key
    assert "baud" not in resolved, "baud is still accepted in a config, but the UI offers the canonical baudrate"

    keys: set[str] = set(_extract_settings_keys_from_ast(TRANSPORTS_DIR / "serial_frame_transport.py"))
    assert {"soi", "eoi", "timeout", "async_mode"} <= keys


# --------------------------------------------------------------------------- #
# the real web app                                                            #
# --------------------------------------------------------------------------- #

CONFIG = """[general]
read_mode = interleaved

[transport.pylon_battery_1]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 9600
address = 2
protocol_version = pylon_rs485_v3.3

[transport.pylon_battery_2]
transport = serial_pylon
port = /dev/ttyUSB0
baudrate = 9600
address = 3
protocol_version = pylon_rs485_v3.3

[transport.frame_listener]
transport = serial_frame_transport
port = /dev/ttyUSB1
baud = 19200
soi = 7E
eoi = 0D
"""


@dataclass
class ReloadStatus:
    """What GatewayManager.reload() returns: ReloadStatusLike (ok, message) plus the flag the commit route reads."""

    ok: bool
    message: str
    using_fallback: bool = False


class Manager:
    """GatewayManagerLike test double that records reload requests."""

    def __init__(self, gateway: object) -> None:
        self._gateway: object = gateway
        self.reloads: list[str] = []

    @property
    def current(self) -> object | None:
        return self._gateway

    def reload(self, trigger: str) -> ReloadStatus:
        self.reloads.append(trigger)
        return ReloadStatus(ok=True, message="ok")


@pytest.fixture(scope="module")
def web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """Boot the real FastAPI app (lifespan, scanner, staging DB) on a temp config and a temp copy of the registry."""
    from fastapi.testclient import TestClient

    from classes.WebServer.main import create_app

    tmp: Path = tmp_path_factory.mktemp("mpg_web")
    registry: Path = tmp / "registry"
    registry.mkdir()
    for name in ("transport_defaults.json", "setting_descriptions.json"):
        shutil.copy(WEBSERVER_DIR / name, registry / name)

    previous_registry_dir: Path = tr._registry_dir
    tr.configure(registry)

    config_path: Path = tmp / "config.cfg"
    config_path.write_text(CONFIG, encoding="utf-8")

    gateway = SimpleNamespace(web_server=None)
    manager = Manager(gateway)

    app: FastAPI = create_app(
        config_dir=tmp, config_path=config_path, db_dir=tmp, gateway_instance=gateway,
        gateway_manager=manager, log_path=tmp / "mpg.log", project_root=REPO,
        protocols_dir=REPO / "protocols",
    )
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, config_path=config_path, reloads=manager.reloads, registry=registry)
    finally:
        tr.configure(previous_registry_dir)


def test_web_pages_render_with_serial_transports(web: SimpleNamespace) -> None:
    for path in ("/", "/pages/transport-library", "/pages/transport-settings", "/pages/global-settings"):
        assert web.client.get(path).status_code == 200, path

    library = web.client.get("/pages/transport-library").text
    for name in ("serial_base", "serial_pylon", "modbus_base", "transport_base"):
        assert name in library


def test_device_pages_exist_for_each_battery_section(web: SimpleNamespace) -> None:
    for name, address in (("pylon_battery_1", "2"), ("pylon_battery_2", "3")):
        response = web.client.get(f"/device/{name}")
        assert response.status_code == 200, name
        assert "serial_pylon" in response.text
        assert f'value="{address}"' in response.text
        assert "ignore_checksum_errors" in response.text


def test_serial_frame_device_page_offers_its_own_and_the_serial_settings(web: SimpleNamespace) -> None:
    response = web.client.get("/device/frame_listener")
    assert response.status_code == 200
    assert "serial_frame_transport" in response.text

    rows: dict[Any, Any] = {r["key"]: r for r in web.client.get("/api/devices/frame_listener/settings").json()}
    assert {"transport", "port", "baud", "soi", "eoi"} <= set(rows), "the keys the section actually uses"
    assert rows["soi"]["value_disk"] == "7E"
    assert rows["baud"]["value_disk"] == "19200"
    assert rows["soi"]["default_value"] == "7E"
    assert tr.get_default_for("serial_frame_transport", "timeout") == "5.0"


def test_device_settings_api_gets_defaults_from_the_serial_template(web: SimpleNamespace) -> None:
    rows: dict[Any, Any] = {r["key"]: r for r in web.client.get("/api/devices/pylon_battery_1/settings").json()}
    assert {"transport", "port", "baudrate", "address", "protocol_version"} <= set(rows)
    assert rows["address"]["value_disk"] == "2"
    assert rows["address"]["default_value"] == "0"
    assert rows["baudrate"]["default_value"] == "9600"
    assert tr.get_default_for("serial_pylon", "ignore_checksum_errors") == "False"


def test_transport_settings_api_describes_the_serial_keys(web: SimpleNamespace) -> None:
    rows: dict[Any, Any] = {r["key"]: r for r in web.client.get("/api/transport-settings").json()}
    assert rows["ignore_checksum_errors"]["transports"] == "serial_pylon"
    assert rows["ignore_checksum_errors"]["description"]
    assert rows["unit_id"]["description"]
    assert "serial_base" in rows["unit_id"]["transports"]


def test_startup_sync_leaves_the_committed_registry_unchanged(web: SimpleNamespace) -> None:
    """If the scanner wants to add keys on startup, the committed JSON is stale and every web start dirties the tree."""
    for name in ("transport_defaults.json", "setting_descriptions.json"):
        committed = json.loads((WEBSERVER_DIR / name).read_text(encoding="utf-8"))
        synced = json.loads((web.registry / name).read_text(encoding="utf-8"))
        assert synced == committed, f"{name} changed during web startup sync"


def test_editing_and_committing_a_serial_device_writes_config_cfg(web: SimpleNamespace) -> None:
    client = web.client
    rows: dict[Any, Any] = {r["key"]: r for r in client.get("/api/devices/pylon_battery_1/settings").json()}

    assert client.patch(f"/api/devices/pylon_battery_1/settings/{rows['address']['id']}", json={"value_staged": "4"}).status_code == 200

    created = client.post("/api/devices/pylon_battery_1/settings/create-and-activate", json={"key": "ignore_checksum_errors"})
    assert created.status_code == 200
    assert client.patch(
        f"/api/devices/pylon_battery_1/settings/{created.json()['id']}", json={"value_staged": "true"}
    ).status_code == 200

    diff = client.get("/api/commit/diff").json()
    assert diff["summary"]["total_changes"] == 2

    committed = client.post("/api/commit")
    assert committed.status_code == 200, committed.text

    parser = configparser.ConfigParser()
    parser.read(web.config_path)
    assert parser["transport.pylon_battery_1"]["address"] == "4"
    assert parser["transport.pylon_battery_1"]["ignore_checksum_errors"] == "true"
    assert parser["transport.pylon_battery_2"]["address"] == "3", "other batteries must be untouched"
    assert "ignore_checksum_errors" not in parser["transport.pylon_battery_2"]
    assert web.reloads, "the gateway should be asked to reload after a commit"
