# Description: Tests for the web UI, database and migration support of the optional Home Assistant protocol-CSV columns.
# File: test_webui_ha_columns.py
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

"""Tests for ``ha device class`` / ``ha state class`` / ``ha entity category`` in the web UI stack.

These use a real SQLite database created by the real Alembic migrations, real CSV
files and the real parsers — the failure modes being guarded against (a commit
stripping the columns from a CSV, a dirty row overwriting real values with blanks,
a migration that cannot run on an existing or a fresh database) only show up
against real files and a real schema.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.orm import Session, sessionmaker

from classes.ha_metadata import (
    HA_COLUMN_LABELS,
    HA_COLUMN_TITLES,
    HA_COLUMNS,
    ha_dropdown_options,
    validate_ha_value,
)
from classes.WebServer import config_writer, scanner
from classes.WebServer.models import ProtocolRegister
from classes.WebServer.routers import pages, protocols
from classes.WebServer.services import protocol_service
from classes.WebServer.services.protocol_service import DeviceRegisterView

if TYPE_CHECKING:
    from collections.abc import Iterator

WEBSERVER_DIR: Path = Path(scanner.__file__).resolve().parent
TEMPLATES_DIR: Path = WEBSERVER_DIR / "templates"

CSV_HEADER = (
    "register,variable_name,documented_name,unit,data_type,values,"
    "ha_device_class,ha_state_class,ha_entity_category\n"
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _alembic_config(db_path: Path) -> Config:
    ini: Path = WEBSERVER_DIR / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(WEBSERVER_DIR / "migrations"))
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Session]:
    """A session on a SQLite database built by the real migrations (head)."""
    db_path: Path = tmp_path / "mpg.sqlite"
    command.upgrade(_alembic_config(db_path), "head")
    engine: Engine = create_engine(f"sqlite:///{db_path}")
    session: Session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _write_csv(tmp_path: Path, body: str, name: str = "acme_x.input_registry_map.csv") -> Path:
    folder: Path = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True, exist_ok=True)
    path: Path = folder / name
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def _scan_into(db: Session, csv_path: Path) -> list[ProtocolRegister]:
    rows: list[ProtocolRegister] = []
    for entry in scanner._parse_protocol_csv(csv_path, "acme"): # type: ignore
        row: ProtocolRegister | None = scanner._upsert_protocol_register(db, entry) # type: ignore
        assert row is not None
        rows.append(row)
    db.commit()
    return rows


def _view(i: int, name: str, **overrides: Any) -> DeviceRegisterView:
    base: dict[str, Any] = {
        "id": i, "protocol_name": "p", "registry_type": "input", "register_address": str(i),
        "variable_name": name, "documented_name": name, "unit": "V", "data_type": "USHORT",
        "values_range": "0-65535", "adjustments": {}, "note": "", "read_interval": "",
        "write_mode_protocol": "R", "user_write_enabled": False, "mask_enabled": False,
        "screen_enabled": False, "is_dirty": False,
    }
    base.update(overrides)
    return DeviceRegisterView(**base)


# ---------------------------------------------------------------------------
# migration 0005
# ---------------------------------------------------------------------------
def test_migration_upgrade_keeps_existing_rows_with_null_ha_columns_and_downgrades(tmp_path: Path) -> None:
    """An existing database keeps its rows (HA cells NULL = 'never loaded'), and 0005 reverses cleanly."""
    db_path: Path = tmp_path / "old.sqlite"
    cfg: Config = _alembic_config(db_path)
    command.upgrade(cfg, "0004_pending_delete")
    con: sqlite3.Connection = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO protocol_registers (protocol_group, protocol_name, registry_type, register_address,"
        " variable_name, documented_name, write_mode_protocol, is_dirty, is_synthetic, is_json_desc,"
        " pending_delete) VALUES ('g','p','input','1','vpv1','Vpv1','R',1,0,0,0)"
    )
    con.commit()
    con.close()

    # This test is about migration 0005 specifically, so target it by name — "head"
    # moves on every time a migration is added (0006 backfills NULL flags etc.).
    command.upgrade(cfg, "0005_ha_columns")
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT variable_name, is_dirty, ha_device_class, ha_state_class, ha_entity_category"
        " FROM protocol_registers"
    ).fetchone()
    version = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    con.close()
    assert row == ("vpv1", 1, None, None, None)  # data intact; NULL, not "", for pre-existing rows
    assert version == "0005_ha_columns"

    command.downgrade(cfg, "0004_pending_delete")
    con = sqlite3.connect(db_path)
    columns: set[Any] = {r[1] for r in con.execute("PRAGMA table_info(protocol_registers)")}
    rows_left = con.execute("SELECT count(*) FROM protocol_registers").fetchone()[0]
    con.close()
    assert not columns & set(HA_COLUMNS)
    assert rows_left == 1


def test_migration_fresh_install_reaches_head_and_matches_model(db: Session) -> None:
    """A brand-new database (all migrations from scratch) has exactly the columns the model declares."""
    db_columns: dict[str, ReflectedColumn] = {c["name"]: c for c in inspect(db.get_bind()).get_columns("protocol_registers")}
    for column in HA_COLUMNS:
        assert column in db_columns
        assert db_columns[column]["nullable"] is True
        assert hasattr(ProtocolRegister, column)


# ---------------------------------------------------------------------------
# scanner: CSV -> DB
# ---------------------------------------------------------------------------
def test_scanner_parses_ha_columns_lowercased_and_blank_when_absent(tmp_path: Path) -> None:
    """Values are normalized; a CSV without the columns yields blanks, not KeyErrors."""
    path: Path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,Voltage,MEASUREMENT,Diagnostic\n")
    entry = scanner._parse_protocol_csv(path, "acme")[0] # type: ignore
    assert (entry["ha_device_class"], entry["ha_state_class"], entry["ha_entity_category"]) == (
        "voltage", "measurement", "diagnostic",
    )

    legacy: Path = tmp_path / "protocols" / "acme" / "acme_x.holding_registry_map.csv"
    legacy.write_text("register,variable_name,documented_name,unit\n1,limit,Limit,A\n", encoding="utf-8")
    entry: dict[str, Any] = scanner._parse_protocol_csv(legacy, "acme")[0] # type: ignore
    assert (entry["ha_device_class"], entry["ha_state_class"], entry["ha_entity_category"]) == ("", "", "")


def test_scanner_accepts_space_separated_header_names(tmp_path: Path) -> None:
    """'ha device class' (as documented) and 'ha_device_class' (as the editor writes) are equivalent."""
    folder = tmp_path / "protocols" / "acme"
    folder.mkdir(parents=True)
    path = folder / "acme_x.input_registry_map.csv"
    path.write_text(
        "register;variable name;documented name;unit;ha device class\n1;soc;Soc;%;battery\n", encoding="utf-8"
    )
    assert scanner._parse_protocol_csv(path, "acme")[0]["ha_device_class"] == "battery" # type: ignore


def test_scanner_paired_registers_inherit_ha_values_from_high_half(tmp_path: Path) -> None:
    """If only the _h half carries a value, the merged register still gets it."""
    path = _write_csv(
        tmp_path,
        "1,energy_l,Energy,kWh,,0-65535,,,\n2,energy_h,Energy,kWh,,0-65535,energy,total_increasing,\n",
    )
    entries: list[dict[str, Any]] = scanner._parse_protocol_csv(path, "acme") # type: ignore
    merged: list[dict[str, Any]] = [e for e in entries if e["variable_name"] == "energy"]
    assert merged
    assert (merged[0]["ha_device_class"], merged[0]["ha_state_class"]) == ("energy", "total_increasing")


def test_upsert_inserts_and_refreshes_clean_rows_from_csv(db: Session, tmp_path: Path) -> None:
    """A non-dirty row always mirrors the CSV, including a value cleared in the file."""
    path: Path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,voltage,measurement,\n")
    row: ProtocolRegister = _scan_into(db, path)[0]
    assert (row.ha_device_class, row.ha_state_class, row.ha_entity_category) == ("voltage", "measurement", "")

    path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,diagnostic\n")
    path.write_text(CSV_HEADER + "1,vpv1,Vpv1,V,,0-65535,,,diagnostic\n", encoding="utf-8")
    row = _scan_into(db, path)[0]
    assert (row.ha_device_class, row.ha_state_class, row.ha_entity_category) == ("", "", "diagnostic")


def test_upsert_fills_never_loaded_null_cells_even_on_dirty_rows(db: Session, tmp_path: Path) -> None:
    """The data-loss guard: a dirty row that predates the migration must not commit blanks over real CSV values."""
    path: Path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,voltage,measurement,diagnostic\n")
    row: ProtocolRegister = _scan_into(db, path)[0]
    # Simulate a pre-migration row with an uncommitted edit: HA cells NULL, unit edited, dirty.
    row.ha_device_class = row.ha_state_class = row.ha_entity_category = None
    row.unit = "mV"
    row.is_dirty = True
    db.commit()

    row = _scan_into(db, path)[0]

    assert row.unit == "mV"  # the staged edit survives the rescan
    assert row.is_dirty is True
    assert (row.ha_device_class, row.ha_state_class, row.ha_entity_category) == (
        "voltage", "measurement", "diagnostic",
    )


def test_upsert_keeps_staged_ha_edit_on_dirty_row(db: Session, tmp_path: Path) -> None:
    """A dirty row's *loaded* HA value is a staged edit and must not be reverted by a rescan."""
    path: Path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,voltage,,\n")
    row: ProtocolRegister = _scan_into(db, path)[0]
    row.ha_device_class = "power"  # user changed the dropdown, not yet committed
    row.is_dirty = True
    db.commit()

    row = _scan_into(db, path)[0]

    assert row.ha_device_class == "power"


