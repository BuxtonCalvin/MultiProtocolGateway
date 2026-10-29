"""Unit tests for the TimescaleDB bridge's flush worker: recovering when the database returns, and queue accounting (no database needed).

tsdb_connected only goes True from the initial connect and from a successful reconnect attempt. If the database returned
in the gap between a reconnect thread exhausting reconnect_attempts and the next data write, that write succeeded but
nothing flipped the flag back: the backlog was never replayed and every tsdb_connected-gated job stayed disabled.

Queue accounting: every item the flush worker takes off the queue must be marked done exactly once. It used to count an
item twice when an exception escaped the write (the second task_done() raised ValueError and ended the worker thread),
and never counted an item taken just as a stop was requested.
"""

# These tests deliberately exercise the bridge's private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
import queue
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

import classes.transports.timescaledb as tsdb


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeEngine:
    """connect() fails while the 'server' is down, like an unreachable database, and works once it is up."""

    def __init__(self, up: bool) -> None:
        self.up: bool = up
        self.connects: int = 0

    def connect(self) -> _FakeConn:
        self.connects += 1
        if not self.up:
            raise OperationalError("SELECT 1", {}, Exception("server unreachable"))
        return _FakeConn()


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakeSession:
    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _noop(*_args: object, **_kwargs: object) -> None:
    """Stands in for any hook the test does not care about."""
    return None


def _not_stale(*_args: object, **_kwargs: object) -> bool:
    return False


def _one_narrow_metric(*_args: object, **_kwargs: object) -> tuple[dict[str, Any], dict[str, float]]:
    """_process_raw_metrics(): no wide data, one narrow metric, so the worker takes the narrow-write path."""
    return {}, {"metric": 1.0}


def _join_reconnect_thread(timeout: float = 5.0) -> None:
    for thread in threading.enumerate():
        if thread.name == "TSDB ReconnectThread":
            thread.join(timeout)


