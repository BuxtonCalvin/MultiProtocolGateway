"""Tests for the Metrics Edit "also apply to the shared narrow table" option and the simplified range-picker format.

The gateway writes every sample to BOTH the wide and the narrow table, so a wide-table edit alone leaves the narrow copy
(and the narrow rollups) with the old value. A wide edit can therefore be staged with also_narrow, and committing it runs
the wide edit and then the same edit against device_metrics_narrow. Nothing here needs a database: the real code is driven
with small fakes, and the templates are rendered from the real files.
"""

# These tests deliberately exercise private helpers and BridgeAdminManager internals.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

import classes.transports.timescaledb as tsdb
import classes.WebServer.services.bridge_service as bs
from classes.WebServer.routers import timescale as ts_router

TEMPLATES: Path = Path(__file__).resolve().parents[1] / "classes" / "WebServer" / "templates"
_env: Environment = Environment(
    loader=ChoiceLoader([
        DictLoader({"base.html": "{% block title %}{% endblock %}|{% block head %}{% endblock %}|{% block content %}{% endblock %}"}),
        FileSystemLoader(str(TEMPLATES)),
    ]),
    autoescape=True,
)


# ── range picker format ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _la_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts_router, "get_machine_timezone", lambda: "America/Los_Angeles")


@pytest.mark.parametrize("raw", [
    "2026-10-17  20:05:55",   # the picker's own format: two spaces
    "2026-10-17 20:05:55",    # one space, typed by hand
    "2026-10-17   20:05:55",  # any run of whitespace
    "2026-10-17T20:05:55",    # ISO, as an API client or the old datetime-local input would send
])
def test_parse_accepts_picker_and_iso_formats(raw: str) -> None:
    assert ts_router._parse_local_datetime(raw) == datetime(2026, 10, 17, 20, 5, 55, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_parse_minutes_only_still_works() -> None:
    assert ts_router._parse_local_datetime("2026-10-17T20:05") == datetime(2026, 10, 17, 20, 5, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_parse_rejects_garbage() -> None:
    with pytest.raises(HTTPException) as exc:
        ts_router._parse_local_datetime("not a time")
    assert exc.value.status_code == 400


def test_end_bound_covers_the_whole_second() -> None:
    """m_time has microseconds; an END of ...:49 must include a row stamped ...:49.810365."""
    row: datetime = datetime(2026, 10, 17, 20, 5, 49, 810365, tzinfo=ZoneInfo("America/Los_Angeles"))
    start: datetime = ts_router._parse_local_datetime("2026-10-17  20:05:49")
    end: datetime = ts_router._parse_local_datetime("2026-10-17  20:05:49", end_of_second=True)
    assert start <= row <= end
    assert end.microsecond == 999999
    assert ts_router._parse_local_datetime("2026-10-17  20:05:49").microsecond == 0   # start bound untouched
    assert row > ts_router._parse_local_datetime("2026-10-17  20:05:49")                # i.e. without the fix it was missed


# ── narrow_companion_value ───────────────────────────────────────────────────────────────────────────────────────────

def _mgr(columns: dict[str, tuple[int, str]]) -> Any:
    mgr: Any = tsdb.BridgeAdminManager.__new__(tsdb.BridgeAdminManager)

    def resolve_table(_kind: str, _protocol_name: str | None) -> tuple[str, int]:
        return "device_metrics_wide__p", 7

    def load_columns(_protocol_id: int) -> dict[str, tuple[int, str]]:
        return columns

    mgr._resolve_metric_edit_table = resolve_table
    mgr._load_existing_wide_columns = load_columns
    return mgr


COLUMNS: dict[str, tuple[int, str]] = {
    "pload": (1, "INTEGER"), "pgrid": (2, "INTEGER"), "volts": (3, "DOUBLE PRECISION"),
    "relay": (4, "BOOLEAN"), "mode": (5, "TEXT"),
}


@pytest.mark.parametrize(("fields", "value", "expected"), [
    (["pload"], "700", 700.0),          # INTEGER -> float, the way the narrow writer stores it
    (["volts"], "230.5", 230.5),
    (["relay"], "true", 1.0),           # BOOLEAN -> 1.0 / 0.0, as _flush_batch_narrow does
    (["relay"], "off", 0.0),
    (["mode"], "Standby", "Standby"),   # TEXT stays text
    (["pload", "pgrid"], "5", 5.0),     # same representation for both
])
def test_narrow_companion_value(fields: list[str], value: str, expected: float | str) -> None:
    assert _mgr(COLUMNS).narrow_companion_value("p", fields, value) == expected


def test_narrow_companion_value_rejects_bad_input() -> None:
    mgr: Any = _mgr(COLUMNS)
    with pytest.raises(ValueError):
        mgr.narrow_companion_value("p", ["pload"], "abc")           # not a number for an INTEGER column
    with pytest.raises(ValueError):
        mgr.narrow_companion_value("p", ["pload"], "1.5")           # not a whole number
    with pytest.raises(ValueError):
        mgr.narrow_companion_value("p", ["nope"], "1")              # column doesn't exist
    with pytest.raises(ValueError):
        mgr.narrow_companion_value("p", ["pload", "mode"], "5")     # numeric 5.0 vs text "5": ambiguous
    with pytest.raises(ValueError):
        mgr.narrow_companion_value("p", ["pload"], None)


# ── staging + commit ─────────────────────────────────────────────────────────────────────────────────────────────────

class _FakeMgr:
    """Records edit_metric_values() calls; narrow_companion_value() delegates to the real implementation."""

    def __init__(self, fail_on_narrow: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.validated: list[tuple[Any, ...]] = []
        self.fail_on_narrow: bool = fail_on_narrow
        self._real: Any = _mgr(COLUMNS)

    def validate_metric_edit_value(self, *args: Any, **kwargs: Any) -> None:
        self.validated.append((args, kwargs))

    def narrow_companion_value(self, *a: Any) -> float | str:
        return self._real.narrow_companion_value(*a)

    def edit_metric_values(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if kw["table_kind"] == "narrow" and self.fail_on_narrow:
            raise RuntimeError("narrow failed")
        n: int = 1 if kw["table_kind"] == "wide" else 13
        return types.SimpleNamespace(
            table_kind=kw["table_kind"], protocol_name=kw["protocol_name"],
            table_name="device_metrics_wide__p" if kw["table_kind"] == "wide" else "device_metrics_narrow",
            device_info_id=kw["device_info_id"], field_names=kw["field_names"], action=kw["action"],
            rows_affected=n, rollups_refreshed=True,
        )


T0: datetime = datetime(2026, 10, 17, 20, 5, 49, tzinfo=ZoneInfo("America/Los_Angeles"))
T1: datetime = T0.replace(microsecond=999999)


def _stage(state: Any, **over: Any) -> str:
    args: dict[str, Any] = {
        "table_kind": "wide", "protocol_name": "p", "table_name": "device_metrics_wide__p", "device_info_id": 3,
        "device_label": "dev", "field_names": ["pload"], "start_time": T0, "end_time": T1, "action": "delete",
        "new_value": None, "also_narrow": True,
    }
    args.update(over)
    return bs.stage_metric_edit(None, state, **args)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Any:
    st: Any = types.SimpleNamespace()
    fake: _FakeMgr = _FakeMgr()
    st.fake = fake

    def field_manager(_gateway: object) -> _FakeMgr:
        return fake

    monkeypatch.setattr(bs, "_field_manager", field_manager)
    return st


def test_delete_with_companion_runs_wide_then_narrow(state: Any) -> None:
    _stage(state)
    results = bs.commit_staged_metric_edits(None, state)
    kinds: list[str] = [c["table_kind"] for c in state.fake.calls]
    assert kinds == ["wide", "narrow"]
    narrow: dict[str, Any] = state.fake.calls[1]
    assert (narrow["device_info_id"], narrow["field_names"], narrow["start_time"], narrow["end_time"]) == (3, ["pload"], T0, T1)
    assert narrow["action"] == "delete" and narrow["protocol_name"] is None and narrow["new_value"] is None
    assert results[0]["also_narrow"] is True
    assert (results[0]["rows_affected"], results[0]["narrow_rows_affected"]) == (1, 13)
    assert bs.has_staged_metric_edits(state) is False   # fully applied -> cleared from staging


def test_set_value_companion_uses_wide_type_not_narrow_contents(state: Any) -> None:
    _stage(state, action="set_value", new_value="700")
    bs.commit_staged_metric_edits(None, state)
    narrow: dict[str, Any] = state.fake.calls[1]
    assert narrow["new_value"] == 700.0 and narrow["narrow_kind"] == "numeric"


def test_boolean_set_value_companion(state: Any) -> None:
    _stage(state, action="set_value", field_names=["relay"], new_value="true")
    bs.commit_staged_metric_edits(None, state)
    assert state.fake.calls[1]["new_value"] == 1.0


def test_without_companion_only_the_wide_table_is_touched(state: Any) -> None:
    _stage(state, also_narrow=False)
    results = bs.commit_staged_metric_edits(None, state)
    assert [c["table_kind"] for c in state.fake.calls] == ["wide"]
    assert results[0]["also_narrow"] is False and results[0]["narrow_rows_affected"] == 0


def test_also_narrow_is_ignored_for_the_narrow_table_itself(state: Any) -> None:
    _stage(state, table_kind="narrow", protocol_name=None, table_name="device_metrics_narrow")
    assert [e["also_narrow"] for e in bs.get_staged_metric_edits(state)] == [False]
    bs.commit_staged_metric_edits(None, state)
    assert [c["table_kind"] for c in state.fake.calls] == ["narrow"]   # once, not twice


def test_staging_validates_the_companion(state: Any) -> None:
    _stage(state, action="set_value", new_value="5")
    _args, kwargs = state.fake.validated[0]
    assert kwargs["also_narrow"] is True


def test_narrow_failure_keeps_the_entry_staged_for_retry(state: Any) -> None:
    state.fake.fail_on_narrow = True
    _stage(state)
    with pytest.raises(RuntimeError):
        bs.commit_staged_metric_edits(None, state)
    assert len(bs.get_staged_metric_edits(state)) == 1     # retry re-runs both steps; delete is idempotent


# ── templates ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_preview_shows_picker_style_timestamps_and_narrow_count() -> None:
    preview: dict[str, Any] = {
        "row_count": 13, "narrow_row_count": 13,
        "sample": [{"m_time": "2026-10-17T20:05:49.810365-07:00", "m_time_display": "2026-10-17  20:05:49",
                    "field_name": "pload", "value": None}],
    }
    html: str = _env.get_template("partials/timescale_metrics_edit_preview.html").render(preview=preview)
    assert "2026-10-17  20:05:49" in html and ".810365" not in html and "T20:05" not in html
    assert "matching row(s) in the shared narrow table will also be changed" in html
    preview["narrow_row_count"] = None
    assert "shared narrow table" not in _env.get_template("partials/timescale_metrics_edit_preview.html").render(preview=preview)


def test_staged_list_shows_seconds_and_narrow_marker() -> None:
    edit: dict[str, Any] = {
        "edit_id": "x", "table_kind": "wide", "protocol_name": "p", "table_name": "device_metrics_wide__p",
        "device_info_id": 3, "device_label": "dev", "field_names": ["pload"], "start_time": T0, "end_time": T1,
        "action": "delete", "new_value": None, "also_narrow": True,
    }
    html: str = _env.get_template("partials/timescale_metrics_edit_staged.html").render(staged_edits=[edit])
    assert "2026-10-17  20:05:49 → 2026-10-17  20:05:49" in html
    assert "+ device_metrics_narrow" in html
    edit["also_narrow"] = False
    assert "+ device_metrics_narrow" not in _env.get_template("partials/timescale_metrics_edit_staged.html").render(staged_edits=[edit])


def test_page_has_text_pickers_and_the_narrow_checkbox() -> None:
    html: str = _env.get_template("pages/timescale_metrics_edit.html").render(tables=[], staged_edits=[], linked_protocols=None)
    assert 'type="datetime-local"' not in html
    assert 'id="metrics-edit-start-time"' in html and 'id="metrics-edit-also-narrow"' in html
    assert "/static/js/flatpickr.min.js" in html and "/static/css/flatpickr.min.css" in html


# ── click a preview row to fill Start / End ──────────────────────────────────────────────────────────────────────────

def test_preview_rows_are_clickable_and_carry_the_second() -> None:
    preview: dict[str, Any] = {
        "row_count": 2, "narrow_row_count": None,
        "sample": [
            {"m_time": "x", "m_time_display": "2026-10-17  20:05:49", "field_name": "pload", "value": None},
            {"m_time": "y", "m_time_display": "2026-10-17  20:05:34", "field_name": "pload", "value": 670},
        ],
    }
    html: str = _env.get_template("partials/timescale_metrics_edit_preview.html").render(preview=preview)
    assert 'data-ts="2026-10-17  20:05:49"' in html and 'data-ts="2026-10-17  20:05:34"' in html   # whole seconds only
    assert html.count('onclick="setMetricsEditRangeFromRow(this)"') == 2
    assert "Click a row to set Start and End" in html


def test_page_defines_the_row_click_handler() -> None:
    html: str = _env.get_template("pages/timescale_metrics_edit.html").render(tables=[], staged_edits=[], linked_protocols=None)
    assert "function setMetricsEditRangeFromRow(row)" in html


def test_preview_timestamps_are_shown_in_the_machine_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pickers are read in the machine timezone, so a clicked row must be displayed (and copied) in that zone, not
    whatever zone the database session returned."""
    import classes.transports.timescaledb as tsdb_mod
    from classes.transports.timescaledb import MetricEditPreview, MetricValueSample

    monkeypatch.setattr(tsdb_mod, "get_machine_timezone", lambda: "America/Los_Angeles")
    utc_row: datetime = datetime(2026, 10, 18, 3, 5, 49, 810365, tzinfo=ZoneInfo("UTC"))   # = 20:05:49 on the 17th, LA time

    def preview_metric_edit(*_args: Any, **_kwargs: Any) -> MetricEditPreview:
        return MetricEditPreview(
            row_count=1, sample=[MetricValueSample(m_time=utc_row, field_name="pload", value=None)], narrow_row_count=None
        )

    fake: Any = types.SimpleNamespace(preview_metric_edit=preview_metric_edit)

    def field_manager(_gateway: object) -> Any:
        return fake

    monkeypatch.setattr(bs, "_field_manager", field_manager)
    out = bs.preview_metric_edit(None, "wide", "p", 3, ["pload"], T0, T1)
    assert out["sample"][0]["m_time_display"] == "2026-10-17  20:05:49"
