"""Unit tests for RollupManager helpers that decide what gets dropped, rebuilt or deleted (no database needed)."""

# These tests deliberately exercise the manager's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import logging
import threading
import types
from contextlib import contextmanager
from typing import Any, Generator

import pytest
from sqlalchemy.exc import OperationalError

import classes.transports.timescaledb as tsdb


def _noop(*_args: object, **_kwargs: object) -> None:
    """Stand-in for a collaborator method whose effect does not matter to the test."""
    return None


def _noop_one(_argument: object) -> None:
    return None


def _manager() -> Any:
    manager: Any = object.__new__(tsdb.RollupManager)
    manager._log = logging.getLogger("test_timescaledb_rollup_helpers")
    known_views: dict[str, object] = {}
    manager.known_rollup_views = known_views
    return manager


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows: list[Any] = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _RecordingSession:
    def __init__(self, view_rows: list[tuple[str, str]] | None = None) -> None:
        self.view_rows: list[tuple[str, str]] = view_rows or []
        self.statements: list[str] = []

    def execute(self, statement: object, _params: object = None) -> _Rows:
        sql: str = str(statement)
        self.statements.append(sql)
        return _Rows(self.view_rows if "FROM timescaledb_information.continuous_aggregates" in sql else [])

    @contextmanager
    def begin_nested(self) -> Generator[None, None, None]:
        yield

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.mark.parametrize(
    ("view_name", "granularity"),
    [
        ("hourly_rollup_narrow", "hourly"),
        ("daily_rollup_wide__eg4_18kpv", "daily"),
        ("weekly_rollup_wide__eg4_18kpv", "weekly"),
        ("monthly_rollup_narrow", "monthly"),
        # the protocol's own name contains another granularity word: the prefix still decides
        ("weekly_rollup_wide__hourly_meter", "weekly"),
        ("hourly_rollup_wide__monthly_totals", "hourly"),
        ("something_else", "default"),
    ],
)
def test_the_granularity_comes_from_the_view_name_prefix(view_name: str, granularity: str) -> None:
    assert tsdb.RollupManager._granularity_from_view_name_helper(view_name) == granularity


def test_a_database_error_while_checking_a_rollup_is_raised_not_answered_with_rebuild() -> None:
    """"rebuild" makes ensure_rollups drop every continuous aggregate; an error reading the catalog must never cause that."""

    class _FailingSession:
        def scalar(self, *_args: object, **_kwargs: object) -> str:
            raise OperationalError("SELECT", {}, Exception("server closed the connection unexpectedly"))

    with pytest.raises(OperationalError):
        _manager().rollup_needs_rebuild(_FailingSession(), "daily_rollup_narrow", "1 day")


def test_views_are_dropped_weekly_then_daily_then_hourly_whatever_the_protocol_is_called() -> None:
    session: _RecordingSession = _RecordingSession([
        ("public", "hourly_rollup_wide__weekly_meter"),  # an hourly view whose protocol name contains "weekly"
        ("public", "daily_rollup_wide__weekly_meter"),
        ("public", "weekly_rollup_wide__weekly_meter"),
    ])
    manager: Any = _manager()

    def dynamic_settings() -> dict[str, str]:
        return {"lock_timeout": "5s", "work_mem": "64MB"}

    def set_lock_timeout(_session: object, _timeout: object) -> None:
        return None

    manager.hypertable_mgr = types.SimpleNamespace(
        get_dynamic_settings_helper=dynamic_settings,
        set_lock_timeout=set_lock_timeout,
    )
    manager._drop_all_continuous_aggregates(session)

    dropped: list[str] = [s for s in session.statements if s.startswith("DROP MATERIALIZED VIEW")]
    assert [d.split('"."')[1].split('"')[0].split("_")[0] for d in dropped] == ["weekly", "daily", "hourly"]


def test_the_ghost_job_sweep_only_treats_a_job_as_orphaned_when_its_target_is_gone() -> None:
    """A retention policy on a raw hypertable must survive the sweep, and workers are matched on their bracketed job id."""
    session: _RecordingSession = _RecordingSession()
    _manager()._purge_ghost_jobs_helper(session)

    sql: str = session.statements[0]
    assert "timescaledb_information.hypertables" in sql  # a job on a hypertable is not an orphan
    assert "'%[' || t.job_id || ']%'" in sql  # not '%' || job_id || '%', which also matches job 10, 100... and unrelated sessions
    assert "'%' || t.job_id || '%'" not in sql


# ---------------------------------------------------------------------------
# a protocol whose rollup views could not be created must stay "incomplete"
# ---------------------------------------------------------------------------

