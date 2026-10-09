# Description: database.py — SQLAlchemy engine, session factory, schema bootstrap (fresh create / Alembic upgrade).
# File: database.py
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

"""
database.py — SQLAlchemy engine, session factory, and schema bootstrap.

The staging DB is a local SQLite file: config/data-db/mpg_staging.db

Schema bootstrap
----------------
``ensure_schema()`` is the single startup entry point:

* A brand-new database (no tables) is built **directly from the SQLAlchemy models**
  with ``create_all`` — no migration is run — and then stamped with the current
  Alembic head. The stamp only records "this schema is at revision X"; without it the
  first migration shipped in a later release would try to replay history onto tables
  that already exist.
* An existing database is upgraded with ``run_migrations()`` exactly as before.

``pytests/test_db_schema.py`` asserts the migrations produce exactly what the models
declare, which is what makes the two paths equivalent.

Usage
-----
    from classes.WebServer.database import get_session, ensure_schema

    # Dependency injection in FastAPI routes:
    @router.get("/")
    def my_route(db: Session = Depends(get_session)):
        ...

    # On startup:
    ensure_schema(db_path, alembic_ini_path)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Literal

from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.engine.interfaces import DBAPIConnection, DBAPICursor
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from .models import (
    AppState,
    Base,
    DeviceProtocolSelection,
    OrphanedFilterName,
    ProtocolRegister,
    Setting,
    SettingDescription,
)

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_db(db_path: Path) -> Engine:
    """
    Create (or reuse) the SQLAlchemy engine and session factory.
    Called once during FastAPI startup.

    check_same_thread=False is required because FastAPI's gateway thread
    and the web server thread both access the same SQLite file.
    """
    global _engine, _SessionLocal

    db_url: str = f"sqlite:///{db_path.as_posix()}"
    _engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    # Enable WAL mode so the gateway thread and web thread don't block each other
    def set_wal_mode(
        dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
    ) -> None:
        cursor: DBAPICursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listens_for(_engine, "connect")(set_wal_mode)

    _SessionLocal = sessionmaker(
        bind=_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    msg: str = f"SQLite staging DB engine created at {db_path}"
    _log.info(msg)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _SessionLocal


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a SQLAlchemy session and
    commits on success or rolls back on any exception.

    Usage:
        @router.get("/")
        def view(db: Session = Depends(get_session)):
            ...
    """
    factory: sessionmaker[Session] = get_session_factory()
    db: Session = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Context-manager variant for use outside of FastAPI routes
    (e.g., the scanner, file watcher, commit engine).
    """
    factory: sessionmaker[Session] = get_session_factory()
    db: Session = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Alembic migration runner
# ---------------------------------------------------------------------------

def run_migrations(db_path: Path, alembic_ini_path: Path) -> None:
    """
    Run Alembic migrations to head on startup.

    If migrations fail the exception is re-raised so FastAPI startup
    aborts — we never want to run against an out-of-date schema.
    """
    try:
        from alembic import command as alembic_command

        cfg: Config = _alembic_config(db_path, alembic_ini_path)

        alembic_command.upgrade(cfg, "head")
        _log.info("Alembic migrations applied successfully.")
    except Exception as exc:
        msg: str = f"Alembic migration failed — aborting server startup: {exc}"
        _log.exception(msg)
        raise


def _alembic_config(db_path: Path, alembic_ini_path: Path) -> Config:
    from alembic.config import Config as AlembicConfig

    cfg: Config = AlembicConfig(str(alembic_ini_path))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    cfg.set_main_option("script_location", str(alembic_ini_path.parent / "migrations"))
    return cfg


def database_has_tables(db_path: Path) -> bool:
    """True if the SQLite file at ``db_path`` exists and contains any user table.

    A missing file or an empty one (which SQLite creates the moment anything
    connects) is a brand-new database. Uses a throwaway engine so no connection
    to the file outlives the check.
    """
    if not db_path.exists():
        return False
    probe: Engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return bool(inspect(probe).get_table_names())
    finally:
        probe.dispose()


def create_schema(db_path: Path, alembic_ini_path: Path) -> None:
    """Build the complete, current schema from the models and stamp it at Alembic head.

    No migration is executed. Only for a database with no tables — ``ensure_schema``
    decides that; calling this on an existing database would leave its recorded
    revision wrong.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    builder: Engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(bind=builder)
    finally:
        builder.dispose()

    # Record the revision so later releases migrate from here instead of replaying
    # history. This writes alembic_version only — it does not run any migration.
    from alembic import command as alembic_command

    alembic_command.stamp(_alembic_config(db_path, alembic_ini_path), "head")


