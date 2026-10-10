# Description: Guards that the migrated database schema matches the SQLAlchemy models, and that migration 0006 upgrades existing databases safely.
# File: test_db_schema.py
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

"""Schema-drift tests for the web UI staging database.

Migration 0001 declared columns with ``default=...`` (an ORM-only default) and no
``nullable=False``, so the migrated schema drifted from the models. Migration 0006
resolved that; these tests keep it resolved and prove the upgrade is safe on a
database that already holds data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, Inspector, create_engine, inspect
from sqlalchemy.engine.interfaces import ReflectedIndex

from classes.WebServer.models import AppState, Base, ProtocolRegister, Setting

WEBSERVER_DIR: Path = Path(__import__("classes.WebServer.models", fromlist=["x"]).__file__).resolve().parent

# table -> {column: (value after NULL backfill, SQL default expected on insert)}
EXPECTED_BACKFILL: dict[str, dict[str, object]] = {
    "settings": {"transport_type": "general", "is_active": 1, "is_dirty": 0, "is_orphan": 0},
    "protocol_registers": {"write_mode_protocol": "R", "is_dirty": 0},
    "device_protocol_selections": {
        "user_write_enabled": 0, "mask_enabled": 0, "screen_enabled": 0,
        "user_write_enabled_disk": 0, "mask_enabled_disk": 0, "screen_enabled_disk": 0, "is_dirty": 0,
    },
    "config_backups": {"trigger": "manual"},
    "app_state": {
        "has_dirty_settings": 0, "has_dirty_protocols": 0, "has_orphans": 0,
        "dirty_settings_count": 0, "dirty_protocols_count": 0, "orphan_count": 0, "scanner_status": "idle",
    },
    "setting_descriptions": {"is_dirty": 0},
}


def _cfg(db_path: Path) -> Config:
    ini: Path = WEBSERVER_DIR / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(WEBSERVER_DIR / "migrations"))
    return cfg


def _schema_objects(db_path: Path) -> dict[str, tuple[set[str], set[str]]]:
    """table -> (index names, unique-constraint names)."""
    engine: Engine = create_engine(f"sqlite:///{db_path}")
    try:
        insp: Inspector = inspect(engine)
        return {
            t: (
                {i["name"] for i in insp.get_indexes(t) if i["name"]},
                {u["name"] for u in insp.get_unique_constraints(t) if u["name"]},
            )
            for t in insp.get_table_names() if t != "alembic_version"
        }
    finally:
        engine.dispose()


def _not_null(db_path: Path, table: str, column: str) -> bool:
    con: sqlite3.Connection = sqlite3.connect(db_path)
    try:
        return any(r[1] == column and r[3] == 1 for r in con.execute(f"PRAGMA table_info({table})"))  # noqa: S608
    finally:
        con.close()


def _populate_with_nulls(db_path: Path) -> None:
    """Insert one row per affected table with every to-be-tightened column NULL."""
    con: sqlite3.Connection = sqlite3.connect(db_path)
    con.execute("INSERT INTO settings (section, key, transport_type, is_active, is_dirty, is_orphan)"
                " VALUES ('s','k',NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO protocol_registers (protocol_group, protocol_name, registry_type, register_address,"
                " variable_name, documented_name, write_mode_protocol, is_dirty, ha_device_class)"
                " VALUES ('g','p','input','1','v','V',NULL,NULL,'voltage')")
    con.execute("INSERT INTO device_protocol_selections (device_name, protocol_name, registry_type, register_address,"
                " user_write_enabled, mask_enabled, screen_enabled, user_write_enabled_disk, mask_enabled_disk,"
                " screen_enabled_disk, is_dirty) VALUES ('d','p','input','1',NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO config_backups (filepath, trigger) VALUES ('/x',NULL)")
    con.execute("INSERT INTO app_state (id, has_dirty_settings, has_dirty_protocols, has_orphans,"
                " dirty_settings_count, dirty_protocols_count, orphan_count, scanner_status)"
                " VALUES (1,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO setting_descriptions (key, is_dirty) VALUES ('alpha', NULL)")
    con.commit()
    con.close()


@pytest.fixture
def old_db(tmp_path: Path) -> Path:
    """A database at revision 0005 (the pre-0006 shape) holding NULL-flag rows."""
    path: Path = tmp_path / "old.sqlite"
    command.upgrade(_cfg(path), "0005_ha_columns")
    _populate_with_nulls(path)
    return path


# ---------------------------------------------------------------------------
# the drift guard
# ---------------------------------------------------------------------------
def test_migrated_schema_matches_models_exactly(tmp_path: Path) -> None:
    """Strict model-vs-database comparison (types AND server defaults) must be empty.

    This is the permanent guard: adding or changing a model without a migration, or
    writing a migration that does not produce what the model declares, fails here.
    """
    path: Path = tmp_path / "fresh.sqlite"
    cfg: Config = _cfg(path)
    command.upgrade(cfg, "head")

    engine: Engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        diffs: Any = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True}),
            Base.metadata,
        )
    engine.dispose()

    assert diffs == []
    command.check(cfg)  # env.py now compares types and server defaults too


def test_every_model_table_is_created_by_migrations(tmp_path: Path) -> None:
    """main.py also calls Base.metadata.create_all() after migrating; that must stay a no-op.

    If a model table were missing from the migrations, create_all would quietly create it
    on startup and hide the omission (then a later migration would fail on 'table exists').
    """
    path: Path = tmp_path / "fresh.sqlite"
    command.upgrade(_cfg(path), "head")
    migrated: set[str] = set(_schema_objects(path))

    assert migrated == {t.name for t in Base.metadata.sorted_tables}


# ---------------------------------------------------------------------------
# upgrading a populated database
# ---------------------------------------------------------------------------
def test_upgrade_backfills_nulls_with_documented_defaults(old_db: Path) -> None:
    """Pre-existing NULLs become the column's default, not a failed migration."""
    command.upgrade(_cfg(old_db), "head")

    con: sqlite3.Connection = sqlite3.connect(old_db)
    for table, expected in EXPECTED_BACKFILL.items():
        columns: str = ", ".join(expected)
        row: Any = con.execute(f"SELECT {columns} FROM {table}").fetchone()  # noqa: S608
        assert dict(zip(expected, row, strict=True)) == expected, table
    con.close()