# ---------------------------------------------------------------------------
# commit path: DB -> CSV (the writer regenerates the whole file)
# ---------------------------------------------------------------------------
def test_commit_preserves_ha_columns_in_csv_and_runtime_loader_sees_them(db: Session, tmp_path: Path) -> None:
    """Regression: the writer used a fixed column list, so any commit stripped the HA columns."""
    from classes.protocol_settings import protocol_settings

    csv_path: Path = _write_csv(
        tmp_path,
        "1,vpv1,Vpv1,V,,0-65535,voltage,measurement,\n2,soc,Soc,%,,0-100,battery,,diagnostic\n3,plain,Plain,,,0-1,,,\n",
    )
    (csv_path.parent / "acme_x.json").write_text(json.dumps({"transport": "modbus_tcp"}), encoding="utf-8")
    rows: list[ProtocolRegister] = _scan_into(db, csv_path)
    rows[0].documented_name = "Edited name"  # any edit marks the protocol dirty
    rows[0].is_dirty = True
    db.commit()

    written: int = config_writer._write_protocol_csvs(db, tmp_path / "protocols") # type: ignore

    assert written == 1
    header: list[str] = csv_path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert {"ha_device_class", "ha_state_class", "ha_entity_category"} <= set(header)
    reparsed: dict[Any, dict[str, Any]] = {e["variable_name"]: e for e in scanner._parse_protocol_csv(csv_path, "acme")} # type: ignore
    assert (reparsed["vpv1"]["ha_device_class"], reparsed["vpv1"]["ha_state_class"]) == ("voltage", "measurement")
    assert (reparsed["soc"]["ha_device_class"], reparsed["soc"]["ha_entity_category"]) == ("battery", "diagnostic")
    assert reparsed["plain"]["ha_device_class"] == ""

    # and the MQTT bridge's own loader reads exactly the same values back
    ps = protocol_settings("acme_x", settings_dir=str(tmp_path / "protocols"))
    entries: dict[str, protocol_service.registry_map_entry] = {e.variable_name: e for group in ps.registry_map.values() for e in group}
    assert (entries["vpv1"].ha_device_class, entries["vpv1"].ha_state_class) == ("voltage", "measurement")
    assert entries["soc"].ha_entity_category == "diagnostic"


