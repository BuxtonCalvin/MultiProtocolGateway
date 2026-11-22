# Description: tests for the Timeshift export download endpoint and its file-name handling.
# File: test_timeshift_export_csv.py
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
from collections.abc import Callable, Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from classes.WebServer.routers import timeshift as ts_router
from classes.WebServer.services import timeshift_service as ts

PARAMS: dict[str, str] = {
    "version": "1", "measurement": "device_data",
    "start_time": "2026-01-01 00:00:00", "end_time": "2026-07-01 00:00:00",
    "target_start": "2026-01-01 00:00:00", "local_timezone": "UTC",
}


@pytest.fixture
def client() -> TestClient:
    app: FastAPI = FastAPI()
    app.include_router(ts_router.router)
    return TestClient(app)


def _windows(*row_sets: list[dict[str, Any]]) -> Generator[ts.ExportWindow, None, None]:
    for index, rows in enumerate(row_sets, start=1):
        yield ts.ExportWindow(index, len(row_sets), rows)


def _ignore_progress(_label: str, _done: int, _total: int) -> None:
    """A progress callback that discards everything."""


def _never_cancel() -> None:
    """A cancellation check that never cancels."""


def _record_into(seen: list[tuple[str, int, int]]) -> Callable[[str, int, int], None]:
    def record(label: str, done: int, total: int) -> None:
        seen.append((label, done, total))
    return record


def _fake_bridge(*_args: object) -> object:
    """Stands in for _require_bridge: any object is "a connected bridge"."""
    return object()


def _two_windows(*_args: object) -> Generator[ts.ExportWindow, None, None]:
    return _windows([{"time": "t1", "soc": 5}], [{"time": "t2", "soc": 6}])


def _events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestWriteExportCsv:
    def test_header_is_the_union_of_every_window_in_first_seen_order(self, tmp_path: Path) -> None:
        csv_path, spool = tmp_path / "out.csv", tmp_path / "out.spool"
        count: int = ts.write_export_csv(
            _windows([{"time": "t1", "soc": 5}], [{"time": "t2", "volts": 52.5, "soc": 6}]),
            csv_path, spool, _ignore_progress, _never_cancel,
        )
        assert count == 2
        assert csv_path.read_bytes() == b"time,soc,volts\r\nt1,5,\r\nt2,6,52.5\r\n"
        assert not spool.exists()

    def test_values_csv_would_have_written_survive_the_spool(self, tmp_path: Path) -> None:
        csv_path, spool = tmp_path / "out.csv", tmp_path / "out.spool"
        when: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts.write_export_csv(_windows([{"time": "t", "a": None, "b": True, "c": 1.5, "d": when, "e": "x,y"}]), csv_path, spool, _ignore_progress, _never_cancel)
        assert csv_path.read_bytes().decode("utf-8") == f'time,a,b,c,d,e\r\nt,,True,1.5,{when},"x,y"\r\n'

    def test_progress_never_goes_backwards_and_ends_full(self, tmp_path: Path) -> None:
        seen: list[tuple[str, int, int]] = []
        rows: list[dict[str, Any]] = [{"time": str(n), "v": n} for n in range(12_000)]
        ts.write_export_csv(_windows(rows[:6000], rows[6000:]), tmp_path / "o.csv", tmp_path / "o.spool", _record_into(seen), _never_cancel)
        fractions: list[float] = [done / total for _label, done, total in seen]
        assert fractions == sorted(fractions) and fractions[-1] == 1.0
        assert seen[0][0].startswith("Reading data: window 1 of 2")

    def test_cancelling_removes_every_file(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def cancel() -> None:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("cancelled")

        csv_path, spool = tmp_path / "o.csv", tmp_path / "o.spool"
        with pytest.raises(RuntimeError):
            ts.write_export_csv(_windows([{"time": "a"}], [{"time": "b"}], [{"time": "c"}]), csv_path, spool, _ignore_progress, cancel)
        assert not csv_path.exists() and not spool.exists()


class TestExportEndpoints:
    def test_stream_reports_progress_then_a_token_that_downloads_the_file_once(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ts_router, "_require_bridge", _fake_bridge)
        monkeypatch.setattr(ts_router, "iter_export_windows", _two_windows)
        resp: Response = client.get("/api/timeshift/export/stream", params=PARAMS)
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
        events: list[dict[str, Any]] = _events(resp.text)
        assert events[-1]["type"] == "done" and events[-1]["rows"] == 2
        assert events[-1]["filename"] == "device_data_20260101_20260701_v1.csv"
        assert all(e["type"] == "progress" for e in events[:-1]) and events[:-1]

        download: Response = client.get(f"/api/timeshift/export/file/{events[-1]['token']}")
        assert download.status_code == 200 and download.headers["content-type"].startswith("text/csv")
        assert download.headers["content-disposition"] == 'attachment; filename="device_data_20260101_20260701_v1.csv"'
        assert download.text == "time,soc\r\nt1,5\r\nt2,6\r\n"
        assert client.get(f"/api/timeshift/export/file/{events[-1]['token']}").status_code == 404   # one-shot: deleted after sending

    def test_a_failure_while_reading_is_an_error_event(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any) -> Generator[ts.ExportWindow, None, None]:
            yield ts.ExportWindow(1, 2, [{"time": "t"}])
            raise RuntimeError("influx went away")

        monkeypatch.setattr(ts_router, "_require_bridge", _fake_bridge)
        monkeypatch.setattr(ts_router, "iter_export_windows", boom)
        events: list[dict[str, Any]] = _events(client.get("/api/timeshift/export/stream", params=PARAMS).text)
        assert events[-1] == {"type": "error", "detail": "Export failed: influx went away"}

    def test_a_bad_request_is_still_an_http_error_not_a_stream(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ts_router, "_require_bridge", _fake_bridge)
        resp: Response = client.get("/api/timeshift/export/stream", params={**PARAMS, "start_time": "not a date"})
        assert resp.status_code == 400

    def test_unknown_token_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/timeshift/export/file/nope").status_code == 404


class TestFilenames:
    def test_measurement_cannot_inject_path_parts_or_quotes(self) -> None:
        assert ts_router._safe_filename_part("../a b/c") == "a_b_c"  # pyright: ignore[reportPrivateUsage]
        assert ts_router._safe_filename_part('x"; y') == "x_y"  # pyright: ignore[reportPrivateUsage]
        assert ts_router._safe_filename_part("///") == "export"  # pyright: ignore[reportPrivateUsage]