@pytest.fixture(autouse=True)
def _isolated_disconnect_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The connected setter persists a "connection lost, not yet reported restored" marker; keep it out of the repo."""

    def marker_in_tmp(_self: object) -> Path:
        return tmp_path / "disconnect.marker"

    monkeypatch.setattr(tsdb.timescaledb, "_disconnect_marker_file", marker_in_tmp)


class _FakeBacklog:
    """The slice of BacklogManager the flush worker and reconnect worker use, recording what they do to it."""

    def __init__(self, events: list[str]) -> None:
        self.events: list[str] = events
        self.released: list[int] = []
        self.acked: list[int] = []

    def replay_to_queue(self) -> int:
        self.events.append("replay")
        return 0

    def enqueue(self, _payload: object) -> bool:
        self.events.append("enqueue")
        return True

    def ack(self, rowid: int) -> None:
        self.acked.append(rowid)

    def release(self, rowid: int) -> None:
        self.released.append(rowid)

    def commit_acks(self) -> None:
        return None


@pytest.fixture
def bridge() -> Any:
    """A timescaledb bridge with just enough state for its reconnect and flush-worker code, flagged disconnected."""
    b: Any = object.__new__(tsdb.timescaledb)
    b.close = lambda: None  # __del__ calls close(), which needs a fully built bridge
    b._log = logging.getLogger("test_timescaledb_reconnect_recovery")
    b._reconnect_lock = threading.RLock()
    b._reconnect_thread_running = False
    b._stop_event = threading.Event()
    b._stop_reconnect_event = threading.Event()
    b.tsdb_connected = False
    # transport_base's connected setter (reached via _set_tsdb_connected) reads this state and sends the alerts.
    b._connected = False
    b._ever_connected = True
    b._needs_reconnection = True
    b.transport_name = "transport.timescaledb"
    b.rollup_policy = {"tsdb_connected": False}
    b.reconnect_attempts = 2
    b.reconnect_delay = 0
    b.use_exponential_backoff = True
    b.max_reconnect_delay = 300
    b.engine = _FakeEngine(up=False)
    b.enable_persistent_storage = True
    b._backlog_lock = threading.RLock()
    events: list[str] = []
    b.events = events
    b._rediscover_protocols = lambda: events.append("rediscover")
    b.rollup_mgr = types.SimpleNamespace(repopulate_known_rollup_views=lambda: events.append("repopulate"))

    b.backlog = _FakeBacklog(events)
    messages: list[str] = []
    b.messages = messages

    def send_message(message: str = "", **_kwargs: object) -> None:
        messages.append(message)

    b.send_message = send_message
    return b


# ---------------------------------------------------------------------------
# _recover_if_flagged_disconnected
# ---------------------------------------------------------------------------

def test_recovery_hook_is_a_noop_when_already_connected(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.tsdb_connected = True
    triggers: list[int] = []
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: triggers.append(1))
    bridge._recover_if_flagged_disconnected()
    assert triggers == []


def test_recovery_hook_does_nothing_while_a_reconnect_is_already_running(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge._reconnect_thread_running = True
    triggers: list[int] = []
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: triggers.append(1))
    bridge._recover_if_flagged_disconnected()
    assert triggers == []


def test_recovery_hook_starts_a_reconnect_when_flagged_disconnected(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    triggers: list[int] = []
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: triggers.append(1))
    bridge._recover_if_flagged_disconnected()
    assert triggers == [1]


def test_database_returning_after_reconnect_attempts_were_exhausted_is_recovered(bridge: Any) -> None:
    """The original bug, end to end, using the real reconnect worker."""
    bridge._reconnect_thread_running = True  # as _trigger_reconnect would have set it before starting the worker
    bridge._attempt_reconnect()  # the database is down, so every attempt fails and the worker gives up
    assert bridge.engine.connects == 2
    assert bridge.tsdb_connected is False
    assert bridge._reconnect_thread_running is False  # the worker exited; nothing is retrying any more
    assert bridge.events == []  # and no recovery ran
    assert bridge.messages == []  # (it was already flagged disconnected, so no new alert either)

    bridge.engine.up = True  # the database comes back in the gap before the next write
    bridge._recover_if_flagged_disconnected()  # what the flush worker now does after a write commits
    _join_reconnect_thread()

    assert bridge.tsdb_connected is True
    assert bridge.rollup_policy["tsdb_connected"] is True  # the rollup/hypertable managers see it too
    assert bridge.events == ["rediscover", "repopulate", "replay"]  # the same recovery any reconnect gets
    assert [m.split(":")[0] for m in bridge.messages] == ["Connection restored"]  # and the user is told


# ---------------------------------------------------------------------------
# the flush worker's use of it
# ---------------------------------------------------------------------------

def _item(n: int = 0) -> dict[str, Any]:
    return {
        "device_info_id": 1,
        "m_time": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=n),
        "metrics": {"metric": 1},
        "transport_name": "transport.test",
        "protocol": "proto",
        "wide_table_name": None,
    }


def _queue_of(*items: dict[str, Any]) -> queue.Queue[dict[str, Any] | None]:
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    for item in items:
        q.put(item)
    q.put(None)  # tells the worker to exit
    return q


def _prime_flush_worker(bridge: Any, monkeypatch: pytest.MonkeyPatch, narrow: Callable[..., None]) -> None:
    bridge.SessionFactory = lambda: _FakeSession()
    bridge._last_cleanup_time = time.time()
    bridge._flush_event = threading.Event()
    bridge.migration_in_progress = threading.Event()
    bridge.schema_lock = threading.RLock()
    monkeypatch.setattr(bridge, "_check_is_stale", _not_stale)
    monkeypatch.setattr(bridge, "_commit_transport_state", _noop)
    monkeypatch.setattr(bridge, "_process_raw_metrics", _one_narrow_metric)
    monkeypatch.setattr(bridge, "_flush_batch_narrow", narrow)


def _failing_write(*_args: Any, **_kwargs: Any) -> None:
    raise OperationalError("INSERT", {}, Exception("connection lost"))


@pytest.mark.parametrize(("connected", "expected_reconnects"), [(False, 1), (True, 0)])
def test_flush_worker_starts_a_reconnect_after_a_write_only_when_flagged_disconnected(
    bridge: Any, monkeypatch: pytest.MonkeyPatch, connected: bool, expected_reconnects: int
) -> None:
    bridge.tsdb_connected = connected
    _prime_flush_worker(bridge, monkeypatch, narrow=_noop)
    reconnects: list[int] = []
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: reconnects.append(1))
    bridge._flush_queue = _queue_of(_item())
    bridge._flush_worker()
    assert len(reconnects) == expected_reconnects


def test_flush_worker_does_not_run_the_recovery_check_after_a_failed_write(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _prime_flush_worker(bridge, monkeypatch, narrow=_failing_write)
    checks: list[int] = []
    reconnects: list[int] = []
    monkeypatch.setattr(bridge, "_recover_if_flagged_disconnected", lambda: checks.append(1))
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: reconnects.append(1))
    bridge._flush_queue = _queue_of(_item())
    bridge._flush_worker()
    assert checks == []  # a failed write is not proof the database is back
    assert reconnects == [1]  # the existing failure handling is unchanged
    assert bridge.events == ["enqueue"]  # and the data still goes to the backlog


def test_a_failing_recovery_check_cannot_take_down_the_flush_worker(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[int] = []

    def counting_write(*_args: object, **_kwargs: object) -> None:
        writes.append(1)

    _prime_flush_worker(bridge, monkeypatch, narrow=counting_write)

    def broken() -> None:
        raise RuntimeError("could not start thread")

    monkeypatch.setattr(bridge, "_recover_if_flagged_disconnected", broken)
    bridge._flush_queue = _queue_of(_item(0), _item(1))
    bridge._flush_worker()  # must return normally, not die on a double task_done()
    assert len(writes) == 2  # both items were processed
    assert bridge._flush_queue.unfinished_tasks == 0


# ---------------------------------------------------------------------------
# the flush worker marks every item done exactly once
# ---------------------------------------------------------------------------

def _unexpected_error(*_args: object, **_kwargs: object) -> None:
    raise KeyError("something the write path does not expect")


def _stale(*_args: object, **_kwargs: object) -> bool:
    return True


def _no_metrics(*_args: object, **_kwargs: object) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


class _FullDiskBacklog(_FakeBacklog):
    def enqueue(self, _payload: object) -> bool:
        raise OSError("No space left on device")


def _apply_scenario(bridge: Any, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    """Configure the real flush worker so that every item takes one particular path through it."""
    narrow: Callable[..., None] = _noop
    if scenario in ("write_fails", "backlog_write_fails"):
        narrow = _failing_write
    elif scenario == "unexpected_error":
        narrow = _unexpected_error
    _prime_flush_worker(bridge, monkeypatch, narrow=narrow)
    if scenario == "stale":
        monkeypatch.setattr(bridge, "_check_is_stale", _stale)
    elif scenario == "no_data":
        monkeypatch.setattr(bridge, "_process_raw_metrics", _no_metrics)
    elif scenario == "backlog_write_fails":
        bridge.backlog = _FullDiskBacklog(bridge.events)
    monkeypatch.setattr(bridge, "_trigger_reconnect", _noop)  # the recovery path is tested above; no threads here


@pytest.mark.parametrize(
    "scenario",
    ["success", "stale", "no_data", "write_fails", "unexpected_error", "backlog_write_fails"],
)
def test_every_item_is_marked_done_exactly_once_on_every_path(bridge: Any, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    _apply_scenario(bridge, monkeypatch, scenario)
    bridge._flush_queue = _queue_of(_item(0), _item(1), _item(2))
    bridge._flush_worker()  # must return normally: a second task_done() per item raised ValueError and ended the worker
    assert bridge._flush_queue.unfinished_tasks == 0
    assert bridge._flush_queue.empty()  # and every item, including the shutdown sentinel, was consumed


def test_the_worker_keeps_going_after_an_unexpected_exception(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def fail_first_then_succeed(*_args: object, **_kwargs: object) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise KeyError("something the write path does not expect")

    _prime_flush_worker(bridge, monkeypatch, narrow=fail_first_then_succeed)
    monkeypatch.setattr(bridge, "_trigger_reconnect", _noop)
    commits: list[int] = []

    def record_commit(*_args: object, **_kwargs: object) -> None:
        commits.append(1)

    monkeypatch.setattr(bridge, "_commit_transport_state", record_commit)
    bridge._flush_queue = _queue_of(_item(0), _item(1))
    bridge._flush_worker()
    assert len(attempts) == 2  # the second item was still attempted
    assert len(commits) == 1  # and the one that did not fail was committed


def test_an_exception_in_the_backlog_does_not_end_the_worker(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write is routed to the backlog; if that fails too (disk full) the worker must survive it."""
    _apply_scenario(bridge, monkeypatch, "backlog_write_fails")
    bridge._flush_queue = _queue_of(_item(0), _item(1))
    bridge._flush_worker()
    assert bridge._flush_queue.unfinished_tasks == 0