def test_commit_writes_blank_for_null_cells_not_the_string_none(db: Session, tmp_path: Path) -> None:
    """NULL (never loaded) must serialize as an empty cell."""
    csv_path: Path = _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,\n")
    row: ProtocolRegister = _scan_into(db, csv_path)[0]
    row.ha_device_class = row.ha_state_class = row.ha_entity_category = None
    row.is_dirty = True
    db.commit()

    config_writer._write_protocol_csvs(db, tmp_path / "protocols") # type: ignore

    assert "None" not in csv_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# service layer: editing, validation, export
# ---------------------------------------------------------------------------
def test_update_field_accepts_valid_values_and_normalizes(db: Session, tmp_path: Path) -> None:
    """Dropdown values are stored lower-case and the row is marked dirty by the caller's path."""
    row: ProtocolRegister = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,\n"))[0]

    result: ProtocolRegister | None = protocol_service.update_protocol_register_field(db, row.id, "ha_device_class", "Voltage")
    assert result is not None
    assert result.ha_device_class == "voltage"
    assert protocol_service.update_protocol_register_field(db, row.id, "ha_state_class", "none").ha_state_class == "none"  # type: ignore[union-attr]
    assert protocol_service.update_protocol_register_field(db, row.id, "ha_entity_category", "").ha_entity_category == ""  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ha_state_class", "bogus"),
        ("ha_entity_category", "none"),  # only class columns accept 'none'
        ("ha_device_class", "wind_speed"),
        ("ha_device_class", "outlet"),  # a switch class is not valid on an input register
    ],
)
def test_update_field_rejects_values_the_ui_would_never_offer(
    db: Session, tmp_path: Path, field: str, value: str
) -> None:
    """The dropdown is not the only way in: a hand-built request must not stage an invalid cell."""
    row: ProtocolRegister = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,\n"))[0]
    with pytest.raises(ValueError, match="not a valid"):
        protocol_service.update_protocol_register_field(db, row.id, field, value)
    assert getattr(row, field) in ("", None)  # nothing staged


