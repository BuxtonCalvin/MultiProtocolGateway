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

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from classes.WebServer.routers import timeshift as ts_router

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


class TestExportCsvEndpoint:
    def test_returns_the_csv_with_a_filename_the_page_can_read(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(*_a: Any, **_k: Any) -> tuple[str, str, int]:
            return "device_data_20260101_20260701_v1.csv", "time,soc\n2026-01-01T00:00:00,5\n", 1
        monkeypatch.setattr(ts_router, "_build_export_csv", fake)
        resp = client.get("/api/timeshift/export.csv", params=PARAMS)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert resp.headers["content-disposition"] == 'attachment; filename="device_data_20260101_20260701_v1.csv"'
        assert resp.text == "time,soc\n2026-01-01T00:00:00,5\n"


class TestFilenames:
    def test_measurement_cannot_inject_path_parts_or_quotes(self) -> None:
        assert ts_router._safe_filename_part("../a b/c") == "a_b_c"  # pyright: ignore[reportPrivateUsage]
        assert ts_router._safe_filename_part('x"; y') == "x_y"  # pyright: ignore[reportPrivateUsage]
        assert ts_router._safe_filename_part("///") == "export"  # pyright: ignore[reportPrivateUsage]
