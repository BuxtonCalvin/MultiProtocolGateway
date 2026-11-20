"""Unit tests for starting the timescaledb bridge while the database is unreachable (no database needed).

The gateway must start, scrapers must keep running and points must be queued even when the database is down or still restarting.
Covers the connection manager (never raises for an unreachable database, bounded wait, extension preflight on reconnect), the
reconnect worker finishing the startup work later, and the device id cache kept in the backlog's SQLite file.
"""

# These tests deliberately exercise private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from sqlalchemy.exc import OperationalError

import classes.transports.timescaledb as tsdb

# ---------------------------------------------------------------------------
# connection manager
# ---------------------------------------------------------------------------


def _manager(auto_update: bool = True) -> tsdb.TimescaleDBConnectionManager:
    return tsdb.TimescaleDBConnectionManager(
        "127.0.0.1", 1, "solar", "u", "p", log=logging.getLogger("test_startup_without_database"), auto_update_extensions=auto_update
    )


def _refuse(**_kw: Any) -> Any:
    raise psycopg2.OperationalError("connection refused")


def _no_sleep(_seconds: float) -> None:
    return None


def _must_not_run(*_args: object, **_kwargs: object) -> None:
    pytest.fail("must not run when the server is not answering")


def test_connect_does_not_raise_when_the_database_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    manager: tsdb.TimescaleDBConnectionManager = _manager()
    monkeypatch.setattr(psycopg2, "connect", _refuse)
    monkeypatch.setattr(time, "sleep", _no_sleep)
    monkeypatch.setattr(tsdb.TimescaleDBConnectionManager, "PREFLIGHT_TIMEOUT_SECONDS", 0.0)

    manager.connect()  # used to raise out of the transport's constructor and end the whole process

    assert manager._engine is not None  # so the session factory, flush worker and backlog can still be built
    assert manager._connected is False
    assert manager.engine is manager._engine


def test_a_server_that_never_answers_ends_the_preflight_at_the_first_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def refuse_and_count(**kw: Any) -> Any:
        attempts.append(str(kw["dbname"]))
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", refuse_and_count)
    monkeypatch.setattr(time, "sleep", _no_sleep)

    assert _manager()._preflight_update_extensions(wait=False) is False
    assert attempts == ["postgres"]  # the other five database/extension pairs would only wait out the same deadline


def test_the_startup_preflight_stops_waiting_when_its_deadline_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psycopg2, "connect", _refuse)
    monkeypatch.setattr(time, "sleep", _no_sleep)
    monkeypatch.setattr(tsdb.TimescaleDBConnectionManager, "PREFLIGHT_TIMEOUT_SECONDS", 0.0)
    assert _manager()._preflight_update_extensions(wait=True) is False


def test_the_preflight_does_nothing_and_succeeds_when_auto_update_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psycopg2, "connect", _must_not_run)
    assert _manager(auto_update=False)._preflight_update_extensions() is True