def test_update_field_allows_unchanged_custom_value_but_not_a_new_one(db: Session, tmp_path: Path) -> None:
    """A hand-edited CSV value outside the curated list stays editable as-is."""
    row: ProtocolRegister = _scan_into(db, _write_csv(tmp_path, "1,wind,Wind,m/s,,0-65535,wind_speed,,\n"))[0]
    assert row.ha_device_class == "wind_speed"

    again: ProtocolRegister | None = protocol_service.update_protocol_register_field(db, row.id, "ha_device_class", "wind_speed")
    assert again is not None
    with pytest.raises(ValueError, match="not a valid"):
        protocol_service.update_protocol_register_field(db, row.id, "ha_device_class", "gas_speed")


def test_update_field_unknown_field_still_returns_none(db: Session, tmp_path: Path) -> None:
    """Whitelist behavior is unchanged for non-HA fields."""
    row: ProtocolRegister = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,\n"))[0]
    assert protocol_service.update_protocol_register_field(db, row.id, "not_a_field", "x") is None


def test_route_returns_422_for_invalid_value_and_404_for_missing(db: Session, tmp_path: Path) -> None:
    """An invalid value is a 422 with the reason — not the misleading 404 'not found'."""
    row: ProtocolRegister = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,,,\n"))[0]

    with pytest.raises(HTTPException) as bad:
        protocols.update_register_field(
            row.id, protocols.FieldUpdateRequest(field="ha_state_class", value="bogus"), db
        )
    assert bad.value.status_code == 422
    assert "not a valid HA State" in str(bad.value.detail)

    with pytest.raises(HTTPException) as missing:
        protocols.update_register_field(
            99999, protocols.FieldUpdateRequest(field="ha_state_class", value="measurement"), db
        )
    assert missing.value.status_code == 404

    ok: dict[str, Any] = protocols.update_register_field(
        row.id, protocols.FieldUpdateRequest(field="ha_state_class", value="measurement"), db
    )
    assert ok["value"] == "measurement"
    assert ok["is_dirty"] is True


