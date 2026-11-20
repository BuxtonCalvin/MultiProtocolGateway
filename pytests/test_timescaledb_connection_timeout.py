"""Unit tests for the timescaledb transport's connection_timeout setting and the extension-update preflight (no database needed).

connection_timeout bounds how long a connection attempt may take, so an unreachable database host fails after that many
seconds instead of the operating system's TCP timeout (about two minutes). The setting is documented in the transport's
Configuration options and read with the other connection options; libpq itself calls the option "connect_timeout", so that
name still appears where it is handed to the driver.

The preflight tests cover TimescaleDBConnectionManager._preflight_update_extensions: it must run ALTER EXTENSION ... UPDATE as
the very first statement of a fresh autocommit session, before any SQLAlchemy connection exists, so a database whose extension
is older than the server image (for example right after a container upgrade) can still be opened.
"""

# These tests deliberately exercise the manager's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import time
from typing import Any

import psycopg2
import pytest

import classes.transports.timescaledb as tsdb


class _Settings:
    """The slice of a configparser section the connection manager reads."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values: dict[str, str] = values

    def get(self, key: str, fallback: str = "") -> str:
        return self._values.get(key, fallback)

    def getint(self, key: str, fallback: int = 0) -> int:
        return int(self._values[key]) if key in self._values else fallback

    def getboolean(self, key: str, fallback: bool = False) -> bool:
        if key not in self._values:
            return fallback
        return self._values[key].strip().lower() in ("1", "true", "yes", "on")


class _FakeCursor:
    """Records every statement together with the database and the autocommit flag at the moment it was executed."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn: _FakeConn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        # The autocommit flag is captured at execute time: it must already be on, or psycopg2 sends BEGIN first
        # (and BEGIN would load the old extension library before the ALTER ever ran).
        self._conn.calls.append((self._conn.dbname, self._conn.autocommit, sql))


class _FakeConn:
    def __init__(self, dbname: str, calls: list[tuple[str, bool, str]]) -> None:
        self.dbname: str = dbname
        self.calls: list[tuple[str, bool, str]] = calls
        self.autocommit: bool = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        return None


def _manager(auto_update: bool = True) -> tsdb.TimescaleDBConnectionManager:
    return tsdb.TimescaleDBConnectionManager("h", 5432, "solar", "u", "p", auto_update_extensions=auto_update)


def _install_fake_connect(monkeypatch: Any, calls: list[tuple[str, bool, str]]) -> None:
    def fake_connect(**kw: Any) -> _FakeConn:
        return _FakeConn(str(kw["dbname"]), calls)

    monkeypatch.setattr(psycopg2, "connect", fake_connect)


def _no_sleep(_seconds: float) -> None:
    return None


def _sleep_must_not_be_called(_seconds: float) -> None:
    pytest.fail("must not retry")


# ---------------------------------------------------------------------------------------------------------------------
# connection_timeout
# ---------------------------------------------------------------------------------------------------------------------


def test_the_connection_manager_reads_connection_timeout_from_the_settings() -> None:
    settings: Any = _Settings({"connection_timeout": "4"})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 4


def test_connection_timeout_defaults_to_ten_seconds() -> None:
    settings: Any = _Settings({})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 10


def test_the_old_name_is_not_read_any_more() -> None:
    settings: Any = _Settings({"connect_timeout": "99"})
    assert tsdb.TimescaleDBConnectionManager.from_settings(settings).connection_timeout == 10


def test_the_configured_value_reaches_the_driver_under_libpqs_own_option_name() -> None:
    settings: Any = _Settings({"connection_timeout": "7"})
    connect_args: dict[str, int] = tsdb.TimescaleDBConnectionManager.from_settings(settings)._connect_args()

    assert connect_args["connect_timeout"] == 7
    assert connect_args["keepalives"] == 1  # a dead link is noticed, not waited on


def test_the_transport_documents_and_defaults_the_setting_under_its_new_name() -> None:
    assert tsdb.timescaledb.connection_timeout == 10
    assert not hasattr(tsdb.timescaledb, "connect_timeout")
    assert "connection_timeout (int)" in (tsdb.timescaledb.__init__.__doc__ or "")


# ---------------------------------------------------------------------------------------------------------------------
# auto_update_extensions preflight
# ---------------------------------------------------------------------------------------------------------------------


def test_from_settings_reads_auto_update_extensions() -> None:
    on: Any = _Settings({})
    off: Any = _Settings({"auto_update_extensions": "false"})
    assert tsdb.TimescaleDBConnectionManager.from_settings(on).auto_update_extensions is True
    assert tsdb.TimescaleDBConnectionManager.from_settings(off).auto_update_extensions is False


def test_preflight_alters_each_extension_first_in_a_fresh_autocommit_session(monkeypatch: Any) -> None:
    calls: list[tuple[str, bool, str]] = []
    _install_fake_connect(monkeypatch, calls)

    _manager()._preflight_update_extensions()

    assert calls == [
        (db, True, f"ALTER EXTENSION {ext} UPDATE")
        for db in ("postgres", "template1", "solar")
        for ext in ("timescaledb", "timescaledb_toolkit")
    ]


def test_preflight_does_nothing_when_auto_update_is_off(monkeypatch: Any) -> None:
    calls: list[tuple[str, bool, str]] = []
    _install_fake_connect(monkeypatch, calls)

    _manager(auto_update=False)._preflight_update_extensions()

    assert calls == []


def test_preflight_retries_while_the_server_is_restarting(monkeypatch: Any) -> None:
    calls: list[tuple[str, bool, str]] = []
    attempts: list[int] = []

    def flaky_connect(**kw: Any) -> _FakeConn:
        attempts.append(1)
        if len(attempts) <= 2:
            raise psycopg2.OperationalError("the database system is starting up")
        return _FakeConn(str(kw["dbname"]), calls)

    monkeypatch.setattr(psycopg2, "connect", flaky_connect)
    monkeypatch.setattr(time, "sleep", _no_sleep)  # don't actually wait

    _manager()._alter_extension_update_raw("solar", "timescaledb")

    assert len(attempts) == 3
    assert calls == [("solar", True, "ALTER EXTENSION timescaledb UPDATE")]


def test_preflight_skips_a_database_that_does_not_exist_yet(monkeypatch: Any) -> None:
    def no_such_db(**_kw: Any) -> _FakeConn:
        raise psycopg2.OperationalError('FATAL:  database "solar" does not exist')

    monkeypatch.setattr(psycopg2, "connect", no_such_db)
    monkeypatch.setattr(time, "sleep", _sleep_must_not_be_called)

    _manager()._alter_extension_update_raw("solar", "timescaledb")  # returns without raising
