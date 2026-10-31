"""Unit tests for how the timescaledb bridge behaves when the database is unreachable at startup, and for what it restores
after a reconnect (no database needed)."""

# These tests deliberately exercise the bridge's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import logging
import queue
import threading
import types
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

import classes.transports.timescaledb as tsdb


def _no_rows() -> list[object]:
    return []


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _pending_registration(protocol: str) -> dict[str, tuple[dict[str, object], list[object] | None]]:
    """What init_bridge remembers about a registration that failed: the protocol's registry map and its synthetic fields."""
    return {protocol: ({}, None)}


class _ExplodingSession:
    """A session whose first statement fails, as when the database is unreachable."""

    def __enter__(self) -> _ExplodingSession:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    def rollback(self) -> None:
        return None

    def commit(self) -> None:
        return None


@pytest.fixture
def bridge() -> Any:
    b: Any = object.__new__(tsdb.timescaledb)
    b._log = logging.getLogger("test_timescaledb_startup_recovery")
    b._device_lock = threading.RLock()
    b._device_cache = {}
    b._verified_devices = set()
    b.SessionFactory = _ExplodingSession
    b.hypertable_mgr = None
    b._protocol_wide_table_map = {}
    b._flush_queue = queue.Queue[Any]()
    b._registered_protocols = set()
    b._failed_registrations = {}
    return b


def _transport() -> Any:
    return types.SimpleNamespace(
        transport_name="inverter_1", protocol_name="eg4_18kpv", device_identifier="x", device_name="x",
        device_manufacturer="x", device_model="x", device_serial_number="x", device_location="x",
    )


# ---------------------------------------------------------------------------
# an unresolved device must never get a real-looking id
# ---------------------------------------------------------------------------

def test_a_device_that_cannot_be_resolved_gets_id_zero_not_a_plausible_one(bridge: Any) -> None:
    assert bridge._get_or_create_device(_transport(), metric_count=3) == 0


def test_write_data_drops_a_packet_for_an_unresolved_device(bridge: Any) -> None:
    bridge.write_data({"battery_soc": 87}, _transport())
    assert bridge._flush_queue.empty()


def test_a_device_resolved_earlier_keeps_its_id_while_the_database_is_down(bridge: Any) -> None:
    bridge._device_cache["inverter_1"] = 7
    bridge._verified_devices.add("inverter_1")
    assert bridge._get_or_create_device(_transport(), metric_count=3) == 7


# ---------------------------------------------------------------------------
# registrations that failed at startup are completed after a reconnect
# ---------------------------------------------------------------------------

