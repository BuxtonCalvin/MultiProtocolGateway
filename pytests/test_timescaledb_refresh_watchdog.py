"""Unit tests for the RollupManager refresh watchdog (no database needed).

The watchdog alerts when a continuous-aggregate refresh is blocked by a lock. The alert says the refresh "has been
running for over 30 seconds", so the watchdog must not look at the database until 30 seconds have passed: checking at
once flagged the brief lock waits every refresh has when it starts. It also alerts once per refresh and says what is
holding the lock.
"""

# These tests deliberately exercise the manager's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import logging
import threading
import types
from typing import Any

import pytest

import classes.transports.timescaledb as tsdb

_VIEW = "daily_rollup_wide__eg4_18kpv"


class _Result:
    def __init__(self, one: Any = None, many: Any = ()) -> None:
        self._one: Any = one
        self._many: Any = many

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> Any:
        return list(self._many)


class _FakeSession:
    """Answers the watchdog's two queries: the refresh's wait event, then (only when blocked) who blocks it."""

    def __init__(self, statuses: list[str | None], sleeps: list[int], blockers: list[Any] | None = None) -> None:
        self.statuses: list[str | None] = list(statuses)  # wait_event_type per check; None = the refresh is no longer running
        self.blockers: list[Any] = blockers or []
        self.sleeps: list[int] = sleeps
        self.sleeps_before_first_check: int | None = None
        self.queries: list[str] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, statement: object, _params: object = None) -> _Result:
        sql: str = str(statement)
        self.queries.append(sql)
        if self.sleeps_before_first_check is None:
            self.sleeps_before_first_check = len(self.sleeps)
        if "pg_blocking_pids" in sql:
            return _Result(many=self.blockers)
        status: str | None = self.statuses.pop(0) if self.statuses else None
        return _Result(one=None if status is None else types.SimpleNamespace(wait_event_type=status))

    def rollback(self) -> None:
        return None


def _manager(session: _FakeSession, messages: list[str]) -> Any:
    manager: Any = object.__new__(tsdb.RollupManager)
    manager._log = logging.getLogger("test_timescaledb_refresh_watchdog")
    manager.SessionFactory = lambda: session

    def send_message(message: str = "", **_kwargs: object) -> None:
        messages.append(message)

    manager.send_message = send_message
    return manager


def _run(manager: Any, monkeypatch: pytest.MonkeyPatch, sleeps: list[int], *, refresh_finishes_first: bool = False) -> None:
    """Run the real watchdog thread to completion with sleeping replaced by a gate the test controls."""
    gate: threading.Event = threading.Event()

    def fake_sleep(_seconds: float) -> None:
        sleeps.append(1)
        gate.wait(5.0)

    monkeypatch.setattr(tsdb.time, "sleep", fake_sleep)
    stop_signal: list[bool] = manager._start_refresh_watchdog_helper(_VIEW)
    if refresh_finishes_first:
        stop_signal[0] = True  # what _refresh_single_rollup_helper does when the refresh completes
    gate.set()
    for thread in threading.enumerate():
        if thread.name == f"Watchdog_{_VIEW}":
            thread.join(5.0)
            assert not thread.is_alive()


def test_a_refresh_that_finishes_before_the_30_second_mark_never_touches_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    messages: list[str] = []
    session: _FakeSession = _FakeSession(["Lock"], sleeps)
    _run(_manager(session, messages), monkeypatch, sleeps, refresh_finishes_first=True)
    assert session.queries == []
    assert messages == []


def test_the_first_look_happens_only_after_30_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    session: _FakeSession = _FakeSession([None], sleeps)
    _run(_manager(session, []), monkeypatch, sleeps)
    assert session.sleeps_before_first_check is not None
    assert session.sleeps_before_first_check >= 30


def test_a_refresh_still_blocked_after_30_seconds_alerts_once_and_names_the_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    messages: list[str] = []
    blocker: Any = types.SimpleNamespace(
        pid=4242, state="idle in transaction", application_name="grafana", open_for="0:12:00", query="SELECT * FROM device_metrics"
    )
    session: _FakeSession = _FakeSession(["Lock", "Lock", None], sleeps, blockers=[blocker])  # blocked at two checks, then done
    _run(_manager(session, messages), monkeypatch, sleeps)

    assert len(messages) == 1  # once per refresh, not every 30 seconds
    assert f"{_VIEW} is BLOCKED by a lock" in messages[0]
    assert "pid 4242" in messages[0] and "grafana" in messages[0] and "idle in transaction" in messages[0]


def test_a_refresh_that_is_running_but_not_waiting_on_a_lock_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    messages: list[str] = []
    session: _FakeSession = _FakeSession(["IO", None], sleeps)
    _run(_manager(session, messages), monkeypatch, sleeps)
    assert messages == []
