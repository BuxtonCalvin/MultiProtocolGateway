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


# ---------------------------------------------------------------------------
# one blocked view must not stop the views after it from being refreshed
# ---------------------------------------------------------------------------

def _loop_manager(views: list[str], event: threading.Event | None = None) -> Any:
    rm: Any = _manager(event if event is not None else threading.Event())
    rm.known_rollup_views = dict.fromkeys(views, "1 hour")
    rm._blocked_refresh_cycles = {}

    def view_exists(_conn: Any, _view_name: str) -> bool:
        return True

    rm._view_exists_conn_helper = view_exists
    return rm


def _refuse(rm: Any, blocked: set[str], refreshed: list[str], error: DBAPIError | None = None) -> None:
    """Make _execute_refresh_with_retry record each view and refuse those in ``blocked``."""

    def fake(_conn: Any, _statement: str, params: dict[str, Any], view_name: str) -> None:
        refreshed.append(view_name)
        if view_name in blocked:
            raise error if error is not None else _error(CONCURRENT)

    rm._execute_refresh_with_retry = fake


def test_a_blocked_view_is_skipped_and_the_views_after_it_are_still_refreshed() -> None:
    rm: Any = _loop_manager(["hourly_rollup_narrow", "monthly_rollup_narrow", "monthly_rollup_wide__eg4_18kpv"])
    refreshed: list[str] = []
    _refuse(rm, {"monthly_rollup_narrow"}, refreshed)
    assert rm._refresh_known_views_helper(object()) == ["monthly_rollup_narrow"]
    assert refreshed == ["hourly_rollup_narrow", "monthly_rollup_narrow", "monthly_rollup_wide__eg4_18kpv"]


def test_no_view_blocked_returns_an_empty_list() -> None:
    rm: Any = _loop_manager(["a", "b"])
    refreshed: list[str] = []
    _refuse(rm, set(), refreshed)
    assert rm._refresh_known_views_helper(object()) == []
    assert refreshed == ["a", "b"]


def test_an_error_that_is_not_a_concurrent_refresh_still_aborts_the_cycle() -> None:
    rm: Any = _loop_manager(["a", "b"])
    refreshed: list[str] = []
    _refuse(rm, {"a"}, refreshed, error=_error(LOCK_TIMEOUT))
    with pytest.raises(DBAPIError):
        rm._refresh_known_views_helper(object())
    assert refreshed == ["a"]


def test_shutdown_during_a_blocked_refresh_is_not_swallowed() -> None:
    event: threading.Event = threading.Event()
    rm: Any = _loop_manager(["a", "b"], event)
    refreshed: list[str] = []

    def fake(_conn: Any, _statement: str, _params: dict[str, Any], view_name: str) -> None:
        refreshed.append(view_name)
        event.set()  # what _execute_refresh_with_retry's wait reports when the app is stopping
        raise _error(CONCURRENT)

    rm._execute_refresh_with_retry = fake
    with pytest.raises(DBAPIError):
        rm._refresh_known_views_helper(object())
    assert refreshed == ["a"]


def test_a_view_that_does_not_exist_is_skipped_without_being_reported_as_blocked() -> None:
    rm: Any = _loop_manager(["a", "b"])

    def view_exists(_conn: Any, view_name: str) -> bool:
        return view_name != "a"

    rm._view_exists_conn_helper = view_exists
    refreshed: list[str] = []
    _refuse(rm, set(), refreshed)
    assert rm._refresh_known_views_helper(object()) == []
    assert refreshed == ["b"]


def test_blocked_views_warn_first_then_error_at_the_threshold_then_recover(caplog: pytest.LogCaptureFixture) -> None:
    rm: Any = _loop_manager(["v"])
    threshold: int = tsdb.RollupManager.BLOCKED_REFRESH_ALERT_CYCLES
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        for _ in range(threshold):
            rm._note_blocked_refreshes_helper(["v"])
        levels: list[int] = [r.levelno for r in caplog.records]
        assert levels == [logging.WARNING] * (threshold - 1) + [logging.ERROR]

        caplog.clear()
        rm._note_blocked_refreshes_helper(["v"])  # past the threshold: quiet until the next multiple
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

        caplog.clear()
        rm._note_blocked_refreshes_helper([])
        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert rm._blocked_refresh_cycles == {}
