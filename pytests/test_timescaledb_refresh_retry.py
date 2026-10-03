"""Unit tests for the concurrent-refresh retry in classes.transports.timescaledb (no database needed)."""

# These tests deliberately exercise RollupManager's private helpers.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

import classes.transports.timescaledb as tsdb


class _FakeOrig(Exception):
    """Stands in for psycopg2.errors.LockNotAvailable, which cannot be built with a pgcode by hand."""

    def __init__(self, message: str, pgcode: str) -> None:
        super().__init__(message)
        self.pgcode: str = pgcode


CONCURRENT = "could not refresh continuous aggregate \"v\" due to a concurrent refresh"
LOCK_TIMEOUT = "canceling statement due to lock timeout"


def _error(message: str, pgcode: str = "55P03") -> DBAPIError:
    return OperationalError("CALL refresh_continuous_aggregate(...)", {}, _FakeOrig(message, pgcode))


class _FakeConn:
    """execute() raises each queued error in turn, then succeeds."""

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors: list[BaseException] = list(errors)
        self.calls: int = 0

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


class _RecordingEvent(threading.Event):
    """Records requested waits instead of sleeping; optionally reports that shutdown was requested."""

    def __init__(self, stop: bool = False) -> None:
        super().__init__()
        self.waits: list[float] = []
        self._stop: bool = stop

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0.0)
        return self._stop


def _manager(event: threading.Event) -> Any:
    rm: Any = tsdb.RollupManager.__new__(tsdb.RollupManager)
    rm._log = logging.getLogger("refresh-retry-test")
    rm._stop_refresh_rollup_event = event
    return rm


def test_predicate_matches_only_concurrent_refresh() -> None:
    assert tsdb.RollupManager._is_concurrent_refresh_error_helper(_error(CONCURRENT))
    assert not tsdb.RollupManager._is_concurrent_refresh_error_helper(_error(LOCK_TIMEOUT))  # same SQLSTATE, ordinary lock timeout
    assert not tsdb.RollupManager._is_concurrent_refresh_error_helper(_error(CONCURRENT, pgcode="42P01"))
    assert not tsdb.RollupManager._is_concurrent_refresh_error_helper(ValueError(CONCURRENT))


def test_retries_then_succeeds() -> None:
    event = _RecordingEvent()
    conn = _FakeConn([_error(CONCURRENT), _error(CONCURRENT)])
    _manager(event)._execute_refresh_with_retry(conn, "CALL x()", {}, "v")
    assert conn.calls == 3
    assert event.waits == list(tsdb.RollupManager.CONCURRENT_REFRESH_RETRY_DELAYS[:2])


def test_gives_up_after_last_retry_and_raises_original() -> None:
    event = _RecordingEvent()
    n = len(tsdb.RollupManager.CONCURRENT_REFRESH_RETRY_DELAYS)
    conn = _FakeConn([_error(CONCURRENT) for _ in range(n + 1)])
    with pytest.raises(DBAPIError, match="concurrent refresh"):
        _manager(event)._execute_refresh_with_retry(conn, "CALL x()", {}, "v")
    assert conn.calls == n + 1
    assert event.waits == list(tsdb.RollupManager.CONCURRENT_REFRESH_RETRY_DELAYS)


def test_other_errors_are_not_retried() -> None:
    event = _RecordingEvent()
    for err in (_error(LOCK_TIMEOUT), _error("relation does not exist", pgcode="42P01")):
        conn = _FakeConn([err])
        with pytest.raises(DBAPIError):
            _manager(event)._execute_refresh_with_retry(conn, "CALL x()", {}, "v")
        assert conn.calls == 1
    assert event.waits == []


def test_shutdown_during_wait_raises_without_further_attempts() -> None:
    event = _RecordingEvent(stop=True)
    conn = _FakeConn([_error(CONCURRENT), _error(CONCURRENT)])
    with pytest.raises(DBAPIError):
        _manager(event)._execute_refresh_with_retry(conn, "CALL x()", {}, "v")
    assert conn.calls == 1
    assert len(event.waits) == 1