def _setup_manager(views_error: Exception | None, marked: list[tuple[str, bool]]) -> Any:
    manager: Any = _manager()
    manager.enable_rollups = True
    manager.migration_in_progress = tsdb.MigrationGate()

    def ensure_views(*_args: object, **_kwargs: object) -> None:
        if views_error is not None:
            raise views_error

    def mark_complete(protocol: str, complete: bool = True) -> None:
        marked.append((protocol, complete))

    def dynamic_raw_table_settings(**_kwargs: object) -> tuple[str, str, str]:
        return ("1 day", "7 days", "Static")

    manager._ensure_cagg_views_for_protocol = ensure_views
    manager._schedule_initial_refresh = _noop
    manager._refresh_protocol_rollups_helper = _noop
    manager._initial_refresh_lock = threading.Lock()
    manager._changed_rollup_stacks = set()
    manager.full_refresh_on_startup = False
    manager.mark_rollup_setup_complete_helper = mark_complete
    column_counts: dict[str, int] = {}
    manager.hypertable_mgr = types.SimpleNamespace(
        ensure_hypertables=_noop,
        enable_compression=False,
        apply_retention_policy=_noop_one,
        get_dynamic_raw_table_settings_helper=dynamic_raw_table_settings,
        raw_wide_chunk_time_interval="1 day",
        raw_wide_compress_after_interval="7 days",
        compress_segmentby_wide="device_info_id",
        flag_pending_retune=_noop_one,
        protocol_wide_column_counts=column_counts,
    )

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, *_args: object, **_kwargs: object) -> Any:
            return types.SimpleNamespace(scalar=lambda: 12)

    manager.SessionFactory = _Session
    return manager


def test_add_wide_rollup_marks_the_protocol_complete_when_the_views_were_created() -> None:
    marked: list[tuple[str, bool]] = []
    _setup_manager(None, marked).add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert marked == [("eg4_18kpv", True)]


def test_add_wide_rollup_does_not_mark_the_protocol_complete_when_view_creation_failed() -> None:
    """Marking it complete would stop the reconnect/rediscovery path from ever retrying: it only retries incomplete protocols."""
    marked: list[tuple[str, bool]] = []
    manager: Any = _setup_manager(OperationalError("CREATE", {}, Exception("lock timeout")), marked)
    with pytest.raises(RuntimeError, match="could not be created"):
        manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert marked == []
    assert not manager.migration_in_progress.is_set()  # and the flush worker is released again


def test_a_failure_in_the_shared_narrow_flow_is_still_only_logged() -> None:
    manager: Any = _setup_manager(None, [])

    def failing_setup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("narrow views failed")

    manager.setup_with_retry = failing_setup
    manager.refresh_rollups = _noop
    manager._schedule_initial_refresh = _noop
    manager.setup_rollup(
        table_name="device_metrics_narrow", segment_by="device_info_id", compress_after_interval="7 days",
        chunk_time_interval="1 day", protocol_name=None, wide_table_name=None, use_shared_rollup_flow=True,
    )  # does not raise


# ---------------------------------------------------------------------------
# the startup full refresh runs on a background worker, never inside init_bridge's call chain
# ---------------------------------------------------------------------------

def _recording_schedule(manager: Any) -> list[tuple[str, str | None, str | None]]:
    scheduled: list[tuple[str, str | None, str | None]] = []

    def record_schedule(kind: str, protocol_name: str | None, wide_table_name: str | None) -> None:
        scheduled.append((kind, protocol_name, wide_table_name))

    manager._schedule_initial_refresh = record_schedule
    return scheduled


def _run_shared_setup(manager: Any) -> None:
    manager.setup_rollup(
        table_name="device_metrics_narrow", segment_by="device_info_id", compress_after_interval="7 days",
        chunk_time_interval="1 day", protocol_name=None, wide_table_name=None, use_shared_rollup_flow=True,
    )


def test_an_existing_matching_protocol_stack_skips_the_startup_full_refresh() -> None:
    manager: Any = _setup_manager(None, [])
    scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(manager)
    manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == []


def test_a_protocol_stack_whose_views_were_just_created_is_fully_refreshed_once() -> None:
    manager: Any = _setup_manager(None, [])
    scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(manager)

    def ensure_views(protocol_name: str, _wide_table_name: str | None, force: bool = False) -> None:
        manager._mark_rollup_stack_changed_helper(protocol_name)

    manager._ensure_cagg_views_for_protocol = ensure_views
    manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == [("protocol", "eg4_18kpv", "device_metrics_wide__eg4_18kpv")]

    scheduled.clear()
    manager._ensure_cagg_views_for_protocol = _noop  # the mark was consumed: a later unchanged setup does not repeat it
    manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == []


def test_a_protocol_stack_whose_setup_failed_is_still_refreshed_and_the_failure_still_raised() -> None:
    manager: Any = _setup_manager(OperationalError("CREATE", {}, Exception("lock timeout")), [])
    scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(manager)
    with pytest.raises(RuntimeError, match="could not be created"):
        manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == [("protocol", "eg4_18kpv", "device_metrics_wide__eg4_18kpv")]


