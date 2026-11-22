# Description: tests for the Timeshift progress stream (thermometer events), the batched writers that feed it, and the streaming import endpoint.
# File: test_timeshift_progress.py
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

# pyright: strict

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from httpx import Response
from influxdb.exceptions import InfluxDBClientError  # type: ignore[import-untyped]

from classes.WebServer.routers import timeshift as ts_router
from classes.WebServer.services import timeshift_service as ts
from classes.WebServer.services.timeshift_service import (
    JobResult,
    ProgressReporter,
    stream_job,
)

TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "classes" / "WebServer" / "templates"


def _lines(stream: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream]


class _Recorder(list[tuple[str, int, int]]):
    """A progress callback that records every (label, done, total) it is given."""

    def __call__(self, label: str, done: int, total: int) -> None:
        self.append((label, done, total))


class TestStreamJob:
    def test_progress_events_then_one_done_event(self) -> None:
        def job(reporter: ProgressReporter) -> JobResult:
            reporter.report("half", 5, 10)
            reporter.report("full", 10, 10)
            return {"answer": 42}

        events: list[dict[str, Any]] = _lines(stream_job(job, name="t", cancel_on_disconnect=False))
        assert events[-1] == {"type": "done", "answer": 42}
        assert events[0] == {"type": "progress", "label": "half", "done": 5, "total": 10}
        assert events[-2]["done"] == 10   # the completing event is never throttled away

    def test_http_exception_detail_becomes_the_error_text(self) -> None:
        def job(_r: ProgressReporter) -> JobResult:
            raise HTTPException(status_code=409, detail="outside retention")

        assert _lines(stream_job(job, name="t", cancel_on_disconnect=False)) == [{"type": "error", "detail": "outside retention"}]

    def test_any_other_exception_gets_the_prefix(self) -> None:
        def job(_r: ProgressReporter) -> JobResult:
            raise RuntimeError("disk full")

        assert _lines(stream_job(job, name="t", cancel_on_disconnect=False, error_prefix="Export failed: "))[-1] == {"type": "error", "detail": "Export failed: disk full"}

    def test_closing_the_stream_cancels_a_cancellable_job_only(self) -> None:
        for cancellable in (True, False):
            started: threading.Event = threading.Event()
            release: threading.Event = threading.Event()
            outcome: list[bool] = []

            def job(reporter: ProgressReporter, started: threading.Event = started, release: threading.Event = release, outcome: list[bool] = outcome) -> JobResult:
                reporter.report("working", 1, 100)
                started.set()
                release.wait(timeout=5)
                try:
                    reporter.raise_if_cancelled()
                    outcome.append(False)
                except Exception:
                    outcome.append(True)
                    raise
                return {}

            stream: Generator[str, None, None] = stream_job(job, name="t", cancel_on_disconnect=cancellable)
            next(stream)           # first progress line
            started.wait(timeout=5)
            stream.close()         # the browser went away
            release.set()
            for _ in range(100):
                if outcome:
                    break
                threading.Event().wait(0.02)
            assert outcome == [cancellable]


def _v1_points(count: int) -> list[ts.InfluxPointDict]:
    return [ts.InfluxPointDict("m", f"2025-06-01T00:{n // 60 % 60:02d}:{n % 60:02d}", {"v": 1}, {}) for n in range(count)]


