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


def _no_sessions_text() -> str:
    return "No refresh session is running."


def test_blocked_views_warn_first_then_error_at_the_threshold_then_recover(caplog: pytest.LogCaptureFixture) -> None:
    rm: Any = _loop_manager(["v"])
    rm._describe_refresh_sessions_helper = _no_sessions_text
    threshold: int = tsdb.RollupManager.BLOCKED_REFRESH_ALERT_CYCLES
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        for _ in range(threshold):
            rm._note_blocked_refreshes_helper(["v"])
        assert [r.levelno for r in caplog.records] == [logging.WARNING] * (threshold - 1) + [logging.ERROR]
        assert "No refresh session is running." in caplog.records[-1].getMessage()  # the diagnosis is part of the error line

        caplog.clear()
        rm._note_blocked_refreshes_helper(["v"])  # past the threshold: quiet until it is time to repeat
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

        caplog.clear()
        rm._note_blocked_refreshes_helper([])
        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert rm._blocked_refresh_cycles == {}


def test_the_blocked_error_is_repeated_at_the_realert_interval(caplog: pytest.LogCaptureFixture) -> None:
    rm: Any = _loop_manager(["v"])
    rm._describe_refresh_sessions_helper = _no_sessions_text
    first: int = tsdb.RollupManager.BLOCKED_REFRESH_ALERT_CYCLES
    again: int = tsdb.RollupManager.BLOCKED_REFRESH_REALERT_CYCLES
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        for _ in range(first + again):
            rm._note_blocked_refreshes_helper(["v"])
    error_cycles: list[int] = [n for n, r in enumerate(caplog.records, start=1) if r.levelno == logging.ERROR]
    assert error_cycles == [first, first + again]


def test_the_wait_is_shortened_while_a_view_is_blocked() -> None:
    rm: Any = _loop_manager(["v"])
    rm.auto_refresh_interval = 21600
    assert rm._next_refresh_wait_helper([]) == 21600.0
    assert rm._next_refresh_wait_helper(["v"]) == tsdb.RollupManager.BLOCKED_REFRESH_RETRY_SECONDS
    rm.auto_refresh_interval = 60  # an interval already shorter than the retry wait is kept
    assert rm._next_refresh_wait_helper(["v"]) == 60.0


# ---------------------------------------------------------------------------
# refresh sessions: described in the log, and cancelled when stuck
# ---------------------------------------------------------------------------

def _session(pid: int, age_seconds: float, application_name: str = "Refresh Continuous Aggregate Policy [1012]") -> Any:
    return tsdb.RefreshSession(pid=pid, application_name=application_name, state="active", wait_event="Lock/relation", age_seconds=age_seconds)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value: Any = value

    def scalar(self) -> Any:
        return self._value


class _SignalSession:
    """Stands in for a SQLAlchemy session: records the pg_cancel_backend / pg_terminate_backend calls it is given."""

    def __init__(self, calls: list[tuple[str, int]], result: Any) -> None:
        self._calls: list[tuple[str, int]] = calls
        self._result: Any = result

    def __enter__(self) -> "_SignalSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any]) -> _Scalar:
        if isinstance(self._result, Exception):
            raise self._result
        self._calls.append(("terminate" if "terminate" in str(statement) else "cancel", params["pid"]))
        return _Scalar(self._result)


def _cancelling_manager(sessions: list[Any], limit: int = 3600, result: Any = True) -> tuple[Any, list[tuple[str, int]]]:
    rm: Any = _loop_manager(["v"])
    rm.stuck_refresh_cancel_after = limit
    rm._cancel_requested_pids = set()
    calls: list[tuple[str, int]] = []

    def fetch() -> list[Any]:
        return sessions

    def factory() -> _SignalSession:
        return _SignalSession(calls, result)

    rm._fetch_refresh_sessions_helper = fetch
    rm.SessionFactory = factory
    return rm, calls


