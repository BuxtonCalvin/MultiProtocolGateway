# Description: Proves blank (or missing) Home Assistant column cells are accepted by every validator, parser, writer and database constraint.
# File: test_ha_blank_cells.py
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

"""Blank ``ha ...`` cells must never raise.

Blank is the *normal* state — it means "infer from the unit" — and arises in many
ways: a CSV whose header has the columns but whose rows are shorter, whitespace-only
cells, registers appended by the analysis page, a dropdown left on "auto", a wizard
row the user did not touch, or a database row that predates the columns (NULL). Each
path is exercised here against the real code, because each one meets a different
piece of validation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from classes.ha_metadata import HA_COLUMNS, validate_ha_value
from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)
from classes.transports.mqtt import mqtt
from classes.transports.transport_base import transport_base
from classes.WebServer import config_writer, scanner
from classes.WebServer.models import ProtocolRegister
from classes.WebServer.routers import pages, protocols
from classes.WebServer.routers.analysis import (
    AnalysisChange,
    _apply_protocol_changes,  # type: ignore
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytests.conftest import DummySettings

WEBSERVER_DIR: Path = Path(scanner.__file__).resolve().parent

# Header names in both styles the code accepts, and both delimiters.
COMMA_HEADER = "register,variable_name,documented_name,unit,data_type,values,ha_device_class,ha_state_class,ha_entity_category"
SEMI_HEADER = "register;variable name;documented name;unit;data type;values;ha device class;ha state class;ha entity category"

# One row per way a cell can be "blank". Fields: register,name,doc,unit,type,values,<3 HA cells>
ROWS: dict[str, str] = {
    "explicit_empty": "1,vpv1,Vpv1,V,,0-65535,,,",
    "short_row": "2,vpv2,Vpv2,V,,0-65535",                 # the three trailing cells are simply absent
    "whitespace_only": "3,vpv3,Vpv3,V,,0-65535,  ,\t, ",  # blank to a human, not to len()
    "partly_filled": "4,soc,Soc,%,,0-100,battery,,",       # one cell set, two blank
}


def _csv(tmp_path: Path, header: str, delimiter: str) -> Path:
    folder: Path = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "acme_x.json").write_text(json.dumps({"transport": "modbus_tcp"}), encoding="utf-8")
    path: Path = folder / "acme_x.input_registry_map.csv"
    body: str = "\n".join(r.replace(",", delimiter) for r in ROWS.values())
    path.write_text(header + "\n" + body + "\n", encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Session]:
    path: Path = tmp_path / "mpg.sqlite"
    cfg = Config(str(WEBSERVER_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    cfg.set_main_option("script_location", str(WEBSERVER_DIR / "migrations"))
    command.upgrade(cfg, "head")
    engine: Engine = create_engine(f"sqlite:///{path}")
    session: Session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _scan(db: Session, csv_path: Path) -> list[ProtocolRegister]:
    rows: list[ProtocolRegister] = []
    for entry in scanner._parse_protocol_csv(csv_path, "acme"): # type: ignore
        row: ProtocolRegister | None = scanner._upsert_protocol_register(db, entry) # type: ignore
        assert row is not None
        rows.append(row)
    db.commit()
    return rows


# ---------------------------------------------------------------------------
# parsers: scanner (web UI) and protocol_settings (the running gateway)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("header", "delimiter"), [(COMMA_HEADER, ","), (SEMI_HEADER, ";")], ids=["comma", "semicolon"])
def test_every_kind_of_blank_cell_parses_in_scanner_and_runtime_loader(
    tmp_path: Path, header: str, delimiter: str
) -> None:
    """Short rows, empty and whitespace-only cells all become '' — never None, never an exception."""
    path: Path = _csv(tmp_path, header, delimiter)

    entries: dict[Any, dict[str, Any]] = {e["variable_name"]: e for e in scanner._parse_protocol_csv(path, "acme")} # type: ignore
    assert set(entries) == {"vpv1", "vpv2", "vpv3", "soc"}
    for name in ("vpv1", "vpv2", "vpv3"):
        assert [entries[name][c] for c in HA_COLUMNS] == ["", "", ""], name
    assert [entries["soc"][c] for c in HA_COLUMNS] == ["battery", "", ""]

    ps = protocol_settings("acme_x", settings_dir=str(tmp_path / "protocols"))
    loaded: dict[str, registry_map_entry] = {e.variable_name: e for group in ps.registry_map.values() for e in group}
    assert set(loaded) == {"vpv1", "vpv2", "vpv3", "soc"}
    for name in ("vpv1", "vpv2", "vpv3"):
        assert (loaded[name].ha_device_class, loaded[name].ha_state_class, loaded[name].ha_entity_category) == (
            "", "", "",
        ), name
    assert loaded["soc"].ha_device_class == "battery"


def test_csv_without_the_columns_at_all_still_loads(tmp_path: Path) -> None:
    """Every existing protocol file (no HA columns) must keep working unchanged."""
    folder: Path = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True)
    (folder / "acme_x.json").write_text(json.dumps({"transport": "modbus_tcp"}), encoding="utf-8")
    path: Path = folder / "acme_x.input_registry_map.csv"
    path.write_text("register,variable_name,documented_name,unit\n1,vpv1,Vpv1,V\n", encoding="utf-8")

    entry: dict[str, Any] = scanner._parse_protocol_csv(path, "acme")[0] # type: ignore
    assert [entry[c] for c in HA_COLUMNS] == ["", "", ""]
    ps = protocol_settings("acme_x", settings_dir=str(tmp_path / "protocols"))
    assert next(e for g in ps.registry_map.values() for e in g).ha_device_class == ""


# ---------------------------------------------------------------------------
# the analysis page appending a register
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("header", "delimiter"), [(COMMA_HEADER, ","), (SEMI_HEADER, ";")], ids=["comma", "semicolon"])
def test_register_added_by_analysis_has_blank_ha_cells_and_loads_everywhere(
    db: Session, tmp_path: Path, header: str, delimiter: str
) -> None:
    """The analysis page knows nothing about HA columns; the row it appends must still be valid."""
    path: Path = _csv(tmp_path, header, delimiter)

    changed, count = _apply_protocol_changes(path, [AnalysisChange(
        protocol_name="acme_x.input_registry_map", registry_type="input", action="add", register_address="99",
    )])
    assert (changed, count) == (True, 1)

    added: dict[str, Any] = next(e for e in scanner._parse_protocol_csv(path, "acme") if e["register_address"] == "99") # type: ignore
    assert [added[c] for c in HA_COLUMNS] == ["", "", ""]

    ps = protocol_settings("acme_x", settings_dir=str(tmp_path / "protocols"))
    assert any(e.register == 99 for g in ps.registry_map.values() for e in g)

    rows: dict[str, ProtocolRegister] = {r.register_address: r for r in _scan(db, path)}
    assert [getattr(rows["99"], c) for c in HA_COLUMNS] == ["", "", ""]  # '' (loaded, blank) — not NULL


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("column", HA_COLUMNS)
@pytest.mark.parametrize("blank", ["", "   ", "\t", None])
def test_validator_accepts_any_blank_for_every_column_and_registry_type(column: str, blank: str | None) -> None:
    """Blank is valid for every column and every registry type, whatever the stored value was."""
    for registry_type in ("input", "holding", "coil", "discrete", "other", "json"):
        assert validate_ha_value(column, blank, registry_type=registry_type) == ""
        assert validate_ha_value(column, blank, registry_type=registry_type, current="legacy_value") == ""


def test_editor_dropdown_left_on_auto_is_accepted_and_commits_blank(db: Session, tmp_path: Path) -> None:
    """Choosing the blank option (including to clear a value) stages a blank, and the commit writes an empty cell."""
    path: Path = _csv(tmp_path, COMMA_HEADER, ",")
    rows: dict[str, ProtocolRegister] = {r.variable_name: r for r in _scan(db, path)}
    soc: ProtocolRegister = rows["soc"]
    assert soc.ha_device_class == "battery"

    for column in HA_COLUMNS:
        result: dict[str, Any] = protocols.update_register_field(
            soc.id, protocols.FieldUpdateRequest(field=column, value=""), db
        )
        assert result["value"] == ""
    assert soc.is_dirty is True

    config_writer._write_protocol_csvs(db, tmp_path / "protocols") # type: ignore

    text = path.read_text(encoding="utf-8")
    assert "None" not in text
    reparsed: dict[Any, dict[str, Any]] = {e["variable_name"]: e for e in scanner._parse_protocol_csv(path, "acme")} # type: ignore
    assert [reparsed["soc"][c] for c in HA_COLUMNS] == ["", "", ""]  # the cleared value really is gone from the file


# ---------------------------------------------------------------------------
# Create Protocol wizard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "protocol_type", ["input", "holding", "coil", "discrete", "other"]
)
def test_wizard_accepts_rows_with_blank_or_omitted_ha_cells(
    protocol_type: str,
) -> None:
    """A wizard row the user left untouched (fields omitted, or sent as '') is valid for every protocol type."""
    omitted = pages.CreateProtocolRequest(
        manufacturer="acme",
        protocol_name="x",
        protocol_type=protocol_type,
        rows=[
            pages.CreateProtocolRowInput(register="1", variable_name="v")
        ],  # Explicit type instantiation
    )

    explicit = pages.CreateProtocolRequest(
        manufacturer="acme",
        protocol_name="x",
        protocol_type=protocol_type,
        rows=[
            pages.CreateProtocolRowInput(
                register="1",
                variable_name="v",
                ha_device_class="",
                ha_state_class="  ",
                ha_entity_category="",
            )
        ],  # Explicit type instantiation
    )

    for request in (omitted, explicit):
        assert [getattr(request.rows[0], c) for c in HA_COLUMNS] == [
            "",
            "",
            "",
        ]



# ---------------------------------------------------------------------------
# database constraints
# ---------------------------------------------------------------------------
def test_database_accepts_null_and_empty_ha_cells_despite_the_not_null_tightening(db: Session) -> None:
    """Migration 0006 made other flag columns NOT NULL; the HA columns must stay nullable."""
    con: sqlite3.Connection = sqlite3.connect(db.get_bind().url.database)  # type: ignore[union-attr]
    base = ("INSERT INTO protocol_registers (protocol_group, protocol_name, registry_type, register_address,"
            " variable_name, documented_name{cols}) VALUES ('g','p','input','{addr}','v{addr}','V'{vals})")
    con.execute(base.format(cols="", vals="", addr="1"))  # HA columns omitted entirely -> NULL
    con.execute(base.format(cols=", ha_device_class, ha_state_class, ha_entity_category",
                            vals=", '', '', ''", addr="2"))  # explicit empty strings
    con.commit()
    rows: list[Any] = con.execute("SELECT register_address, ha_device_class FROM protocol_registers ORDER BY 1").fetchall()
    con.close()
    assert rows == [("1", None), ("2", "")]


def test_null_cells_read_back_through_the_service_as_blank(db: Session, tmp_path: Path) -> None:
    """A NULL (never-loaded) cell is presented as blank, so the template and exports never see None."""
    from classes.WebServer.services import protocol_service

    path: Path = _csv(tmp_path, COMMA_HEADER, ",")
    rows: list[ProtocolRegister] = _scan(db, path)
    for row in rows:
        row.ha_device_class = row.ha_state_class = row.ha_entity_category = None
    db.commit()

    views: str | int | list[protocols.DeviceRegisterView] = protocol_service.get_protocol_registers(db, "acme_x.input_registry_map", "input")["rows"]
    exported: list[dict[str, str | bool]] = protocol_service.export_protocol_registers(db, "acme_x.input_registry_map", "input")

    assert isinstance(views, list)
    assert all((v.ha_device_class, v.ha_state_class, v.ha_entity_category) == ("", "", "") for v in views)
    assert all((r["ha_device_class"], r["ha_state_class"], r["ha_entity_category"]) == ("", "", "") for r in exported)


# ---------------------------------------------------------------------------
# the MQTT bridge
# ---------------------------------------------------------------------------
def test_bridge_infers_from_the_unit_when_every_ha_cell_is_blank(dummy_settings: type[DummySettings], tmp_path: Path) -> None:
    """Blank means 'infer', so discovery for the blank rows still yields a typed, classed entity."""
    path = _csv(tmp_path, COMMA_HEADER, ",")
    assert path.exists()
    client = MagicMock()
    client.publish.return_value = MagicMock(rc=0)
    with patch("classes.transports.mqtt.MQTTClient", return_value=client):
        out = mqtt(dummy_settings(host="b", username="u", password="p"))  # noqa: S106
    out._DISCOVERY_THROTTLE = 0.0 # type: ignore

    src = transport_base(dummy_settings(
        name="transport.src", device_serial_number="SN1", device_name="Inv", device_manufacturer="A", device_model="X",
    ))
    src.protocolSettings = protocol_settings("acme_x", settings_dir=str(tmp_path / "protocols"))
    out.mqtt_discovery(src)

    configs: dict[str, dict[str, Any]] = {}
    for call in client.publish.call_args_list:
        topic, payload = call.args[0], call.args[1]
        if topic.endswith("/config") and payload:
            configs[topic.split("/")[3]] = json.loads(payload)
    for name in ("vpv1", "vpv2", "vpv3"):  # blank in three different ways
        assert (configs[name]["device_class"], configs[name]["state_class"], configs[name]["unit_of_measurement"]) == (
            "voltage", "measurement", "V",
        ), name
        assert "entity_category" not in configs[name]
    assert configs["soc"]["device_class"] == "battery"  # the one explicit cell is honoured
    assert Registry_Type.INPUT in src.protocolSettings.registry_map