def test_upgrade_enforces_not_null_and_adds_database_defaults(old_db: Path) -> None:
    """After 0006 a NULL is rejected, and an insert that omits the columns gets the defaults."""
    command.upgrade(_cfg(old_db), "head")

    for table, expected in EXPECTED_BACKFILL.items():
        for column in expected:
            assert _not_null(old_db, table, column), f"{table}.{column} should be NOT NULL"

    con: sqlite3.Connection = sqlite3.connect(old_db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE settings SET is_active = NULL")
    # a raw insert that omits every flag (what an ORM-less code path would do)
    con.execute("INSERT INTO settings (section, key) VALUES ('s2','k2')")
    con.execute("INSERT INTO config_backups (filepath) VALUES ('/y')")
    row: Any = con.execute(
        "SELECT transport_type, is_active, is_dirty, is_orphan FROM settings WHERE section='s2'"
    ).fetchone()
    assert row == ("general", 1, 0, 0)
    assert con.execute("SELECT trigger FROM config_backups WHERE filepath='/y'").fetchone() == ("manual",)
    con.close()


def test_upgrade_keeps_data_indexes_and_unique_constraints(old_db: Path) -> None:
    """SQLite rebuilds each table to alter it; nothing may be lost in the rebuild."""
    before: dict[str, tuple[set[str], set[str]]] = _schema_objects(old_db)
    con: sqlite3.Connection = sqlite3.connect(old_db)
    counts_before: dict[str, Any] = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}  # noqa: S608
    con.close()

    command.upgrade(_cfg(old_db), "head")

    after: dict[str, tuple[set[str], set[str]]] = _schema_objects(old_db)
    con: sqlite3.Connection = sqlite3.connect(old_db)
    counts_after: dict[str, Any] = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in after}  # noqa: S608
    ha: Any = con.execute("SELECT ha_device_class FROM protocol_registers").fetchone()
    con.close()

    assert counts_after == counts_before
    assert ha == ("voltage",)  # migration 0005's column survives the 0006 rebuild
    for table in before:
        if table == "setting_descriptions":
            continue  # deliberately re-arranged; asserted separately below
        assert after[table] == before[table], f"{table}: indexes/constraints changed by the rebuild"
    # the constraints that protect real data are still there
    assert "uq_register_protocol_type_addr" in after["protocol_registers"][1]
    assert "uq_settings_section_key" in after["settings"][1]
    assert "uq_device_protocol_selection" in after["device_protocol_selections"][1]


