"""Unit tests for RollupManager helpers that decide what gets dropped, rebuilt or deleted (no database needed)."""

# These tests deliberately exercise the manager's private helpers.
# pyright: strict, reportPrivateUsage=false

from __future__ import annotations

import logging
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
    manager._refresh_protocol_rollups_helper = _noop
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
    manager.setup_rollup(
        table_name="device_metrics_narrow", segment_by="device_info_id", compress_after_interval="7 days",
        chunk_time_interval="1 day", protocol_name=None, wide_table_name=None, use_shared_rollup_flow=True,
    )  # does not raise


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