def test_full_refresh_on_startup_forces_the_refresh_even_when_nothing_changed() -> None:
    manager: Any = _setup_manager(None, [])
    manager.full_refresh_on_startup = True
    scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(manager)
    manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == [("protocol", "eg4_18kpv", "device_metrics_wide__eg4_18kpv")]


def test_an_existing_matching_shared_narrow_stack_skips_the_startup_full_refresh() -> None:
    manager: Any = _setup_manager(None, [])
    manager.setup_with_retry = _noop
    scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(manager)
    _run_shared_setup(manager)
    assert scheduled == []


def test_a_shared_narrow_stack_that_was_rebuilt_or_whose_setup_failed_is_fully_refreshed() -> None:
    rebuilt: Any = _setup_manager(None, [])
    rebuilt_scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(rebuilt)

    def rebuild(force: bool = False) -> None:
        rebuilt._mark_rollup_stack_changed_helper(None)

    rebuilt.setup_with_retry = rebuild
    _run_shared_setup(rebuilt)
    assert rebuilt_scheduled == [("shared", None, None)]

    failed: Any = _setup_manager(None, [])
    failed_scheduled: list[tuple[str, str | None, str | None]] = _recording_schedule(failed)

    def failing_setup(force: bool = False) -> None:
        raise RuntimeError("narrow views failed")

    failed.setup_with_retry = failing_setup
    _run_shared_setup(failed)  # still only logged
    assert failed_scheduled == [("shared", None, None)]


def _refresh_manager() -> Any:
    """A manager with just the state the startup-refresh queue and worker use."""
    manager: Any = _manager()
    manager._initial_refresh_lock = threading.Lock()
    manager._initial_refresh_jobs = []
    manager._initial_refresh_worker = None
    manager.initial_refresh_idle = threading.Event()
    manager.initial_refresh_idle.set()
    manager._stop_refresh_rollup_event = threading.Event()
    return manager


