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
from influxdb.exceptions import InfluxDBClientError  # type: ignore[import-untyped]

from classes.WebServer.routers import timeshift as ts_router
from classes.WebServer.services import timeshift_service as ts

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
            err: ts_router.RetentionPolicyConflictError | None = ts.check_influx_retention(None, "1", _influx_points(1, 2, 30, 40))
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
        bridge: SimpleNamespace = self._bridge_raising('{"error":"partial write: points beyond retention policy dropped=1000"}')
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
            resp = self._client(templates).post("/api/timeshift/import", json=self._payload())
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
            resp = self._client(templates).post("/api/timeshift/preview", json=self._payload())
        assert resp.status_code == 200
        assert "will not succeed" in resp.text and "autogen" in resp.text
