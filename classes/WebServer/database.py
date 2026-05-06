"""
database.py — SQLAlchemy engine, session factory, and Alembic upgrade helper.

The staging DB is a local SQLite file: config/data-db/mpg_staging.db

Usage
-----
    from classes.WebServer.database import get_session, run_migrations

    # Dependency injection in FastAPI routes:
    @router.get("/")
    def my_route(db: Session = Depends(get_session)):
        ...

    # On startup:
    run_migrations(db_path, alembic_ini_path)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AppState,
    DeviceProtocolSelection,
    ProtocolRegister,
    Setting,
    SettingDescription,
)

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


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
    @event.listens_for(_engine, "connect")
    def set_wal_mode(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def get_session_factory() -> sessionmaker:
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
    factory = get_session_factory()
    db = factory()
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
    factory = get_session_factory()
    db = factory()
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
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig(str(alembic_ini_path))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
        cfg.set_main_option("script_location", str(alembic_ini_path.parent / "migrations"))

        alembic_command.upgrade(cfg, "head")
        _log.info("Alembic migrations applied successfully.")
    except Exception as exc:
        msg: str = f"Alembic migration failed — aborting server startup: {exc}"
        _log.exception(msg)
        raise


# ---------------------------------------------------------------------------
# AppState bootstrap
# ---------------------------------------------------------------------------

def ensure_app_state(db: Session) -> AppState:
    """
    Ensure the single AppState row (id=1) exists.
    Called during startup after migrations run.
    """
    state = db.get(AppState, 1)
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


    dirty_settings = db.scalar(
        select(func.count()).where(Setting.is_dirty == True)  # noqa: E712
    ) or 0

    dirty_descriptions = db.scalar(
        select(func.count()).where(SettingDescription.is_dirty == True)  # noqa: E712
    ) or 0
    orphan_count = db.scalar(
        select(func.count()).where(
            Setting.is_orphan == True,  # noqa: E712
            Setting.is_active == True,  # noqa: E712
        )
    ) or 0
    dirty_protocols = db.scalar(
        select(func.count()).where(ProtocolRegister.is_dirty == True)  # noqa: E712
    ) or 0
    dirty_device_protocols = db.scalar(
        select(func.count()).where(DeviceProtocolSelection.is_dirty == True)  # noqa: E712
    ) or 0

    state = ensure_app_state(db)
    state.dirty_settings_count = dirty_settings + dirty_descriptions
    state.orphan_count = orphan_count
    state.dirty_protocols_count = dirty_protocols + dirty_device_protocols
    state.has_dirty_settings = (dirty_settings + dirty_descriptions) > 0
    state.has_dirty_protocols = (dirty_protocols + dirty_device_protocols) > 0
    state.has_orphans = orphan_count > 0
    db.commit()
    db.refresh(state)
    return state