def _no_inline_refresh(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a full refresh must not run on the calling thread")


def test_the_protocol_stack_queues_its_initial_refresh_instead_of_running_it() -> None:
    manager: Any = _setup_manager(None, [])
    manager.full_refresh_on_startup = True
    scheduled: list[tuple[str, str | None, str | None]] = []

    def record_schedule(kind: str, protocol_name: str | None, wide_table_name: str | None) -> None:
        scheduled.append((kind, protocol_name, wide_table_name))

    manager._schedule_initial_refresh = record_schedule
    manager._refresh_protocol_rollups_helper = _no_inline_refresh
    manager.add_wide_rollup("eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert scheduled == [("protocol", "eg4_18kpv", "device_metrics_wide__eg4_18kpv")]
    assert not manager.migration_in_progress.is_set()


def test_the_shared_narrow_stack_queues_its_initial_refresh_instead_of_running_it() -> None:
    manager: Any = _setup_manager(None, [])
    manager.full_refresh_on_startup = True
    scheduled: list[tuple[str, str | None, str | None]] = []
    manager.setup_with_retry = _noop

    def record_schedule(kind: str, protocol_name: str | None, wide_table_name: str | None) -> None:
        scheduled.append((kind, protocol_name, wide_table_name))

    manager._schedule_initial_refresh = record_schedule
    manager.refresh_rollups = _no_inline_refresh
    manager.setup_rollup(
        table_name="device_metrics_narrow", segment_by="device_info_id", compress_after_interval="7 days",
        chunk_time_interval="1 day", protocol_name=None, wide_table_name=None, use_shared_rollup_flow=True,
    )
    assert scheduled == [("shared", None, None)]


def test_a_slow_initial_refresh_does_not_block_the_caller_and_the_queue_runs_in_order() -> None:
    manager: Any = _refresh_manager()
    release: threading.Event = threading.Event()
    ran: list[str] = []

    def slow_shared_refresh(force_full: bool = False) -> None:
        assert force_full
        release.wait(timeout=5)
        ran.append("shared")

    def protocol_refresh(protocol_name: str, wide_table_name: str | None) -> None:
        ran.append(f"{protocol_name}:{wide_table_name}")

    manager.refresh_rollups = slow_shared_refresh
    manager._refresh_protocol_rollups_helper = protocol_refresh

    manager._schedule_initial_refresh("shared", None, None)  # returns although the refresh is still blocked
    manager._schedule_initial_refresh("protocol", "eg4_18kpv", "device_metrics_wide__eg4_18kpv")
    assert not manager.initial_refresh_idle.is_set()
    assert ran == []

    release.set()
    assert manager.initial_refresh_idle.wait(timeout=5)
    assert ran == ["shared", "eg4_18kpv:device_metrics_wide__eg4_18kpv"]
    assert manager._initial_refresh_worker is None


def test_a_failed_initial_refresh_does_not_stop_the_ones_queued_behind_it() -> None:
    manager: Any = _refresh_manager()
    ran: list[str] = []

    def failing_shared_refresh(force_full: bool = False) -> None:
        raise RuntimeError("monthly view blocked")

    manager.refresh_rollups = failing_shared_refresh

    def record_protocol_refresh(protocol_name: str, _wide_table_name: str | None) -> None:
        ran.append(protocol_name)

    manager._refresh_protocol_rollups_helper = record_protocol_refresh

    manager._schedule_initial_refresh("shared", None, None)
    manager._schedule_initial_refresh("protocol", "eg4_18kpv", None)
    assert manager.initial_refresh_idle.wait(timeout=5)
    assert ran == ["eg4_18kpv"]


def test_an_identical_waiting_initial_refresh_is_queued_only_once() -> None:
    manager: Any = _refresh_manager()
    release: threading.Event = threading.Event()
    calls: list[str] = []

    def blocked_shared_refresh(force_full: bool = False) -> None:
        release.wait(timeout=5)

    manager.refresh_rollups = blocked_shared_refresh

    def record_protocol_refresh(protocol_name: str, _wide_table_name: str | None) -> None:
        calls.append(protocol_name)

    manager._refresh_protocol_rollups_helper = record_protocol_refresh

    manager._schedule_initial_refresh("shared", None, None)  # becomes the running job, so no longer "waiting"
    manager._schedule_initial_refresh("protocol", "eg4_18kpv", None)
    manager._schedule_initial_refresh("protocol", "eg4_18kpv", None)  # identical to the one still waiting
    release.set()
    assert manager.initial_refresh_idle.wait(timeout=5)
    assert calls == ["eg4_18kpv"]


# ---------------------------------------------------------------------------
# rollup column aliases must stay unique inside Postgres' 63-byte identifier limit
# ---------------------------------------------------------------------------

def test_aliases_that_fit_are_left_exactly_as_they_were() -> None:
    aliases: dict[str, str] = tsdb.RollupManager._rollup_aliases_helper("stats_summary_", ["battery_soc", "pv1_power"])
    assert aliases == {"battery_soc": "stats_summary_battery_soc", "pv1_power": "stats_summary_pv1_power"}


def test_a_long_alias_that_is_unique_once_cut_to_63_bytes_keeps_the_name_postgres_would_have_given_it() -> None:
    """Existing views must keep their column names: the cut-down form is what Postgres already produced for them."""
    column: str = "inverter_battery_pack_maximum_charge_current_limit_amps"  # 55 chars: 'stats_summary_' + 55 = 69
    alias: str = tsdb.RollupManager._rollup_aliases_helper("stats_summary_", [column])[column]
    assert alias == f"stats_summary_{column}"[:63]


@pytest.mark.parametrize("prefix", ["min_", "max_", "stats_summary_"])
def test_columns_that_collide_after_the_cut_get_distinct_aliases_within_the_limit(prefix: str) -> None:
    common: str = "grid_import_total_energy_accumulated_since_commissioning_"  # 56 chars: the difference starts after the cut
    columns: list[str] = [f"{common}kwh_a", f"{common}kwh_b", "short_one"]
    aliases: dict[str, str] = tsdb.RollupManager._rollup_aliases_helper(prefix, columns)

    assert len(set(aliases.values())) == 3  # no two columns share an alias
    assert all(len(alias.encode()) <= 63 for alias in aliases.values())
    assert aliases["short_one"] == f"{prefix}short_one"  # untouched
    assert aliases == tsdb.RollupManager._rollup_aliases_helper(prefix, list(reversed(columns)))  # stable, whatever the order


def test_the_descriptors_use_one_alias_both_to_create_a_column_and_to_read_it_one_view_up() -> None:
    common: str = "grid_import_total_energy_accumulated_since_commissioning_"
    columns: list[str] = [f"{common}kwh_a", f"{common}kwh_b"]

    class _Session:
        def execute(self, *_args: object, **_kwargs: object) -> Any:
            return types.SimpleNamespace(scalars=lambda: iter(columns))

    descriptors: list[Any] = _manager()._resolve_rollup_metric_descriptors_helper(
        session=_Session(), source="device_metrics_wide__big", base_wide_table_name="device_metrics_wide__big"
    )
    assert len({d.summary_alias for d in descriptors}) == 2
    assert len({d.min_alias for d in descriptors}) == 2
    for d in descriptors:
        assert d.rolled_summary_sql == d.summary_alias
        assert d.rolled_min_sql == d.min_alias
        assert d.rolled_max_sql == d.max_alias