def test_a_session_is_described_on_one_line_with_pid_application_state_wait_and_age() -> None:
    line: str = tsdb.RollupManager._format_refresh_session(_session(4242, 47 * 3600))
    assert line == "pid=4242 application_name='Refresh Continuous Aggregate Policy [1012]' state=active wait_event=Lock/relation age=47.0h"
    assert tsdb.RollupManager._format_refresh_session(_session(1, 343)).endswith("age=5.7m")
    assert tsdb.RollupManager._format_refresh_session(_session(1, 3)).endswith("age=3s")


def test_the_blocked_diagnosis_lists_the_running_sessions() -> None:
    rm, _ = _cancelling_manager([_session(10, 7200), _session(11, 60)])
    text: str = rm._describe_refresh_sessions_helper()
    assert text.startswith("Refresh session(s) running: pid=10 ")
    assert "pid=11 " in text


def test_the_blocked_diagnosis_says_so_when_no_session_is_running() -> None:
    rm, _ = _cancelling_manager([])
    assert "No refresh session is running" in rm._describe_refresh_sessions_helper()
    assert "restart or upgrade" in rm._describe_refresh_sessions_helper()


def test_the_blocked_diagnosis_survives_a_failed_session_listing() -> None:
    rm: Any = _loop_manager(["v"])

    def failing() -> list[Any]:
        raise _error(CONCURRENT)

    rm._fetch_refresh_sessions_helper = failing
    assert "could not be listed" in rm._describe_refresh_sessions_helper()


def test_a_session_older_than_the_limit_is_cancelled_and_logged_on_one_line(caplog: pytest.LogCaptureFixture) -> None:
    rm, calls = _cancelling_manager([_session(10, 47 * 3600), _session(11, 600)])
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        rm._cancel_stuck_refreshes_helper()
    assert calls == [("cancel", 10)]  # the 10 minute refresh is left alone
    errors: list[str] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "pid=10" in errors[0] and "wait_event=Lock/relation" in errors[0] and "age=47.0h" in errors[0] and "cancel sent" in errors[0]


def test_a_session_that_survives_the_cancel_is_terminated_on_the_next_cycle() -> None:
    rm, calls = _cancelling_manager([_session(10, 47 * 3600)])
    rm._cancel_stuck_refreshes_helper()
    rm._cancel_stuck_refreshes_helper()
    assert calls == [("cancel", 10), ("terminate", 10)]


def test_a_session_that_ended_is_forgotten_so_a_new_one_with_that_pid_is_cancelled_first() -> None:
    sessions: list[Any] = [_session(10, 47 * 3600)]
    rm, calls = _cancelling_manager(sessions)
    rm._cancel_stuck_refreshes_helper()
    sessions.clear()
    rm._cancel_stuck_refreshes_helper()
    sessions.append(_session(10, 47 * 3600))
    rm._cancel_stuck_refreshes_helper()
    assert calls == [("cancel", 10), ("cancel", 10)]


def test_a_limit_of_zero_never_cancels() -> None:
    rm, calls = _cancelling_manager([_session(10, 47 * 3600)], limit=0)
    rm._cancel_stuck_refreshes_helper()
    assert calls == []


def test_a_failed_cancel_is_reported_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    rm, calls = _cancelling_manager([_session(10, 47 * 3600)], result=_error("42501"))
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        rm._cancel_stuck_refreshes_helper()
    assert calls == []
    assert "cancel failed" in caplog.records[-1].getMessage()


def test_a_session_that_is_already_gone_is_reported_as_not_sent(caplog: pytest.LogCaptureFixture) -> None:
    rm, calls = _cancelling_manager([_session(10, 47 * 3600)], result=False)
    with caplog.at_level(logging.DEBUG, logger="refresh-retry-test"):
        rm._cancel_stuck_refreshes_helper()
    assert calls == [("cancel", 10)]
    assert "not sent" in caplog.records[-1].getMessage()