def test_export_includes_ha_columns_with_blanks_for_null(db: Session, tmp_path: Path) -> None:
    """CSV/JSON export carries the new columns (NULL exports as an empty string)."""
    rows: list[ProtocolRegister] = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,voltage,measurement,\n2,x,X,,,0-1,,,\n"))
    rows[1].ha_device_class = None
    db.commit()

    exported: list[dict[str, str | bool]] = protocol_service.export_protocol_registers(db, "acme_x.input_registry_map", "input")

    by_name: dict[str | bool, dict[str, str | bool]] = {r["variable_name"]: r for r in exported}
    assert by_name["vpv1"]["ha_device_class"] == "voltage"
    assert by_name["vpv1"]["ha_state_class"] == "measurement"
    assert by_name["x"]["ha_device_class"] == ""


def test_registers_view_exposes_ha_values(db: Session, tmp_path: Path) -> None:
    """The editor/device table gets the values via DeviceRegisterView, defaulting NULL to blank."""
    rows: list[ProtocolRegister] = _scan_into(db, _write_csv(tmp_path, "1,vpv1,Vpv1,V,,0-65535,voltage,,diagnostic\n"))
    rows[0].ha_state_class = None
    db.commit()

    page: dict[str, str | int | list[DeviceRegisterView]] = protocol_service.get_protocol_registers(db, "acme_x.input_registry_map", "input")
    views: str | int | list[DeviceRegisterView] = page["rows"]
    assert isinstance(views, list)

    assert (views[0].ha_device_class, views[0].ha_state_class, views[0].ha_entity_category) == (
        "voltage", "", "diagnostic",
    )


# ---------------------------------------------------------------------------
# Create Protocol wizard
# ---------------------------------------------------------------------------
def _wizard(protocol_type: str, **row: str) -> dict[str, Any]:
    return {
        "manufacturer": "acme", "protocol_name": "x", "protocol_type": protocol_type,
        "rows": [{"register": "1", "variable_name": "vpv1", **row}],
    }


def test_wizard_csv_headers_include_ha_columns() -> None:
    """The wizard writes the same columns the editor and loader understand."""
    assert set(HA_COLUMNS) <= set(pages.CREATE_PROTOCOL_CSV_HEADERS)


def test_wizard_request_normalizes_valid_ha_cells() -> None:
    """Valid cells are accepted and lower-cased."""
    req = pages.CreateProtocolRequest(**_wizard("input", ha_device_class="Voltage", ha_entity_category="Diagnostic"))
    assert req.rows[0].ha_device_class == "voltage"
    assert req.rows[0].ha_entity_category == "diagnostic"


def test_wizard_request_validates_device_class_against_protocol_type() -> None:
    """A sensor class on a discrete (binary sensor) protocol is rejected, naming the row."""
    with pytest.raises(ValidationError, match=r"Row 1: .*HA Class"):
        pages.CreateProtocolRequest(**_wizard("discrete", ha_device_class="voltage"))
    pages.CreateProtocolRequest(**_wizard("discrete", ha_device_class="problem"))  # valid there
    pages.CreateProtocolRequest(**_wizard("coil", ha_device_class="outlet"))  # coils may become switches


# ---------------------------------------------------------------------------
# dropdown rendering (protocol_table.html)
# ---------------------------------------------------------------------------
def _render(rows: list[DeviceRegisterView], registry_type: str = "input", device_name: str | None = None,
            with_ha: bool = True) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    ctx: dict[str, Any] = {
        "rows": rows, "protocol_name": "p", "registry_type": registry_type, "device_name": device_name,
    }
    if with_ha:
        ctx.update(
            ha_columns=HA_COLUMNS, ha_labels=HA_COLUMN_LABELS, ha_titles=HA_COLUMN_TITLES,
            ha_options={c: ha_dropdown_options(c, registry_type) for c in HA_COLUMNS},
        )
    return env.get_template("partials/protocol_table.html").render(**ctx)


def _selects(html: str) -> list[str]:
    return re.findall(r"<select.*?</select>", html, re.S)


def test_editor_renders_three_dropdowns_per_row_with_current_value_selected() -> None:
    """Editor mode: a <select> per HA column, the stored value selected, wired to the /field PATCH."""
    html: str = _render([_view(1, "vpv1", ha_device_class="voltage", ha_entity_category="diagnostic")])
    selects: list[str] = _selects(html)
    assert len(selects) == 3
    assert re.findall(r'<option value="([^"]*)" selected', selects[0]) == ["voltage"]
    assert re.findall(r'<option value="([^"]*)" selected', selects[1]) == [""]  # auto
    assert re.findall(r'<option value="([^"]*)" selected', selects[2]) == ["diagnostic"]
    assert sorted(set(re.findall(r"field: '(ha_\w+)'", html))) == sorted(HA_COLUMNS)
    assert 'hx-patch="/api/protocols/1/field"' in selects[0]