def ensure_schema(db_path: Path, alembic_ini_path: Path) -> Literal["created", "migrated"]:
    """Make the database schema current. Called once at startup.

    * New database (no tables)  -> ``create_schema``: built from the models, no
      migrations run. Returns ``"created"``.
    * Existing database         -> ``run_migrations``: upgraded to head, as before.
      Returns ``"migrated"``.

    Any failure is re-raised so FastAPI startup aborts: the server must never run
    against a missing or out-of-date schema.
    """
    try:
        if not database_has_tables(db_path):
            create_schema(db_path, alembic_ini_path)
            _log.info("New database: schema created from the models (no migrations run).")
            return "created"
    except Exception as exc:
        msg: str = f"Creating the database schema failed — aborting server startup: {exc}"
        _log.exception(msg)
        raise

    run_migrations(db_path, alembic_ini_path)
    return "migrated"


# ---------------------------------------------------------------------------
# AppState bootstrap
# ---------------------------------------------------------------------------

def ensure_app_state(db: Session) -> AppState:
    """
    Ensure the single AppState row (id=1) exists.
    Called during startup after migrations run.
    """
    state: AppState | None = db.get(AppState, 1)
    if state is None:
        state = AppState(id=1)
        db.add(state)
        db.commit()
        db.refresh(state)
        _log.info("AppState row created.")
    return state


def refresh_app_state(db: Session) -> AppState:
    """
    Recompute dirty/orphan counts from the live table data and persist.
    Call after any scan or toggle operation.
    """


    dirty_settings: int = db.scalar(
        select(func.count()).where(Setting.is_dirty == True)  # noqa: E712
    ) or 0

    dirty_descriptions: int = db.scalar(
        select(func.count()).where(SettingDescription.is_dirty == True)  # noqa: E712
    ) or 0
    orphan_count: int = db.scalar(
        select(func.count()).where(
            Setting.is_orphan == True,  # noqa: E712
            Setting.is_active == True,  # noqa: E712
        )
    ) or 0
    dirty_protocols: int = db.scalar(
        select(func.count()).where(ProtocolRegister.is_dirty == True)  # noqa: E712
    ) or 0
    pending_delete_protocols: int = db.scalar(
        select(func.count()).where(ProtocolRegister.pending_delete == True)  # noqa: E712
    ) or 0
    dirty_device_protocols: int = db.scalar(
        select(func.count()).where(DeviceProtocolSelection.is_dirty == True)  # noqa: E712
    ) or 0
    orphaned_filter_count: int = db.scalar(
        select(func.count()).select_from(OrphanedFilterName)
    ) or 0

    state: AppState = ensure_app_state(db)
    state.dirty_settings_count = dirty_settings + dirty_descriptions
    state.orphan_count = orphan_count
    state.orphaned_filter_count = orphaned_filter_count
    state.dirty_protocols_count = dirty_protocols + pending_delete_protocols + dirty_device_protocols
    state.has_dirty_settings = (dirty_settings + dirty_descriptions) > 0
    state.has_dirty_protocols = (dirty_protocols + pending_delete_protocols + dirty_device_protocols) > 0
    state.has_orphans = orphan_count > 0
    state.has_orphaned_filters = orphaned_filter_count > 0
    db.commit()
    db.refresh(state)
    return state
