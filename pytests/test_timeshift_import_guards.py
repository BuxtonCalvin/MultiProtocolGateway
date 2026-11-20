# Description: Unit tests for the Timeshift import guards -- InfluxDB retention-policy pre-check/trap, per-field type-conflict reporting, and the Preview/Result panels that show them.
# File: test_timeshift_import_guards.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the Timeshift import guards (services/timeshift_service.py, routers/timeshift.py, partials/timeshift_*.html)."""

# pyright: strict

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from httpx import Response
from influxdb.exceptions import InfluxDBClientError  # type: ignore[import-untyped]

from classes.WebServer.routers import timeshift as ts_router
from classes.WebServer.services import timeshift_service as ts


def _bridge_client(bridge: Any, *_args: Any, **_kwargs: Any) -> Any:
    """Stand-in for _open_v1_client/_open_v3_client: the bridge's own (mocked) client."""
    return bridge.client


@pytest.fixture(autouse=True)
def _reuse_bridge_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeshift opens its own short-lived InfluxDB clients (dynamic timeouts); these tests mock `bridge.client`, so hand that back."""
    monkeypatch.setattr(ts, "_open_v1_client", _bridge_client)
    monkeypatch.setattr(ts, "_open_v3_client", _bridge_client)


TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "classes" / "WebServer" / "templates"


def _naive_utc(delta_from_now: timedelta) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + delta_from_now).isoformat()


def _influx_points(*ages_days: float) -> list[ts.InfluxPointDict]:
    return [ts.InfluxPointDict("m", _naive_utc(-timedelta(days=d)), {"v": 1.0}, {}) for d in ages_days]


def _v1_bridge(policies: list[dict[str, Any]] | Exception) -> SimpleNamespace:
    client = MagicMock()
    if isinstance(policies, Exception):
        client.get_list_retention_policies.side_effect = policies
    else:
        client.get_list_retention_policies.return_value = policies
    return SimpleNamespace(client=client, database="openLuup")


RP_7D: list[dict[str, Any]] = [{"name": "autogen", "duration": "168h0m0s", "shardGroupDuration": "24h0m0s", "default": True}]


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

class TestParseInfluxDuration:
    @pytest.mark.parametrize(("text", "expected"), [
        ("168h0m0s", timedelta(days=7)),
        ("8760h0m0s", timedelta(days=365)),
        ("1h30m0s", timedelta(hours=1, minutes=30)),
        ("0s", None),      # InfluxDB's "INF"
        ("", None),
        (None, None),
    ])
    def test_parse(self, text: str | None, expected: timedelta | None) -> None:
        assert ts.parse_influx_duration(text) == expected


# ---------------------------------------------------------------------------
# Retention pre-check
# ---------------------------------------------------------------------------

class TestCheckInfluxRetention:
    def test_points_older_than_retention_are_reported(self) -> None:
        with patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(RP_7D)):
            err = ts.check_influx_retention(None, "1", _influx_points(1, 2, 30, 40))
        assert err is not None
        text: str = str(err)
        assert '"autogen"' in text and '"openLuup"' in text
        assert "2 of 4 points" in text
        assert "ALTER RETENTION POLICY" in text and "Target Start" in text
        assert "Nothing has been written" in text

    def test_range_inside_retention_is_fine(self) -> None:
        with patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(RP_7D)):
            assert ts.check_influx_retention(None, "1", _influx_points(0.5, 3, 6)) is None

    def test_infinite_retention_never_conflicts(self) -> None:
        rp: list[dict[str, Any]] = [{"name": "autogen", "duration": "0s", "default": True}]
        with patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(rp)):
            assert ts.check_influx_retention(None, "1", _influx_points(4000)) is None

    def test_only_the_default_policy_is_consulted(self) -> None:
        rp: list[dict[str, Any]] = [
            {"name": "short", "duration": "1h0m0s", "default": False},
            {"name": "autogen", "duration": "0s", "default": True},
        ]
        with patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(rp)):
            assert ts.check_influx_retention(None, "1", _influx_points(100)) is None

    def test_lookup_failure_is_best_effort(self) -> None:
        with patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(RuntimeError("boom"))):
            assert ts.check_influx_retention(None, "1", _influx_points(100)) is None

    def test_v3_and_no_points_are_skipped(self) -> None:
        assert ts.check_influx_retention(None, "3", _influx_points(100)) is None
        assert ts.check_influx_retention(None, "1", []) is None


# ---------------------------------------------------------------------------
# Write-time trap (backstop)
# ---------------------------------------------------------------------------

class TestWriteTrapsRetention:
    def _bridge_raising(self, content: str) -> SimpleNamespace:
        client = MagicMock()
        client.write_points.side_effect = InfluxDBClientError(content, 400)
        return SimpleNamespace(client=client, database="openLuup")

    def test_v1_beyond_retention_becomes_friendly_error(self) -> None:
        bridge = self._bridge_raising('{"error":"partial write: points beyond retention policy dropped=1000"}')
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge):
            with pytest.raises(ts.RetentionPolicyConflictError) as exc:
                ts.write_points_v1(None, _influx_points(1), allow_float_coercion=True)
        assert exc.value.may_have_partial_write is True
        assert "retention policy" in str(exc.value) and "Target Start" in str(exc.value)

    def test_v1_missing_retention_policy_is_not_mislabelled(self) -> None:
        # "retention policy not found" is a different failure (seen in the live log) -- must surface as the raw client error.
        bridge: SimpleNamespace = self._bridge_raising('{"error":"retention policy not found: autogen"}')
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge):
            with pytest.raises(InfluxDBClientError):
                ts.write_points_v1(None, _influx_points(1), allow_float_coercion=True)

    def test_v3_retention_error_is_trapped(self) -> None:
        client = MagicMock()
        client.write.side_effect = Exception("points are outside the retention period")
        bridge = SimpleNamespace(client=client, database="db")
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            with pytest.raises(ts.RetentionPolicyConflictError):
                ts.write_points_v3(None, _influx_points(1))


# ---------------------------------------------------------------------------
# Type-conflict reporting
# ---------------------------------------------------------------------------

class TestTypeMismatchSummary:
    def _build(self) -> tuple[list[ts.TimescalePointDict], ts.TimeshiftImportResult]:
        df = pd.DataFrame({
            "Time": ["2025-06-01 00:00:00", "2025-06-01 00:05:00", "2025-06-01 00:10:00"],
            "kwh": [1.5, 2.5, 3.0],      # INTEGER column: 2 fractional values rejected, the whole one is fine
            "soc": [10, 11, 12],
        })
        return ts.build_points_timescale(
            df, table_kind="wide", mapping={"kwh": "kwh", "soc": "soc"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0),
            wide_field_types={"kwh": "INTEGER", "soc": "INTEGER"}, existing_wide_columns={"kwh", "soc"},
        )

    def test_summary_groups_by_field_with_example_and_reason(self) -> None:
        _points, result = self._build()
        assert list(result.type_mismatch_summary) == ["kwh"]
        stat: ts.TypeMismatchStat = result.type_mismatch_summary["kwh"]
        assert stat.count == 2 and stat.declared_type == "INTEGER"
        assert stat.example_value == "1.5" and "whole number" in stat.reason
        assert len(result.type_mismatches) == 2   # legacy flat list still populated

    def test_summary_is_logged_per_field(self, caplog: pytest.LogCaptureFixture) -> None:
        _points, result = self._build()
        with caplog.at_level(logging.WARNING, logger=ts.__name__):
            ts.log_type_mismatch_summary(result, "TimescaleDB")
        assert any("skipped 2 value(s) for 'kwh'" in r.getMessage() and "INTEGER" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# One-to-one, deterministic field matching + duplicate-target guard
# ---------------------------------------------------------------------------

class TestUniqueMatching:
    def _suggest(self, columns: list[str], fields: list[str]) -> dict[str, str]:
        rows: list[ts_router.FieldMappingRow] = ts.suggest_field_mapping(columns, fields, set(), "eg4")
        return {r.source_column: ("" if r.default_ignored else r.suggested_field) for r in rows}

    def test_no_destination_field_is_suggested_twice(self) -> None:
        # Your table has epv1_* only: ePv2*/ePv3* used to be suggested the same field as ePv1* and overwrite it.
        mapping: dict[str, str] = self._suggest(["ePv1All", "ePv2All", "ePv3All", "ePv1Day", "ePv2Day"], ["epv1_all", "epv1_day"])
        assert mapping["ePv1All"] == "epv1_all" and mapping["ePv1Day"] == "epv1_day"
        assert mapping["ePv2All"] == mapping["ePv3All"] == mapping["ePv2Day"] == ""
        assert not ts.find_duplicate_targets({k: v for k, v in mapping.items() if v})

    def test_runner_up_falls_back_to_a_free_matching_field(self) -> None:
        mapping: dict[str, str] = self._suggest(["ePv1All", "ePv2All"], ["epv1_all", "epv2_all"])
        assert mapping == {"ePv1All": "epv1_all", "ePv2All": "epv2_all"}

    def test_result_does_not_depend_on_set_iteration_order(self) -> None:
        columns: list[str] = ["tradiator1", "tradiator2", "tradiator3"]
        fields: list[str] = ["tradiator1", "tradiator2"]
        expected: dict[str, str] = {"tradiator1": "tradiator1", "tradiator2": "tradiator2", "tradiator3": ""}
        for ordering in (fields, fields[::-1]):
            assert self._suggest(columns, ordering) == expected
        assert ts.rank_mappings("tradiator3", {"tradiator2", "tradiator1"}) == ts.rank_mappings("tradiator3", ["tradiator1", "tradiator2"])

    def test_find_duplicate_targets_ignores_blank_targets(self) -> None:
        dups: dict[str, list[str]] = ts.find_duplicate_targets({"a": "x", "b": "x", "c": "", "d": "", "e": "y"})
        assert dups == {"x": ["a", "b"]}
        assert '"x" <- a, b' in ts.duplicate_targets_message(dups)


class TestDuplicateTargetEndpoints:
    def _client(self, templates: Jinja2Templates) -> TestClient:
        return TestInfluxImportEndpoint()._client(templates)  # pyright: ignore[reportPrivateUsage]

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = TestInfluxImportEndpoint()._payload()  # pyright: ignore[reportPrivateUsage]
        payload["mapping_json"] = json.dumps({"a": "v", "b": "v"})   # two columns -> one field
        return payload

    def _upload(self) -> SimpleNamespace:
        df = pd.DataFrame({"Time": ["2020-01-01 00:00:00"], "a": [1.0], "b": [2.0]})
        return SimpleNamespace(dataframe=df, columns=["Time", "a", "b"])

    def _patches(self) -> Any:
        from contextlib import ExitStack
        stack: ExitStack = ExitStack()
        stack.enter_context(patch.object(ts_router, "_require_bridge", return_value=MagicMock()))
        stack.enter_context(patch.object(ts_router, "get_upload", return_value=self._upload()))
        stack.enter_context(patch.object(ts_router, "load_influx_field_types", return_value={}))
        stack.enter_context(patch.object(ts, "get_influxdb1_bridge", return_value=None))
        return stack

    def test_preview_flags_duplicate_targets(self, templates: Jinja2Templates) -> None:
        with self._patches():
            resp: Response = self._client(templates).post("/api/timeshift/preview", json=self._payload())
        assert resp.status_code == 200
        assert "will not succeed" in resp.text and "same destination field" in resp.text

    def test_import_refuses_duplicate_targets_before_writing(self, templates: Jinja2Templates) -> None:
        with self._patches(), patch.object(ts_router, "write_points_v1") as write:
            resp: Response = self._client(templates).post("/api/timeshift/import", json=self._payload())
        assert resp.status_code == 400 and "same destination field" in resp.json()["detail"]
        write.assert_not_called()


# ---------------------------------------------------------------------------
# Round-to-integer option (TimescaleDB wide tables)
# ---------------------------------------------------------------------------

class TestRoundToInteger:
    def _build(self, *, round_to_integer: bool) -> tuple[list[ts.TimescalePointDict], ts.TimeshiftImportResult]:
        df = pd.DataFrame({
            "Time": ["2025-06-01 00:00:00", "2025-06-01 00:05:00", "2025-06-01 00:10:00"],
            "etotal": [175.4, 175.5, 176.0],   # BIGINT total
            "pf": ["1", "[0]", "1"],           # INTEGER column with a text value
            "vbat": [52.5, 52.4, 52.6],        # DOUBLE PRECISION: never rounded
        })
        return ts.build_points_timescale(
            df, table_kind="wide", mapping={"etotal": "etotal", "pf": "pf", "vbat": "vbat"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0),
            wide_field_types={"etotal": "bigint", "pf": "integer", "vbat": "double precision"},
            existing_wide_columns={"etotal", "pf", "vbat"}, round_to_integer=round_to_integer,
        )

    def test_off_by_default_skips_decimals_and_hints_at_the_checkbox(self) -> None:
        points, result = self._build(round_to_integer=False)
        assert result.type_mismatch_summary["etotal"].count == 2
        assert result.suggest_rounding is True and not result.rounded_counts
        assert [p.fields.get("etotal") for p in points] == [None, None, 176.0]

    def test_on_rounds_half_up_only_integer_columns(self) -> None:
        points, result = self._build(round_to_integer=True)
        assert [p.fields["etotal"] for p in points] == [175.0, 176.0, 176.0]   # 175.4 -> 175, 175.5 -> 176 (not banker's)
        assert [p.fields["vbat"] for p in points] == [52.5, 52.4, 52.6]        # float column untouched
        assert result.rounded_counts == {"etotal": 2}
        assert any("Rounded 2 fractional value(s)" in w for w in result.warnings)

    def test_text_is_still_reported_when_rounding(self) -> None:
        _points, result = self._build(round_to_integer=True)
        assert list(result.type_mismatch_summary) == ["pf"]
        assert result.type_mismatch_summary["pf"].example_value == "'[0]'"
        assert result.suggest_rounding is False   # rounding wouldn't help a text value

    @pytest.mark.parametrize(("value", "expected"), [(175.5, 176), (-2.5, -3), (2.4, 2), ("12.6", 13), (float("nan"), None), ("[0]", None), (True, None)])
    def test_round_half_up(self, value: Any, expected: int | None) -> None:
        assert ts.round_half_up(value) == expected


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(TEMPLATES_DIR))


def _render(templates: Jinja2Templates, name: str, **ctx: Any) -> str:
    return templates.get_template(name).render(**ctx)


class TestPanels:
    def _result_with_conflicts(self) -> ts.TimeshiftImportResult:
        _p, result = TestTypeMismatchSummary()._build()  # pyright: ignore[reportPrivateUsage]
        result.points_written = 3
        return result

    def test_result_panel_names_the_skipped_field_and_turns_amber(self, templates: Jinja2Templates) -> None:
        html: str = _render(templates, "partials/timeshift_result.html", result=self._result_with_conflicts(), destination="TimescaleDB", action="import", table_kind="wide")
        assert "kwh" in html and "INTEGER" in html and "1.5" in html
        assert "some data was skipped" in html and "border-warning" in html

    def test_clean_result_panel_stays_green(self, templates: Jinja2Templates) -> None:
        html: str = _render(templates, "partials/timeshift_result.html", result=ts.TimeshiftImportResult(points_written=5), destination="InfluxDB", action="import", table_kind=None)
        assert "border-success" in html and "skipped" not in html

    def test_preview_shows_retention_error_and_wide_notice(self, templates: Jinja2Templates) -> None:
        result = ts.TimeshiftImportResult()
        result.blocking_errors.append('InfluxDB retention policy "autogen" only keeps 7 day(s)')
        html: str = _render(templates, "partials/timeshift_preview.html", points_to_write=4, sample=[], result=result, table_kind=None)
        assert "will not succeed" in html and "autogen" in html
        assert "mirrored into the shared narrow table" not in html
        wide: str = _render(templates, "partials/timeshift_preview.html", points_to_write=4, sample=[], result=ts.TimeshiftImportResult(), table_kind="wide")
        assert "mirrored into the shared narrow table" in wide


# ---------------------------------------------------------------------------
# Endpoint: refuse before writing, with the explanation as the response detail
# ---------------------------------------------------------------------------

class TestInfluxImportEndpoint:
    def _client(self, templates: Jinja2Templates) -> TestClient:
        app = FastAPI()
        app.include_router(ts_router.router)
        app.state.templates = templates
        app.state.gateway = None
        return TestClient(app)

    def _payload(self) -> dict[str, Any]:
        return {
            "upload_id": "u1", "version": "1", "measurement": "device_data", "time_column": "Time",
            "mapping_json": json.dumps({"v": "v"}), "tags_json": json.dumps({"device_identifier": "inv1"}), "local_timezone": "UTC", "source_is_local": False,
            "source_start": "2020-01-01T00:00:00", "target_start": "2020-01-01T00:00:00",
        }

    def _upload(self) -> SimpleNamespace:
        df = pd.DataFrame({"Time": ["2020-01-01 00:00:00", "2020-01-01 00:05:00"], "v": [1.0, 2.0]})
        return SimpleNamespace(dataframe=df, columns=["Time", "v"])

    def test_import_outside_retention_is_refused_before_any_write(self, templates: Jinja2Templates) -> None:
        with (
            patch.object(ts_router, "_require_bridge", return_value=MagicMock()),
            patch.object(ts_router, "get_upload", return_value=self._upload()),
            patch.object(ts_router, "load_influx_field_types", return_value={}),
            patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(RP_7D)),
            patch.object(ts_router, "write_points_v1") as write,
        ):
            resp: Response = self._client(templates).post("/api/timeshift/import", json=self._payload())
        assert resp.status_code == 409
        assert "retention policy" in resp.json()["detail"] and "Target Start" in resp.json()["detail"]
        write.assert_not_called()

    def test_preview_flags_the_same_problem(self, templates: Jinja2Templates) -> None:
        with (
            patch.object(ts_router, "_require_bridge", return_value=MagicMock()),
            patch.object(ts_router, "get_upload", return_value=self._upload()),
            patch.object(ts_router, "load_influx_field_types", return_value={}),
            patch.object(ts, "get_influxdb1_bridge", return_value=_v1_bridge(RP_7D)),
        ):
            resp: Response = self._client(templates).post("/api/timeshift/preview", json=self._payload())
        assert resp.status_code == 200
        assert "will not succeed" in resp.text and "autogen" in resp.text