def test_editor_headers_sit_between_rw_and_adjustments_and_are_sortable() -> None:
    """Column placement keeps Notes as the last, flexible-width column."""
    html: str = _render([_view(1, "vpv1", ha_device_class="voltage")])
    headers: list[Any] = re.findall(r"<th[^>]*>(.*?)</th>", html)
    assert headers[headers.index("R/W") + 1: headers.index("Adjustments")] == ["HA Class", "HA State", "HA Category"]
    assert headers[-1] == "Notes"
    assert 'data-sort-key="ha_device_class"' in html
    assert 'data-sort-ha_device_class="voltage"' in html  # what the sort JS reads for that key


def test_unknown_stored_value_is_shown_as_custom_not_silently_replaced() -> None:
    """A hand-edited class the curated list lacks must remain visible and selected."""
    html: str = _render([_view(1, "wind", ha_device_class="wind_speed")])
    assert re.findall(r'<option value="([^"]*)" selected>([^<]*)', _selects(html)[0]) == [
        ("wind_speed", "wind_speed (custom)")
    ]


def test_device_view_is_read_only_text() -> None:
    """In a device view (no editing) the values are text and there are no dropdowns."""
    html: str = _render([_view(1, "vpv1", ha_device_class="voltage")], device_name="dev1")
    assert "<select" not in html
    assert "voltage" in html
    assert "auto" in html  # blank state class is shown as 'auto', not an empty cell


def test_json_registries_have_no_ha_columns() -> None:
    """The json pseudo-registry has no per-register Home Assistant attributes."""
    html: str = _render([_view(1, "cfg", registry_type="json")], registry_type="json")
    assert "HA Class" not in html
    assert "<select" not in html


def test_coil_dropdown_offers_binary_sensor_and_switch_classes_but_not_sensor_ones() -> None:
    """Options follow the registry type, so the UI cannot offer a class HA would reject."""
    options: list[str] = [v for v, _ in ha_dropdown_options("ha_device_class", "coil")]
    assert {"problem", "outlet"} <= set(options)
    assert "voltage" not in options
    assert "voltage" in [v for v, _ in ha_dropdown_options("ha_device_class", "holding")]


def test_names_are_html_escaped_and_missing_context_degrades_gracefully() -> None:
    """No injection through a variable name, and the page still renders if the route omits the HA context."""
    html: str = _render([_view(1, "<b>x</b>")])
    assert "<b>x</b>" not in html
    without: str = _render([_view(1, "vpv1")], with_ha=False)
    assert "<select" not in without
    assert "HA Class" not in without


def test_paired_register_detail_rows_and_empty_state_keep_column_counts_aligned() -> None:
    """colspans grow with the extra columns so paired detail rows and the empty row still line up."""
    paired: str = _render([_view(1, "energy", paired_high_address="2", data_type="UINT")])
    assert "set on parent row" in paired
    assert 'colspan="3"' in paired
    assert re.findall(r'colspan="(\d+)" class="px-4 py-6', _render([])) == ["12"]  # 9 + 3 HA columns
    assert re.findall(r'colspan="(\d+)" class="px-4 py-6', _render([], registry_type="json")) == ["8"]


# ---------------------------------------------------------------------------
# validator (shared by the API and the wizard)
# ---------------------------------------------------------------------------
def test_validate_ha_value_rules() -> None:
    """Blank and 'none' semantics differ by column."""
    assert validate_ha_value("ha_device_class", "  None ") == "none"
    assert validate_ha_value("ha_entity_category", "") == ""
    assert validate_ha_value("ha_state_class", "Total_Increasing") == "total_increasing"
    with pytest.raises(ValueError, match="Unknown Home Assistant column"):
        validate_ha_value("unit", "V")
