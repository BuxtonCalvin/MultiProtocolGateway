# Description: Tests that a brand-new database is built from the models at startup (no migrations) while existing databases still migrate.
# File: test_db_bootstrap.py
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

"""Startup schema bootstrap: ``database.ensure_schema``.

A new user's database must come into being from the models alone — no Alembic
migration may run — yet end up identical to one built by the migrations, and be a
valid base for every future migration. An existing database must keep migrating.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Inspector, create_engine, inspect

from classes.WebServer import database
from classes.WebServer.database import database_has_tables, ensure_schema
from classes.WebServer.models import AppState, Base

WEBSERVER_DIR: Path = Path(database.__file__).resolve().parent
INI: Path = WEBSERVER_DIR / "alembic.ini"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    cfg.set_main_option("script_location", str(WEBSERVER_DIR / "migrations"))
    return cfg


def _head() -> str:
    head: str | None = ScriptDirectory.from_config(_cfg(Path("x.db"))).get_current_head()
    assert head is not None
    return head


def _version(db_path: Path) -> str | None:
    con: sqlite3.Connection = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _shape(db_path: Path) -> dict[str, Any]:
    """A comparable description of the whole schema: columns, keys, indexes, constraints."""
    engine: database.Engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        insp: Inspector = inspect(engine)
        shape: dict[str, Any] = {}
        for table in sorted(t for t in insp.get_table_names() if t != "alembic_version"):
            shape[table] = {
                "columns": {
                    c["name"]: (
                        type(c["type"]).__name__,
                        getattr(c["type"], "length", None),
                        c["nullable"],
                        str(c["default"]).strip("'()") if c["default"] is not None else None,
                    )
                    for c in insp.get_columns(table)
                },
                "pk": tuple(insp.get_pk_constraint(table)["constrained_columns"]),
                "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)),
                "uniques": sorted((u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(table)),
                "fks": sorted(str(f["constrained_columns"]) for f in insp.get_foreign_keys(table)),
            }
        return shape
    finally:
        engine.dispose()


@pytest.fixture
def no_migrations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbidden(*_a: Any, **_k: Any) -> None:
        msg = "alembic upgrade/downgrade must not run when creating a new database"
        raise AssertionError(msg)

    monkeypatch.setattr(command, "upgrade", forbidden)
    monkeypatch.setattr(command, "downgrade", forbidden)

    real_stamp: Callable[..., None] = command.stamp

    def fake_stamp(cfg: Any, rev: str, *a: Any, **k: Any) -> Any:
        calls.append(rev)
        return real_stamp(cfg, rev, *a, **k)

    monkeypatch.setattr(command, "stamp", fake_stamp)
    return calls


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_database_has_tables_distinguishes_new_from_existing(tmp_path: Path) -> None:
    """Missing file and empty file are new; any user table makes it existing."""
    path: Path = tmp_path / "db.sqlite"
    assert database_has_tables(path) is False  # no file
    sqlite3.connect(path).close()
    assert database_has_tables(path) is False  # empty file (SQLite creates one on first connect)
    con: sqlite3.Connection = sqlite3.connect(path)
    con.execute("CREATE TABLE something (x INTEGER)")
    con.commit()
    con.close()
    assert database_has_tables(path) is True


# ---------------------------------------------------------------------------
# new database
# ---------------------------------------------------------------------------
def test_new_database_is_built_from_models_without_running_migrations(tmp_path: Path, no_migrations: list[str]) -> None:
    """The fresh path never calls upgrade; it creates every model table and stamps head."""
    path: Path = tmp_path / "data" / "mpg.sqlite"  # parent directory does not exist yet either

    result = ensure_schema(path, INI)

    assert result == "created"
    assert set(_shape(path)) == {t.name for t in Base.metadata.sorted_tables}
    assert no_migrations == ["head"]  # the only Alembic call was the stamp
    assert _version(path) == _head()


def test_model_built_schema_matches_the_models_exactly(tmp_path: Path) -> None:
    """Strict comparison (types and server defaults) of the model-built database against the models."""
    path: Path = tmp_path / "mpg.sqlite"
    ensure_schema(path, INI)

    engine: database.Engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn:
        diffs = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True}),
            Base.metadata,
        )
    engine.dispose()
    assert diffs == []


def test_model_built_schema_is_identical_to_the_migrated_schema(tmp_path: Path) -> None:
    """Every column, nullability, default, key, index and constraint matches what migrating produces.

    This is what makes skipping the migrations safe for new users: both routes must yield
    the same database.
    """
    built: Path = tmp_path / "built.sqlite"
    migrated: Path = tmp_path / "migrated.sqlite"
    ensure_schema(built, INI)
    command.upgrade(_cfg(migrated), "head")

    built_shape, migrated_shape = _shape(built), _shape(migrated)

    assert built_shape.keys() == migrated_shape.keys()
    for table in built_shape:
        assert built_shape[table] == migrated_shape[table], f"table {table} differs between create_all and migrations"


def test_model_built_database_is_a_valid_base_for_migrations(tmp_path: Path) -> None:
    """A later release will migrate users who started on the fresh path: prove that works both ways.

    Downgrading the latest migration and re-applying it exercises real Alembic batch operations
    against tables that create_all (not a migration) made.
    """
    path: Path = tmp_path / "mpg.sqlite"
    ensure_schema(path, INI)
    con: sqlite3.Connection = sqlite3.connect(path)
    con.execute("INSERT INTO settings (section, key) VALUES ('s','k')")
    con.commit()
    con.close()

    cfg: Config = _cfg(path)
    command.downgrade(cfg, "-1")
    assert _version(path) != _head()
    command.upgrade(cfg, "head")

    assert _version(path) == _head()
    con = sqlite3.connect(path)
    assert con.execute("SELECT section, key, is_active FROM settings").fetchall() == [("s", "k", 1)]
    con.close()


def test_new_database_works_with_the_application_models(tmp_path: Path) -> None:
    """The startup code after the schema step (ensure_app_state) runs on a model-built database."""
    from sqlalchemy.orm import sessionmaker

    path: Path = tmp_path / "mpg.sqlite"
    ensure_schema(path, INI)
    engine: database.Engine = create_engine(f"sqlite:///{path.as_posix()}")
    session: database.Session = sessionmaker(bind=engine)()
    try:
        state: AppState = database.ensure_app_state(session)
        assert isinstance(state, AppState)
        assert (state.id, state.scanner_status, state.dirty_settings_count) == (1, "idle", 0)
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# restarts and existing databases
# ---------------------------------------------------------------------------
def test_second_start_is_a_no_op_and_keeps_data(tmp_path: Path) -> None:
    """After the first start the database exists, so the next start goes through the (no-op) migrate path."""
    path: Path = tmp_path / "mpg.sqlite"
    assert ensure_schema(path, INI) == "created"
    con: sqlite3.Connection = sqlite3.connect(path)
    con.execute("INSERT INTO settings (section, key) VALUES ('s','k')")
    con.commit()
    con.close()

    assert ensure_schema(path, INI) == "migrated"

    assert _version(path) == _head()
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM settings").fetchone()[0] == 1
    con.close()


def test_existing_database_is_still_upgraded_by_migrations(tmp_path: Path) -> None:
    """A database from an older release (revision 0004, with data) takes the migration path, not create_all."""
    path: Path = tmp_path / "old.sqlite"
    command.upgrade(_cfg(path), "0004_pending_delete")
    con: sqlite3.Connection = sqlite3.connect(path)
    con.execute("INSERT INTO settings (section, key, transport_type) VALUES ('s','k',NULL)")
    con.commit()
    con.close()

    assert ensure_schema(path, INI) == "migrated"

    assert _version(path) == _head()
    con = sqlite3.connect(path)
    assert con.execute("SELECT section, transport_type FROM settings").fetchall() == [("s", "general")]  # 0006 backfill ran
    con.close()


def test_create_schema_failure_aborts_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Like a failed migration, a failed fresh-create must raise so the server never runs on a bad schema."""
    def boom(*_a: Any, **_k: Any) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(Base.metadata, "create_all", boom)
    with pytest.raises(OSError, match="disk full"):
        ensure_schema(tmp_path / "mpg.sqlite", INI)