def test_upgrade_replaces_redundant_key_index_with_one_unique_index(old_db: Path) -> None:
    """setting_descriptions.key: one unique index (as modelled), uniqueness still enforced."""
    command.upgrade(_cfg(old_db), "head")

    engine: Engine = create_engine(f"sqlite:///{old_db}")
    insp: Inspector = inspect(engine)
    key_indexes: list[ReflectedIndex] = [i for i in insp.get_indexes("setting_descriptions") if i["column_names"] == ["key"]]
    constraints: list[str | None] = [u["name"] for u in insp.get_unique_constraints("setting_descriptions")]
    engine.dispose()

    assert [(i["name"], bool(i["unique"])) for i in key_indexes] == [("ix_setting_descriptions_key", True)]
    assert "uq_setting_descriptions_key" not in constraints
    con: sqlite3.Connection = sqlite3.connect(old_db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO setting_descriptions (key) VALUES ('alpha')")  # duplicate of the existing row
    con.close()


def test_upgrade_tolerates_a_developer_database_whose_key_index_is_already_unique(tmp_path: Path) -> None:
    """Migration 0002 notes 'local developer database name variance'; 0006 must not assume the exact 0001 shape."""
    path: Path = tmp_path / "variant.sqlite"
    command.upgrade(_cfg(path), "0005_ha_columns")
    con: sqlite3.Connection = sqlite3.connect(path)
    con.execute("DROP INDEX ix_setting_descriptions_key")
    con.execute("CREATE UNIQUE INDEX ix_setting_descriptions_key ON setting_descriptions (key)")
    con.execute("INSERT INTO setting_descriptions (key, is_dirty) VALUES ('alpha', NULL)")
    con.commit()
    con.close()

    command.upgrade(_cfg(path), "head")

    engine: Engine = create_engine(f"sqlite:///{path}")
    insp: Inspector = inspect(engine)
    key_indexes: list[ReflectedIndex] = [i for i in insp.get_indexes("setting_descriptions") if i["column_names"] == ["key"]]
    constraints: list[str | None] = [u["name"] for u in insp.get_unique_constraints("setting_descriptions")]
    engine.dispose()
    assert [(i["name"], bool(i["unique"])) for i in key_indexes] == [("ix_setting_descriptions_key", True)]
    assert "uq_setting_descriptions_key" not in constraints


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------
def test_downgrade_restores_previous_shape_keeps_data_and_reupgrades(old_db: Path) -> None:
    """0006 is reversible: columns nullable again, key still unique, data intact, and it can run again."""
    cfg: Config = _cfg(old_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005_ha_columns")

    for table, expected in EXPECTED_BACKFILL.items():
        for column in expected:
            assert not _not_null(old_db, table, column), f"{table}.{column} should be nullable again"
    engine: Engine = create_engine(f"sqlite:///{old_db}")
    insp: Inspector = inspect(engine)
    assert "uq_setting_descriptions_key" in [u["name"] for u in insp.get_unique_constraints("setting_descriptions")]
    engine.dispose()
    con: sqlite3.Connection = sqlite3.connect(old_db)
    assert con.execute("SELECT count(*) FROM settings").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO setting_descriptions (key) VALUES ('alpha')")  # uniqueness survives the downgrade
    con.close()

    command.upgrade(cfg, "head")  # the cycle is repeatable
    assert _not_null(old_db, "settings", "is_active")


# ---------------------------------------------------------------------------
# through the application's own startup path
# ---------------------------------------------------------------------------
def test_app_startup_path_and_orm_work_on_an_upgraded_database(old_db: Path) -> None:
    """database.run_migrations() (what main.py calls) then ordinary ORM use, on a formerly-NULL database."""
    from sqlalchemy.orm import sessionmaker

    from classes.WebServer.database import run_migrations
    from classes.WebServer.models import AppState, ProtocolRegister, Setting

    run_migrations(old_db, WEBSERVER_DIR / "alembic.ini")
    # main.py runs create_all() right after migrating; it must change nothing now.
    engine: Engine = create_engine(f"sqlite:///{old_db}")
    Base.metadata.create_all(bind=engine)

    session = sessionmaker(bind=engine)()
    try:
        # backfilled values come back as real Python types, not None
        setting: Setting = session.query(Setting).one()
        assert (setting.is_active, setting.is_dirty, setting.transport_type) == (True, False, "general")
        state: AppState | None = session.get(AppState, 1)
        assert state is not None
        assert (state.has_dirty_settings, state.dirty_settings_count, state.scanner_status) == (False, 0, "idle")
        register: ProtocolRegister = session.query(ProtocolRegister).one()
        assert (register.write_mode_protocol, register.is_dirty, register.ha_device_class) == ("R", False, "voltage")

        # and new rows still insert through the ORM (Python defaults) as before
        session.add(Setting(section="s3", key="k3"))
        session.commit()
        added: Setting = session.query(Setting).filter_by(section="s3").one()
        assert (added.is_active, added.is_dirty, added.transport_type) == (True, False, "general")
    finally:
        session.close()
        engine.dispose()

    # create_all() did not add or alter anything
    assert set(_schema_objects(old_db)) == {t.name for t in Base.metadata.sorted_tables}
