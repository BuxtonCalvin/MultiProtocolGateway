"""Unit tests for the TimescaleDB flush-queue payload surviving the on-disk backlog's JSON round trip (no database needed).

BacklogManager journals every point to SQLite as JSON (json.dumps(..., default=str)), so a datetime m_time is stored as
text. load_from_disk must restore it, and must refuse rows that are not a valid payload, so everything that reads the
flush queue sees one type: m_time is always a datetime.
"""

# These tests deliberately exercise the manager's private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

import pytest

import classes.transports.timescaledb as tsdb
from classes.transports.timescaledb import BacklogManager, FlushPayload


def _no_message(
    message: str,
    title: str = "",
    priority: int = 0,
    services: Optional[Union[list[str], str]] = None,
    **kwargs: Any,
) -> None:
    """SendMessageProtocol stand-in."""
    return None


def _manager(path: Path, flush_queue: queue.Queue[FlushPayload | None], max_age: int = 86400, max_size: int = 100) -> BacklogManager:
    return BacklogManager(
        backlog_file_path=path,
        max_backlog_age=max_age,
        max_backlog_size=max_size,
        send_message=_no_message,
        flush_queue=flush_queue,
        flush_event=threading.Event(),
        backlog_lock=threading.RLock(),
        log=logging.getLogger("test_timescaledb_backlog_payload"),
    )


def _payload(m_time: datetime, *, wide_table_name: str | None = "device_metrics_wide__eg4_18kpv") -> FlushPayload:
    return {
        "device_info_id": 3,
        "metrics": {"battery_voltage": 52.4, "battery_soc": 87, "state": "charging"},
        "m_time": m_time,
        "transport_name": "eg4_18kpv_inverter_1",
        "protocol": "eg4_18kpv",
        "wide_table_name": wide_table_name,
    }


def _recent(**kwargs: float) -> datetime:
    """A tz-aware time safely inside the default 24 h backlog age."""
    return tsdb._now_tz() - timedelta(**kwargs)


def _insert_row(path: Path, m_time_text: str, payload_text: str) -> None:
    """Write a backlog row exactly as another version of the app (or a corrupt file) might have left it."""
    con: sqlite3.Connection = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS backlog (id INTEGER PRIMARY KEY AUTOINCREMENT, m_time TEXT NOT NULL, payload TEXT NOT NULL)")
        con.execute("INSERT INTO backlog (m_time, payload) VALUES (?, ?)", (m_time_text, payload_text))
        con.commit()
    finally:
        con.close()


def _reload(path: Path) -> list[FlushPayload]:
    """What a restarted app would see: a brand-new manager reading the same file."""
    reloaded: BacklogManager = _manager(path, queue.Queue())
    reloaded.load_from_disk()
    return reloaded.backlog_points


def _row_count(path: Path) -> int:
    con: sqlite3.Connection = sqlite3.connect(str(path))
    try:
        return int(con.execute("SELECT COUNT(*) FROM backlog").fetchone()[0])
    finally:
        con.close()


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "m_time",
    [
        datetime(2026, 6, 2, 14, 23, 11, 123456, tzinfo=timezone.utc),  # microseconds
        datetime(2026, 6, 2, 14, 23, 11, tzinfo=timezone.utc),  # no microseconds: str() drops the fraction
        datetime(2026, 6, 2, 14, 23, 11, 5, tzinfo=timezone(timedelta(hours=-7))),  # a non-UTC offset
        datetime(2026, 6, 2, 14, 23, 11, tzinfo=timezone(timedelta(hours=5, minutes=30))),  # a half-hour offset
    ],
)
def test_m_time_survives_the_disk_round_trip_as_a_datetime(tmp_path: Path, m_time: datetime) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    writer: BacklogManager = _manager(path, queue.Queue(), max_age=10**9)
    writer.enqueue(_payload(m_time))

    (point,) = _reload_with_age(path, 10**9)
    assert isinstance(point["m_time"], datetime)
    assert point["m_time"] == m_time  # the same instant
    assert point["m_time"].utcoffset() == m_time.utcoffset()  # and the same offset


def _reload_with_age(path: Path, max_age: int) -> list[FlushPayload]:
    reloaded: BacklogManager = _manager(path, queue.Queue(), max_age=max_age)
    reloaded.load_from_disk()
    return reloaded.backlog_points