class TestBatchedWriters:
    def test_v1_writes_in_batches_and_reports_after_each(self) -> None:
        client = MagicMock()
        progress: _Recorder = _Recorder()
        bridge = SimpleNamespace(client=client, database="db")
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge), patch.object(ts, "_open_v1_client", return_value=client):
            written: int = ts.write_points_v1(None, _v1_points(2500), True, progress)  # noqa: FBT003
        assert written == 2500
        assert [len(c.args[0]) for c in client.write_points.call_args_list] == [1000, 1000, 500]
        assert [done for _l, done, _t in progress] == [0, 1000, 2000, 2500] and {t for _l, _d, t in progress} == {2500}

    def test_v1_type_conflict_retries_only_the_failing_batch_and_keeps_going(self) -> None:
        client = MagicMock()
        calls: list[int] = []

        def write(batch: list[dict[str, Any]], batch_size: int = 0) -> None:
            calls.append(len(batch))
            if len(calls) == 2:   # the second batch hits a conflict once
                err: InfluxDBClientError = InfluxDBClientError('{"error":"field type conflict: input field \\"v\\" on measurement \\"m\\" is type integer, already exists as type float"}', 400)
                raise err
        client.write_points.side_effect = write
        bridge = SimpleNamespace(client=client, database="db")
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge), patch.object(ts, "_open_v1_client", return_value=client):
            assert ts.write_points_v1(None, _v1_points(2500), True) == 2500  # noqa: FBT003
        assert calls == [1000, 1000, 1000, 500]   # batch 2 sent twice, nothing before it resent
        retried: list[dict[str, Any]] = client.write_points.call_args_list[2].args[0]
        assert all(isinstance(p["fields"]["v"], float) for p in retried)

    def test_v3_writes_in_batches_and_flags_a_partial_write(self) -> None:
        client = MagicMock()
        progress: _Recorder = _Recorder()
        bridge = SimpleNamespace(client=client, database="db")
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge), patch.object(ts, "_open_v3_client", return_value=client):
            assert ts.write_points_v3(None, _v1_points(25_000), progress) == 25_000
            assert [len(c.kwargs["record"]) for c in client.write.call_args_list] == [10_000, 10_000, 5_000]
            assert progress[-1][1:] == (25_000, 25_000)

            client.write.side_effect = [None, Exception("points are outside the retention period")]
            with pytest.raises(ts.RetentionPolicyConflictError) as exc:
                ts.write_points_v3(None, _v1_points(25_000))
        assert exc.value.may_have_partial_write is True

    def test_timescale_reports_each_batch_inside_one_transaction(self) -> None:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.begin.return_value.__enter__ = MagicMock(return_value=None)
        session.begin.return_value.__exit__ = MagicMock(return_value=False)
        bridge = SimpleNamespace(SessionFactory=MagicMock(return_value=session), tsdb_connected=True)
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso=f"2025-06-01T00:00:{n % 60:02d}", fields={"soc": float(n), "v": 1.0}) for n in range(1200)]
        progress: _Recorder = _Recorder()
        with patch.object(ts, "get_timescale_bridge", return_value=bridge), patch.object(ts, "_copy_upsert"):
            ts.write_points_timescale(None, "wide", "device_metrics_wide__x", 7, points, progress)
        assert progress[0][1:] == (0, 0)                                  # "preparing" is indeterminate
        assert [done for _l, done, _t in progress[1:]] == [1000, 1200]
        assert {total for _l, _d, total in progress[1:]} == {1200}
        assert progress[-1][0] == "Writing points · 1,200 of 1,200"
        assert session.begin.call_count == 1


class TestStreamingImportEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        app.include_router(ts_router.router)
        app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        app.state.gateway = None
        return TestClient(app)

    def _payload(self) -> dict[str, Any]:
        return {
            "upload_id": "u1", "version": "1", "measurement": "device_data", "time_column": "Time",
            "mapping_json": json.dumps({"v": "v"}), "tags_json": json.dumps({"device_identifier": "inv1"}), "local_timezone": "UTC",
            "source_is_local": False, "source_start": "2020-01-01T00:00:00", "target_start": "2020-01-01T00:00:00",
        }

    def test_import_streams_progress_then_the_result_panel(self, client: TestClient) -> None:
        df = pd.DataFrame({"Time": ["2020-01-01 00:00:00", "2020-01-01 00:05:00"], "v": [1.0, 2.0]})

        def fake_write(_gw: Any, points: list[ts.InfluxPointDict], _coerce: bool, on_progress: Any) -> int:
            on_progress("Writing points · 2 of 2", 2, 2)
            return len(points)

        with (
            patch.object(ts_router, "_require_bridge", return_value=MagicMock()),
            patch.object(ts_router, "get_upload", return_value=SimpleNamespace(dataframe=df, columns=["Time", "v"])),
            patch.object(ts_router, "load_influx_field_types", return_value={}),
            patch.object(ts_router, "check_influx_retention", return_value=None),
            patch.object(ts_router, "write_points_v1", side_effect=fake_write),
        ):
            resp: Response = client.post("/api/timeshift/import", json=self._payload())
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
        events: list[dict[str, Any]] = _lines(resp.text.splitlines())
        assert events[0]["type"] == "progress" and events[0]["label"] == "Writing points · 2 of 2"
        assert events[-1]["type"] == "done" and events[-1]["points_written"] == 2
        assert "Wrote 2 points to InfluxDB" in events[-1]["html"]

    def test_a_failing_write_is_an_error_event(self, client: TestClient) -> None:
        df = pd.DataFrame({"Time": ["2020-01-01 00:00:00"], "v": [1.0]})
        with (
            patch.object(ts_router, "_require_bridge", return_value=MagicMock()),
            patch.object(ts_router, "get_upload", return_value=SimpleNamespace(dataframe=df, columns=["Time", "v"])),
            patch.object(ts_router, "load_influx_field_types", return_value={}),
            patch.object(ts_router, "check_influx_retention", return_value=None),
            patch.object(ts_router, "write_points_v1", side_effect=RuntimeError("connection reset")),
        ):
            resp: Response = client.post("/api/timeshift/import", json=self._payload())
        assert _lines(resp.text.splitlines())[-1] == {"type": "error", "detail": "Import failed: connection reset"}