def test_a_failed_registration_is_retried_and_forgotten_once_it_succeeds(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[str] = []

    def register(protocol: str, _registry_map: object, synthetic_fields: object = None) -> None:
        registered.append(protocol)

    monkeypatch.setattr(bridge, "_register_protocol_schema", register)
    bridge._failed_registrations = _pending_registration("eg4_18kpv")
    bridge._retry_failed_registrations()

    assert registered == ["eg4_18kpv"]
    assert bridge._failed_registrations == {}
    assert "eg4_18kpv" in bridge._registered_protocols


def test_a_registration_that_still_fails_stays_queued_for_the_next_reconnect(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def register(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("not connected")

    monkeypatch.setattr(bridge, "_register_protocol_schema", register)
    bridge._failed_registrations = _pending_registration("eg4_18kpv")
    bridge._retry_failed_registrations()  # must not raise

    assert "eg4_18kpv" in bridge._failed_registrations
    assert "eg4_18kpv" not in bridge._registered_protocols


# ---------------------------------------------------------------------------
# rediscovery after a reconnect
# ---------------------------------------------------------------------------

class _Rows:
    def __init__(self, rows: list[tuple[str, str | None, bool]]) -> None:
        self._rows: list[tuple[str, str | None, bool]] = rows

    def fetchall(self) -> list[tuple[str, str | None, bool]]:
        return list(self._rows)


def test_one_protocol_that_cannot_be_restored_does_not_stop_the_others(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wide table dropped by hand used to raise out of the loop: the remaining protocols were never restored and the reconnect
    worker reported the connection as failed again (a lost/restored alert each time)."""

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows([("broken", "device_metrics_wide__broken", True), ("good", "device_metrics_wide__good", True)])

    restored: list[str] = []

    def fake_table(name: str, *_args: object, **_kwargs: object) -> None:
        if name.endswith("broken"):
            raise RuntimeError("no such table")

    monkeypatch.setattr(tsdb, "Table", fake_table)
    bridge.SessionFactory = _Session
    bridge.engine = object()  # only handed to the (faked) Table reflection
    bridge.schema_lock = threading.RLock()
    bridge.rollup_mgr = None

    def cache_columns(_name: str) -> None:
        return None

    bridge._cache_wide_table_columns = cache_columns
    bridge._restore_metric_mapping = restored.append
    bridge._rediscover_protocols()

    assert restored == ["good"]
    assert bridge._protocol_wide_table_map["good"] == "device_metrics_wide__good"


# ---------------------------------------------------------------------------
# orphaned-lock cleanup at startup
# ---------------------------------------------------------------------------

def test_only_sessions_that_actually_hold_locks_are_considered_orphaned(bridge: Any) -> None:
    class _Conn:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execution_options(self, **_kwargs: object) -> _Conn:
            return self

        def execute(self, statement: object, _params: object = None) -> Any:
            self.sql.append(str(statement))
            return types.SimpleNamespace(fetchall=_no_rows)

    conn: _Conn = _Conn()
    bridge.engine = types.SimpleNamespace(connect=lambda: conn)
    bridge.database = "solar"
    bridge._cleanup_orphaned_locks()

    assert "pg_locks" in conn.sql[0]  # a person's idle query tool that blocks nothing is left alone


# ---------------------------------------------------------------------------
# narrow-only protocols still get the shared narrow rollups
# ---------------------------------------------------------------------------

def test_a_narrow_only_protocol_still_gets_the_rollup_manager_set_up(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol with too many metrics for a wide table writes to the shared narrow table, so rollups (when enabled) apply to it."""
    called_for: list[str] = []

    def safe_table_name(protocol: str) -> str:
        return f"device_metrics_wide__{protocol}"

    def extract_metric_names(*_args: object, **_kwargs: object) -> list[tuple[str, str, float, str]]:
        return [("m", "INT", 1.0, "")] * int(bridge.WIDE_TABLE_COLUMN_LIMIT)

    bridge._safe_table_name = safe_table_name
    bridge._extract_metric_names = extract_metric_names
    bridge._upsert_protocol_registry = _noop
    monkeypatch.setattr(bridge, "_init_or_update_rollup_manager", called_for.append)

    registry_map: dict[str, object] = {}
    bridge._register_protocol_schema("big_protocol", registry_map)

    assert bridge._protocol_wide_table_map["big_protocol"] is None
    assert called_for == ["big_protocol"]


def test_with_rollups_enabled_the_first_protocol_creates_the_manager_and_the_narrow_stack_even_if_it_is_narrow_only(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []

    class _FakeHyperTableManager:
        def __init__(self, **_kwargs: object) -> None:
            self.rollup_mgr: Any = None

    class _FakeRollupManager:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def setup_narrow_rollup(self) -> None:
            calls.append(("narrow_stack", None))

        def add_wide_rollup(self, protocol_name: str, wide_table_name: str | None) -> None:
            calls.append(("protocol", (protocol_name, wide_table_name)))

        def start_auto_refresh(self) -> None:
            calls.append(("auto_refresh", None))

    monkeypatch.setattr(tsdb, "HyperTableManager", _FakeHyperTableManager)
    monkeypatch.setattr(tsdb, "RollupManager", _FakeRollupManager)
    bridge.rollup_policy = {"enable_rollups": True, "enable_auto_refresh": True}
    bridge.rollup_mgr = None
    bridge.enable_persistent_storage = False
    for name in ("SessionFactory", "engine", "migration_in_progress", "send_message", "_backlog_lock", "_flush_queue", "backlog", "_reconnect_lock"):
        setattr(bridge, name, object())
    bridge._protocol_wide_table_map = {"big_protocol": None}

    bridge._init_or_update_rollup_manager("big_protocol")

    assert calls == [("narrow_stack", None), ("protocol", ("big_protocol", None)), ("auto_refresh", None)]


def test_with_rollups_disabled_no_rollup_manager_is_created(bridge: Any) -> None:
    bridge.rollup_policy = {"enable_rollups": False}
    bridge.rollup_mgr = None
    bridge._init_or_update_rollup_manager("big_protocol")
    assert bridge.rollup_mgr is None
