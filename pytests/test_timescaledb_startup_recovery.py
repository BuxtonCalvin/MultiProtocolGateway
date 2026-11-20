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


def _pending_registration(protocol: str) -> tsdb.timescaledb.FailedRegistrations:
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
    b._reconnect_lock = threading.RLock()
    b.tsdb_connected = False
    b._source_transports = {}
    b._ids_checked = set()
    b._deferred_noted = set()
    b._device_id_store = None
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


def test_write_data_keeps_a_packet_for_a_device_whose_id_is_not_known_yet(bridge: Any) -> None:
    """First-ever start with the database down: nothing is cached, but the payload carries transport_name, so the flush worker
    can resolve the real id later. The point is queued with the placeholder 0, not dropped."""
    bridge.write_data({"battery_soc": 87}, _transport())
    payload: Any = bridge._flush_queue.get_nowait()
    assert payload["device_info_id"] == 0
    assert payload["transport_name"] == "inverter_1"
    assert bridge._source_transports["inverter_1"].transport_name == "inverter_1"


class _ForbiddenSession:
    """Fails the test if anything opens a database session."""

    def __init__(self) -> None:
        pytest.fail("the database must not be touched while it is known to be down")


def test_write_data_does_not_touch_the_database_during_a_known_outage(bridge: Any) -> None:
    bridge.SessionFactory = _ForbiddenSession
    bridge.write_data({"battery_soc": 87}, _transport())
    assert bridge._flush_queue.qsize() == 1


def test_write_data_uses_the_id_remembered_from_an_earlier_run_during_an_outage(bridge: Any) -> None:
    bridge._device_cache["inverter_1"] = 7  # loaded from the backlog file at startup, not yet verified this session
    bridge.SessionFactory = _ForbiddenSession
    bridge.write_data({"battery_soc": 87}, _transport())
    assert bridge._flush_queue.get_nowait()["device_info_id"] == 7


# ---------------------------------------------------------------------------
# completing a point's identity at flush time
# ---------------------------------------------------------------------------

def _payload(device_info_id: int = 0, wide_table_name: str | None = None) -> Any:
    return {
        "device_info_id": device_info_id, "metrics": {"battery_soc": 87}, "m_time": None,
        "transport_name": "inverter_1", "protocol": "eg4_18kpv", "wide_table_name": wide_table_name,
    }


def test_a_point_whose_device_is_verified_needs_nothing_completed(bridge: Any) -> None:
    bridge._verified_devices.add("inverter_1")
    assert bridge._needs_identity(_payload(7)) is False


def test_a_point_whose_device_is_not_verified_this_session_needs_completing(bridge: Any) -> None:
    assert bridge._needs_identity(_payload(7)) is True


def test_a_point_whose_protocol_registration_is_pending_needs_completing(bridge: Any) -> None:
    bridge._verified_devices.add("inverter_1")
    bridge._failed_registrations = _pending_registration("eg4_18kpv")
    assert bridge._needs_identity(_payload(7)) is True


def test_nothing_is_completed_while_the_database_is_down(bridge: Any) -> None:
    bridge.SessionFactory = _ForbiddenSession
    payload: Any = _payload()
    assert bridge._complete_identity(payload) is False
    assert payload["device_info_id"] == 0


def _lookup_returning_5(_name: str) -> int:
    return 5


def _lookup_returning_nothing(_name: str) -> int:
    return 0


def test_the_device_is_resolved_through_its_transport_once_the_database_is_back(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.tsdb_connected = True
    bridge._source_transports["inverter_1"] = _transport()
    bridge._protocol_wide_table_map = {"eg4_18kpv": "device_metrics_wide__eg4_18kpv"}

    def upsert(from_transport: Any, metric_count: int = 0) -> int:
        bridge._device_cache[from_transport.transport_name] = 11
        bridge._verified_devices.add(from_transport.transport_name)
        return 11

    monkeypatch.setattr(bridge, "_get_or_create_device", upsert)
    payload: Any = _payload(device_info_id=7)  # 7 = a stale id remembered from an earlier run
    assert bridge._complete_identity(payload) is True
    assert payload["device_info_id"] == 11  # the database's answer wins over the remembered id
    assert payload["wide_table_name"] == "device_metrics_wide__eg4_18kpv"


def test_a_point_from_the_backlog_is_resolved_by_transport_name_when_its_transport_has_not_written_yet(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.tsdb_connected = True
    monkeypatch.setattr(bridge, "_lookup_device_id", _lookup_returning_5)
    payload: Any = _payload()
    assert bridge._complete_identity(payload) is True
    assert payload["device_info_id"] == 5
    assert "inverter_1" in bridge._ids_checked
    assert "inverter_1" not in bridge._verified_devices  # the metadata upsert still happens on the transport's own first write


def test_a_point_stays_queued_when_its_device_cannot_be_resolved(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.tsdb_connected = True
    monkeypatch.setattr(bridge, "_lookup_device_id", _lookup_returning_nothing)
    payload: Any = _payload()
    assert bridge._complete_identity(payload) is False
    assert payload["device_info_id"] == 0


def test_a_point_waits_for_its_protocol_schema_to_be_registered(bridge: Any) -> None:
    bridge.tsdb_connected = True
    bridge._verified_devices.add("inverter_1")
    bridge._device_cache["inverter_1"] = 11
    bridge._failed_registrations = _pending_registration("eg4_18kpv")
    assert bridge._complete_identity(_payload()) is False


def test_the_flush_worker_keeps_a_deferred_point_instead_of_writing_it_while_the_database_is_down(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.migration_in_progress = threading.Event()
    kept: list[Any] = []
    monkeypatch.setattr(bridge, "_keep_unwritten", kept.append)
    payload: Any = _payload()
    assert bridge._flush_one_inner(payload, None) is False
    assert kept == [payload]


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