def test_every_field_survives_the_round_trip(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    original: FlushPayload = _payload(_recent(minutes=5))
    _manager(path, queue.Queue()).enqueue(original)

    (point,) = _reload(path)
    assert point == original


def test_narrow_only_payload_keeps_its_null_wide_table(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    _manager(path, queue.Queue()).enqueue(_payload(_recent(minutes=1), wide_table_name=None))

    (point,) = _reload(path)
    assert point["wide_table_name"] is None


def test_replay_after_a_restart_puts_datetime_payloads_on_the_flush_queue(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    originals: list[FlushPayload] = [_payload(_recent(minutes=n)) for n in (3, 2, 1)]
    writer: BacklogManager = _manager(path, queue.Queue())
    for point in originals:
        writer.enqueue(point)

    flush_queue: queue.Queue[FlushPayload | None] = queue.Queue()
    restarted: BacklogManager = _manager(path, flush_queue)
    restarted.load_from_disk()
    assert restarted.replay_to_queue() == 3

    replayed: list[FlushPayload | None] = [flush_queue.get_nowait() for _ in range(3)]
    assert replayed == originals
    assert all(p is not None and isinstance(p["m_time"], datetime) for p in replayed)
    assert _row_count(path) == 0  # replay clears the journal


def test_in_memory_replay_without_a_restart_is_unchanged(tmp_path: Path) -> None:
    """The in-memory working set holds the original objects, so no round trip happens when the app stayed up."""
    flush_queue: queue.Queue[FlushPayload | None] = queue.Queue()
    manager: BacklogManager = _manager(tmp_path / "backlog.sqlite", flush_queue)
    original: FlushPayload = _payload(_recent(minutes=1))
    manager.enqueue(original)
    manager.replay_to_queue()
    assert flush_queue.get_nowait() is original


def test_the_flush_worker_receives_a_datetime_for_a_reloaded_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: disk -> new manager -> flush queue -> the real _flush_worker, which used to convert a string itself."""
    path: Path = tmp_path / "backlog.sqlite"
    m_time = datetime(2026, 6, 2, 14, 23, 11, 123456, tzinfo=timezone(timedelta(hours=2)))
    # Narrow-only (no wide table), so the worker needs no real table metadata behind its fake session.
    _manager(path, queue.Queue(), max_age=10**9).enqueue(_payload(m_time, wide_table_name=None))

    flush_queue: queue.Queue[FlushPayload | None] = queue.Queue()
    restarted: BacklogManager = _manager(path, flush_queue, max_age=10**9)
    restarted.load_from_disk()
    restarted.replay_to_queue()
    flush_queue.put(None)  # tells the worker to exit

    seen: list[object] = []

    class _Session:
        def begin(self) -> _Transaction:
            return _Transaction()

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _Transaction:
        def __enter__(self) -> _Transaction:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def capture(_data: object, _device_info_id: int, timestamp: object, *_rest: object) -> None:
        seen.append(timestamp)

    def not_stale(*_args: object, **_kwargs: object) -> bool:
        return False

    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def one_narrow_metric(*_args: object, **_kwargs: object) -> tuple[dict[str, Any], dict[str, float]]:
        return {}, {"metric": 1.0}

    bridge: Any = object.__new__(tsdb.timescaledb)
    bridge.close = noop
    bridge._log = logging.getLogger("test_timescaledb_backlog_payload")
    bridge._flush_queue = flush_queue
    bridge._flush_event = threading.Event()
    bridge._stop_event = threading.Event()
    bridge.migration_in_progress = threading.Event()
    bridge.schema_lock = threading.RLock()
    bridge._reconnect_lock = threading.RLock()
    bridge._reconnect_thread_running = False
    bridge.tsdb_connected = True
    bridge.enable_persistent_storage = True
    bridge._last_cleanup_time = time.time()
    bridge.SessionFactory = _Session
    monkeypatch.setattr(bridge, "_check_is_stale", not_stale)
    monkeypatch.setattr(bridge, "_commit_transport_state", noop)
    monkeypatch.setattr(bridge, "_process_raw_metrics", one_narrow_metric)
    monkeypatch.setattr(bridge, "_flush_batch_narrow", capture)

    bridge._flush_worker()

    (timestamp,) = seen
    assert isinstance(timestamp, datetime)
    assert timestamp == m_time


# ---------------------------------------------------------------------------
# compatibility with what is already on users' disks
# ---------------------------------------------------------------------------

def test_a_row_written_by_the_previous_version_still_loads(tmp_path: Path) -> None:
    """The on-disk format is unchanged: m_time is str(datetime) in both the column and the JSON."""
    path: Path = tmp_path / "backlog.sqlite"
    when: datetime = tsdb._now_tz().replace(microsecond=250000) - timedelta(minutes=10)
    legacy_payload: dict[str, Any] = {
        "device_info_id": 7,
        "metrics": {"battery_soc": 88.5},
        "m_time": str(when),
        "transport_name": "inv",
        "protocol": "eg4_18kpv",
        "wide_table_name": "device_metrics_wide__eg4_18kpv",
    }
    _insert_row(path, str(when), json.dumps(legacy_payload))

    (point,) = _reload(path)
    assert point["m_time"] == when
    assert point["device_info_id"] == 7
    assert point["metrics"] == {"battery_soc": 88.5}


def test_the_on_disk_text_is_still_str_of_the_datetime(tmp_path: Path) -> None:
    """Downgrade safety: an older build must still be able to read what this one writes."""
    path: Path = tmp_path / "backlog.sqlite"
    when: datetime = datetime(2026, 6, 2, 14, 23, 11, tzinfo=timezone.utc)
    _manager(path, queue.Queue()).enqueue(_payload(when))

    con = sqlite3.connect(str(path))
    try:
        column, payload_text = con.execute("SELECT m_time, payload FROM backlog").fetchone()
    finally:
        con.close()
    assert column == "2026-06-02 14:23:11+00:00"
    assert json.loads(payload_text)["m_time"] == "2026-06-02 14:23:11+00:00"


def test_expired_rows_are_still_dropped_by_age(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    writer: BacklogManager = _manager(path, queue.Queue(), max_age=10**9)
    writer.enqueue(_payload(tsdb._now_tz() - timedelta(hours=48)))
    writer.enqueue(_payload(_recent(minutes=1)))

    points: list[FlushPayload] = _reload_with_age(path, 86400)  # the default 24 h window
    assert len(points) == 1
    assert _row_count(path) == 1  # the expired row was deleted from disk too


# ---------------------------------------------------------------------------
# rows that are not a valid payload
# ---------------------------------------------------------------------------

_VALID: dict[str, Any] = {
    "device_info_id": 3,
    "metrics": {"a": 1},
    "transport_name": "t",
    "protocol": "p",
    "wide_table_name": "w",
}


@pytest.mark.parametrize(
    "bad_payload",
    [
        "{not json",
        "[1, 2, 3]",
        "null",
        json.dumps({k: v for k, v in _VALID.items() if k != "device_info_id"}),
        json.dumps({k: v for k, v in _VALID.items() if k != "metrics"}),
        json.dumps({k: v for k, v in _VALID.items() if k != "transport_name"}),
        json.dumps({k: v for k, v in _VALID.items() if k != "protocol"}),
        json.dumps({k: v for k, v in _VALID.items() if k != "wide_table_name"}),
        json.dumps({**_VALID, "device_info_id": "3"}),
        json.dumps({**_VALID, "device_info_id": True}),
        json.dumps({**_VALID, "metrics": [1, 2]}),
        json.dumps({**_VALID, "transport_name": 5}),
        json.dumps({**_VALID, "protocol": None}),
        json.dumps({**_VALID, "wide_table_name": 5}),
    ],
)
def test_a_row_that_is_not_a_valid_payload_is_dropped_not_loaded(tmp_path: Path, bad_payload: str) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    good: FlushPayload = _payload(_recent(minutes=2))
    writer: BacklogManager = _manager(path, queue.Queue())
    writer.enqueue(good)
    _insert_row(path, str(_recent(minutes=1)), bad_payload)  # lands after the good row
    assert _row_count(path) == 2

    points: list[FlushPayload] = _reload(path)
    assert points == [good]  # the good row loads; the bad one does not
    assert _row_count(path) == 1  # and is deleted from disk like any other corrupted row


def test_a_row_with_an_unparseable_m_time_column_is_dropped(tmp_path: Path) -> None:
    path: Path = tmp_path / "backlog.sqlite"
    _insert_row(path, "yesterday-ish", json.dumps(_VALID))
    assert _reload(path) == []
    assert _row_count(path) == 0
