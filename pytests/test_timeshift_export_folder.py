# Description: tests for the Timeshift export folder browser and "Export to Folder" endpoints.
# File: test_timeshift_export_folder.py
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

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from classes.WebServer.routers import timeshift as ts_router

FORM: dict[str, str] = {
    "version": "1", "measurement": "device_data",
    "start_time": "2026-01-01 00:00:00", "end_time": "2026-07-01 00:00:00",
    "target_start": "2026-01-01 00:00:00", "local_timezone": "UTC",
}


@pytest.fixture
def client() -> TestClient:
    app: FastAPI = FastAPI()
    app.include_router(ts_router.router)
    return TestClient(app)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root: Path = tmp_path.resolve()
    (root / "a").mkdir()
    (root / "b" / "c").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x")
    monkeypatch.setenv(ts_router.EXPORT_BROWSE_ROOT_ENV, str(root))
    return root


@pytest.fixture
def fake_export(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_a: Any, **_k: Any) -> tuple[str, str, int]:
        return "device_data_20260101_20260701_v1.csv", "time,soc\n2026-01-01T00:00:00,5\n", 1
    monkeypatch.setattr(ts_router, "_build_export_csv", fake)


class TestBrowse:
    def test_lists_only_visible_folders_and_starts_at_the_root(self, client: TestClient, tree: Path) -> None:
        data: dict[str, Any] = client.get("/api/timeshift/browse").json()
        assert [e["name"] for e in data["entries"]] == ["a", "b"]
        assert data["path"] == str(tree) and data["parent"] is None and data["writable"] is True

    def test_sub_folder_reports_its_parent(self, client: TestClient, tree: Path) -> None:
        data: dict[str, Any] = client.get("/api/timeshift/browse", params={"path": str(tree / "b")}).json()
        assert [e["name"] for e in data["entries"]] == ["c"] and data["parent"] == str(tree)

    def test_cannot_leave_the_root(self, client: TestClient, tree: Path) -> None:
        assert client.get("/api/timeshift/browse", params={"path": "/"}).status_code == 403
        assert client.get("/api/timeshift/browse", params={"path": str(tree / "b" / ".." / "..")}).status_code == 403

    def test_a_file_is_not_a_folder(self, client: TestClient, tree: Path) -> None:
        assert client.get("/api/timeshift/browse", params={"path": str(tree / "file.txt")}).status_code == 400


class TestSave:
    def test_writes_the_csv_and_never_overwrites(self, client: TestClient, tree: Path, fake_export: None) -> None:
        form: dict[str, str] = {**FORM, "folder": str(tree / "a")}
        first: dict[str, Any] = client.post("/api/timeshift/export/save", data=form).json()
        second: dict[str, Any] = client.post("/api/timeshift/export/save", data=form).json()
        assert Path(first["path"]).name == "device_data_20260101_20260701_v1.csv"
        assert Path(second["path"]).name == "device_data_20260101_20260701_v1_1.csv"
        assert Path(first["path"]).read_text() == "time,soc\n2026-01-01T00:00:00,5\n"
        assert first["rows"] == 1

    def test_refuses_a_folder_outside_the_root(self, client: TestClient, tree: Path, fake_export: None) -> None:
        assert client.post("/api/timeshift/export/save", data={**FORM, "folder": "/etc"}).status_code == 403

    def test_refuses_a_missing_folder(self, client: TestClient, tree: Path, fake_export: None) -> None:
        assert client.post("/api/timeshift/export/save", data={**FORM, "folder": str(tree / "nope")}).status_code == 400


class TestFilenames:
    def test_measurement_cannot_inject_path_parts(self) -> None:
        assert ts_router._safe_filename_part("../a b/c") == "a_b_c"  # pyright: ignore[reportPrivateUsage]
        assert ts_router._safe_filename_part("///") == "export"  # pyright: ignore[reportPrivateUsage]