def test_an_item_taken_as_a_stop_is_requested_is_still_marked_done(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[int] = []

    def counting_write(*_args: object, **_kwargs: object) -> None:
        writes.append(1)

    _prime_flush_worker(bridge, monkeypatch, narrow=counting_write)
    bridge._stop_event.set()  # a stop was requested while this item was in hand
    bridge._flush_queue = _queue_of(_item(0))
    bridge._flush_worker()
    assert writes == []  # it was not written (unchanged behavior) ...
    assert bridge._flush_queue.unfinished_tasks == 1  # ... but it is no longer counted as outstanding; only the unread sentinel is
    assert bridge._flush_queue.qsize() == 1


def test_queue_join_does_not_return_before_the_last_item_is_processed(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    RollupManager waits on queue.join() while a replayed backlog drains. Counting a failed item twice used up the
    count early, so join() returned while later items were still unprocessed: two unexpected failures were enough to
    release it with the third still in flight.
    """
    third_started = threading.Event()
    release_third = threading.Event()
    third_time = _item(2)["m_time"]

    def fail_the_first_two_then_block(_data: object, _device_info_id: int, timestamp: datetime, *_rest: object) -> None:
        if timestamp != third_time:
            raise KeyError("something the write path does not expect")
        third_started.set()
        release_third.wait(timeout=5.0)

    _prime_flush_worker(bridge, monkeypatch, narrow=fail_the_first_two_then_block)
    monkeypatch.setattr(bridge, "_trigger_reconnect", _noop)
    bridge._flush_queue = _queue_of(_item(0), _item(1), _item(2))
    worker = threading.Thread(target=bridge._flush_worker, daemon=True)
    worker.start()
    assert third_started.wait(timeout=5.0)  # items 0 and 1 have failed; item 2 is in flight

    joined = threading.Event()

    def wait_for_join() -> None:
        bridge._flush_queue.join()
        joined.set()

    threading.Thread(target=wait_for_join, daemon=True).start()
    assert not joined.wait(timeout=0.3)  # still waiting: item 2 has not finished

    release_third.set()
    assert joined.wait(timeout=5.0)  # and released once everything, including the sentinel, is done
    worker.join(timeout=5.0)
    assert not worker.is_alive()


# ---------------------------------------------------------------------------
# connection notices, rejected rows and replayed points
# ---------------------------------------------------------------------------

def test_the_connection_notice_is_sent_after_the_locks_are_released(bridge: Any) -> None:
    """Messaging is network I/O: it must never run while _reconnect_lock is held (it would stall the flush and write paths)."""
    lock_held_while_sending: list[bool] = []

    def send_message(message: str = "", **_kwargs: object) -> None:
        bridge.messages.append(message)
        lock_held_while_sending.append(bridge._reconnect_lock._is_owned())

    bridge.send_message = send_message
    bridge.tsdb_connected = True
    bridge._connected = True

    bridge._set_tsdb_connected(conn_value=False, conn_reason="test")

    assert [m.split(":")[0] for m in bridge.messages] == ["Connection lost"]
    assert lock_held_while_sending == [False]


def test_a_row_the_database_rejects_is_not_treated_as_an_outage(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A constraint violation is the database answering: no "connection lost" alert, no reconnect, no backlog."""

    def rejected(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    bridge.tsdb_connected = True
    bridge._connected = True
    bridge.rollup_policy["tsdb_connected"] = True
    _prime_flush_worker(bridge, monkeypatch, narrow=rejected)
    reconnects: list[int] = []
    monkeypatch.setattr(bridge, "_trigger_reconnect", lambda: reconnects.append(1))
    bridge._flush_queue = _queue_of(_item())
    bridge._flush_worker()

    assert reconnects == []
    assert bridge.tsdb_connected is True
    assert bridge.messages == []
    assert bridge.events == []  # dropped, not journalled
    assert bridge._flush_queue.unfinished_tasks == 0


def test_a_replayed_point_is_kept_when_the_database_is_down_and_acknowledged_when_written(
    bridge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replayed: dict[str, Any] = {**_item(0), "backlog_rowid": 41}

    _prime_flush_worker(bridge, monkeypatch, narrow=_failing_write)
    monkeypatch.setattr(bridge, "_trigger_reconnect", _noop)
    bridge._flush_queue = _queue_of(replayed)
    bridge._flush_worker()
    assert bridge.backlog.released == [41]  # still in the backlog; the next replay re-sends it
    assert bridge.backlog.acked == []
    assert bridge.events == []  # and it was not journalled a second time

    _prime_flush_worker(bridge, monkeypatch, narrow=_noop)
    bridge._flush_queue = _queue_of(replayed)
    bridge._flush_worker()
    assert bridge.backlog.acked == [41]  # written: now it may leave the backlog


def test_a_live_point_in_hand_at_shutdown_is_journalled_not_lost(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[int] = []

    def counting_write(*_args: object, **_kwargs: object) -> None:
        writes.append(1)

    _prime_flush_worker(bridge, monkeypatch, narrow=counting_write)
    bridge._stop_event.set()
    bridge._flush_queue = _queue_of(_item(0))
    bridge._flush_worker()
    assert writes == []
    assert bridge.events == ["enqueue"]