def test_ensure_ready_reports_false_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    manager: tsdb.TimescaleDBConnectionManager = _manager()
    monkeypatch.setattr(psycopg2, "connect", _refuse)
    assert manager.ensure_ready(wait=False) is False
    assert manager._connected is False


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Engine:
    def connect(self) -> _Conn:
        return _Conn()


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_ensure_ready_runs_the_preflight_every_time_not_just_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database upgraded while the gateway kept running is repaired by the next reconnect attempt."""
    manager: tsdb.TimescaleDBConnectionManager = _manager()
    manager._engine = _Engine()  # type: ignore[assignment]
    runs: list[bool] = []

    def preflight(wait: bool = True) -> bool:
        runs.append(wait)
        return True

    monkeypatch.setattr(manager, "_preflight_update_extensions", preflight)
    monkeypatch.setattr(manager, "_create_database_if_missing", _noop)

    assert manager.ensure_ready() is True
    assert manager.ensure_ready() is True
    assert runs == [False, False]  # reconnect attempts do not wait for the server
    assert manager._connected is True


def test_connect_is_a_no_op_once_the_engine_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    manager: tsdb.TimescaleDBConnectionManager = _manager()
    manager._engine = _Engine()  # type: ignore[assignment]
    monkeypatch.setattr(manager, "ensure_ready", _must_not_run)
    manager.connect()  # a second protocol instance sharing the manager must not repeat the preflight


# ---------------------------------------------------------------------------
# reconnect worker finishes a startup that happened with the database down
# ---------------------------------------------------------------------------


class _VerifyingManager:
    def __init__(self, ready: bool) -> None:
        self.ready: bool = ready
        self.calls: list[bool] = []

    def ensure_ready(self, wait: bool = False) -> bool:
        self.calls.append(wait)
        return self.ready


class _FailingEngine:
    def connect(self) -> _Conn:
        raise OperationalError("SELECT 1", {}, Exception("server unreachable"))


def _bridge(engine: Any, manager: Any = None, startup_complete: bool = False) -> Any:
    b: Any = object.__new__(tsdb.timescaledb)
    b._log = logging.getLogger("test_startup_without_database")
    b.engine = engine
    if manager is not None:
        b._connection_manager = manager
    b._startup_complete = startup_complete
    b._stop_reconnect_event = threading.Event()
    b._reconnect_lock = threading.RLock()
    b._reconnect_thread_running = True
    b.tsdb_connected = False
    b.reconnect_attempts = 1
    b.reconnect_delay = 0
    b.use_exponential_backoff = False
    b.rollup_mgr = None
    b.enable_persistent_storage = False
    b.events = []

    def set_connected(conn_value: bool, conn_reason: str) -> None:
        b.tsdb_connected = conn_value
        b.events.append((conn_value, conn_reason))

    def connect_tsdb() -> None:
        b.events.append("connect_tsdb")
        b._startup_complete = True

    b._set_tsdb_connected = set_connected
    b.connect_tsdb = connect_tsdb
    b._rediscover_protocols = _noop
    return b


def test_verify_asks_the_manager_to_repair_when_a_plain_connect_fails() -> None:
    manager: _VerifyingManager = _VerifyingManager(ready=True)
    _bridge(_FailingEngine(), manager)._verify_database()  # must not raise
    assert manager.calls == [False]


def test_verify_raises_when_the_manager_cannot_get_the_database_ready() -> None:
    manager: _VerifyingManager = _VerifyingManager(ready=False)
    with pytest.raises(OperationalError):
        _bridge(_FailingEngine(), manager)._verify_database()


def test_verify_does_not_involve_the_manager_when_the_database_answers() -> None:
    manager: _VerifyingManager = _VerifyingManager(ready=False)
    _bridge(_Engine(), manager)._verify_database()
    assert manager.calls == []


def test_the_reconnect_worker_runs_the_startup_work_that_was_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", _no_sleep)
    bridge: Any = _bridge(_Engine(), startup_complete=False)
    bridge._attempt_reconnect()
    assert bridge.events[0] == "connect_tsdb"
    assert bridge._startup_complete is True
    assert bridge.tsdb_connected is True


def test_the_reconnect_worker_does_not_repeat_startup_work_after_a_normal_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", _no_sleep)
    bridge: Any = _bridge(_Engine(), startup_complete=True)
    bridge._attempt_reconnect()
    assert "connect_tsdb" not in bridge.events
    assert bridge.tsdb_connected is True


def test_a_failing_startup_step_leaves_the_bridge_disconnected_for_the_next_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", _no_sleep)
    bridge: Any = _bridge(_Engine(), startup_complete=False)

    def failing_connect_tsdb() -> None:
        raise RuntimeError("extension check refused")

    bridge.connect_tsdb = failing_connect_tsdb
    bridge._attempt_reconnect()
    assert bridge.tsdb_connected is False
    assert bridge._startup_complete is False


# ---------------------------------------------------------------------------
# device ids remembered in the backlog's SQLite file
# ---------------------------------------------------------------------------


def _store(path: Path) -> tsdb.DeviceIdStore:
    return tsdb.DeviceIdStore(path, logging.getLogger("test_startup_without_database"))


def test_ids_survive_a_restart(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.db"
    first: tsdb.DeviceIdStore = _store(path)
    first.save("inverter_1", 7)
    first.save("battery_1", 9)
    assert _store(path).load() == {"inverter_1": 7, "battery_1": 9}


def test_a_changed_id_replaces_the_old_one(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.db"
    store: tsdb.DeviceIdStore = _store(path)
    store.save("inverter_1", 7)
    store.save("inverter_1", 12)  # e.g. the database was recreated
    assert _store(path).load() == {"inverter_1": 12}


def test_saving_the_same_id_again_does_not_write(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.db"
    store: tsdb.DeviceIdStore = _store(path)
    store.save("inverter_1", 7)
    path.unlink()  # if the next save wrote, the file would be recreated
    store.save("inverter_1", 7)
    assert not path.exists()


def test_the_cache_lives_beside_the_backlog_table_without_disturbing_it(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.db"
    con: sqlite3.Connection = sqlite3.connect(str(path))
    con.execute("CREATE TABLE backlog (id INTEGER PRIMARY KEY AUTOINCREMENT, m_time TEXT NOT NULL, payload TEXT NOT NULL)")
    con.execute("INSERT INTO backlog (m_time, payload) VALUES ('2026-11-20T00:00:00', '{}')")
    con.commit()
    con.close()

    _store(path).save("inverter_1", 7)

    check: sqlite3.Connection = sqlite3.connect(str(path))
    try:
        assert check.execute("SELECT COUNT(*) FROM backlog").fetchone() == (1,)
        assert check.execute("SELECT device_info_id FROM device_id_cache").fetchone() == (7,)
    finally:
        check.close()


def test_an_unusable_file_loses_the_cache_but_never_raises(tmp_path: Path) -> None:
    bad: Path = tmp_path / "is_a_directory.db"
    bad.mkdir()
    store: tsdb.DeviceIdStore = _store(bad)
    assert store.load() == {}
    store.save("inverter_1", 7)  # must not raise


def test_a_zero_id_is_never_remembered(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.db"
    _store(path).save("inverter_1", 0)
    assert not path.exists()
