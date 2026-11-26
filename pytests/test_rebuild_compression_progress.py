# Description: Tests for the Rebuild Compression progress stream -- chunk counts on every progress event and the per-chunk log lines.
# File: test_rebuild_compression_progress.py
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

"""HyperTableManager.rebuild_compression() progress events and logging, run against a stand-in object (no database)."""

# pyright: basic

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from classes.transports.timescaledb import HyperTableManager

TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "classes" / "WebServer" / "templates"


def _stub(chunks_by_table: dict[str, list[str]], *, fail_on_call: int | None = None) -> SimpleNamespace:
    """A stand-in `self` for the unbound generator: one shared_narrow group, planning query returns each table's chunks."""
    current: dict[str, Any] = {"table": None}
    calls: dict[str, int] = {"convert": 0}
    session = MagicMock()

    def execute(statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql: str = str(statement)
        if "timescaledb_information.chunks" in sql:
            current["table"] = (params or {})["tname"]
            return SimpleNamespace(fetchall=lambda: [SimpleNamespace(chunk_name=c) for c in chunks_by_table.get(current["table"], [])])
        if "convert_to_columnstore" in sql:
            calls["convert"] += 1
            if fail_on_call is not None and calls["convert"] == fail_on_call:
                raise RuntimeError("could not obtain lock on chunk")
        return MagicMock()

    session.execute.side_effect = execute
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    return SimpleNamespace(
        get_dynamic_settings_helper=lambda: {"lock_timeout": "30s", "work_mem": "64MB"},
        _compression_group_tables=lambda protocol_names=None: [{"protocol_name": "shared_narrow", "selected": True, "tables": list(chunks_by_table)}],
        _get_hypertable_total_sizes=lambda tables: {t: 1000 * (i + 1) for i, t in enumerate(tables)},
        SessionFactory=factory,
        set_lock_timeout=MagicMock(),
        _maintain_rebuilt_table=MagicMock(),
        pause_compression_job_for_table=lambda session, table: [],
        resume_compression_job_for_table=MagicMock(),
        _log=logging.getLogger("test.rebuild_compression"),
    )


def _run(stub: SimpleNamespace) -> list[dict[str, Any]]:
    return list(HyperTableManager.rebuild_compression(stub, None))  # type: ignore[arg-type]


class TestProgressEvents:
    def test_every_event_carries_running_and_total_chunk_counts(self) -> None:
        events: list[dict[str, Any]] = _run(_stub({"raw": ["_hyper_1_1_chunk", "_hyper_1_2_chunk", "_hyper_1_3_chunk"], "hourly": ["_hyper_2_9_chunk"]}))
        progress: list[dict[str, Any]] = [e for e in events if e["type"] == "progress"]
        assert [e["chunks_done"] for e in progress] == [1, 2, 3, 4]
        assert {e["chunks_total"] for e in progress} == {4}
        assert events[-1]["type"] == "done"

    def test_bytes_still_weighted_and_reach_total_exactly_at_the_end(self) -> None:
        progress: list[dict[str, Any]] = [e for e in _run(_stub({"raw": ["a", "b"], "hourly": ["c"]})) if e["type"] == "progress"]
        assert progress[-1]["bytes_processed"] == progress[-1]["bytes_total"] == 3000
        assert [e["bytes_processed"] for e in progress] == sorted(e["bytes_processed"] for e in progress)

    def test_failed_chunk_still_advances_the_count(self) -> None:
        events: list[dict[str, Any]] = _run(_stub({"raw": ["a", "b", "c"]}, fail_on_call=2))
        progress: list[dict[str, Any]] = [e for e in events if e["type"] == "progress"]
        assert [e["chunks_done"] for e in progress] == [1, 2, 3]
        assert events[-1]["result"]["groups"][0]["chunks_failed"] == 1

    def test_nothing_compressed_means_no_progress_events(self) -> None:
        events: list[dict[str, Any]] = _run(_stub({"raw": []}))
        assert [e["type"] for e in events] == ["done"]


class TestMaintenanceStep:
    def test_runs_once_per_table_with_that_tables_chunks(self) -> None:
        stub: SimpleNamespace = _stub({"raw": ["a", "b"], "hourly": ["c"]})
        _run(stub)
        assert [c.args for c in stub._maintain_rebuilt_table.call_args_list] == [("raw", ["a", "b"]), ("hourly", ["c"])]

    def test_skipped_when_every_chunk_of_the_table_failed(self) -> None:
        stub: SimpleNamespace = _stub({"raw": ["a", "b"]}, fail_on_call=1)
        stub.SessionFactory.return_value.__enter__.return_value.execute.side_effect = _always_fail_convert(stub)
        _run(stub)
        stub._maintain_rebuilt_table.assert_not_called()

    def test_not_run_when_nothing_is_compressed(self) -> None:
        stub: SimpleNamespace = _stub({"raw": []})
        _run(stub)
        stub._maintain_rebuilt_table.assert_not_called()


def _always_fail_convert(stub: SimpleNamespace) -> Any:
    """Planning query still returns the chunks; every convert_to_columnstore call fails."""
    def execute(statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql: str = str(statement)
        if "timescaledb_information.chunks" in sql:
            return SimpleNamespace(fetchall=lambda: [SimpleNamespace(chunk_name="a"), SimpleNamespace(chunk_name="b")])
        if "convert_to_columnstore" in sql:
            raise RuntimeError("could not obtain lock on chunk")
        return MagicMock()
    return execute


def _maintenance_stub(resolved: str | None, *, fail_reindex_on: str | None = None) -> tuple[SimpleNamespace, list[str]]:
    """A stand-in `self` for _maintain_rebuilt_table(); returns it with the list of non-lookup statements it executed."""
    executed: list[str] = []
    conn = MagicMock()

    def execute(statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql: str = str(statement)
        if "continuous_aggregates" in sql:
            return SimpleNamespace(scalar=lambda: resolved)
        executed.append(sql)
        if fail_reindex_on is not None and sql.startswith("REINDEX") and fail_reindex_on in sql:
            raise RuntimeError("deadlock detected")
        return MagicMock()

    conn.execute.side_effect = execute
    conn.__enter__.return_value = conn
    engine = MagicMock(spec=Engine)
    engine.connect.return_value.execution_options.return_value = conn
    session = MagicMock()
    session.get_bind.return_value = engine
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    return SimpleNamespace(SessionFactory=factory, _log=logging.getLogger("test.rebuild_compression")), executed


class TestMaintainRebuiltTable:
    def _call(self, stub: SimpleNamespace, table: str, chunks: list[str]) -> None:
        HyperTableManager._maintain_rebuilt_table(stub, table, chunks)  # type: ignore[arg-type]

    def test_reindexes_each_chunk_concurrently_then_vacuums_the_table(self) -> None:
        stub, executed = _maintenance_stub("public.device_metrics_narrow")
        self._call(stub, "device_metrics_narrow", ["_hyper_1_1_chunk", "_timescaledb_internal._hyper_1_2_chunk"])
        assert executed == [
            "REINDEX TABLE CONCURRENTLY _timescaledb_internal._hyper_1_1_chunk",
            "REINDEX TABLE CONCURRENTLY _timescaledb_internal._hyper_1_2_chunk",
            "VACUUM (ANALYZE) public.device_metrics_narrow",
        ]

    def test_runs_on_an_autocommit_connection(self) -> None:
        stub, _ = _maintenance_stub("public.t")
        self._call(stub, "t", ["c"])
        engine = stub.SessionFactory.return_value.__enter__.return_value.get_bind.return_value
        engine.connect.return_value.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")

    def test_a_failed_reindex_does_not_stop_the_rest(self) -> None:
        stub, executed = _maintenance_stub("public.t", fail_reindex_on="_hyper_1_1_chunk")
        self._call(stub, "t", ["_hyper_1_1_chunk", "_hyper_1_2_chunk"])
        assert any(s.endswith("_hyper_1_2_chunk") for s in executed)
        assert executed[-1] == "VACUUM (ANALYZE) public.t"

    def test_missing_table_is_skipped_without_raising(self) -> None:
        stub, executed = _maintenance_stub(None)
        self._call(stub, "gone", ["c"])
        assert executed == []


class TestProgressLogging:
    def test_logs_start_each_chunk_and_finish(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="test.rebuild_compression"):
            _run(_stub({"raw": ["_hyper_1_1_chunk", "_hyper_1_2_chunk"]}))
        lines: list[str] = [r.getMessage() for r in caplog.records]
        assert any("starting -- 2 chunk(s) across 1 table(s)" in m for m in lines)
        assert any("[1/2, " in m and "'_hyper_1_1_chunk' rewritten" in m for m in lines)
        assert any("[2/2, 100.0%]" in m for m in lines)
        assert any("finished in" in m for m in lines)

    def test_failed_chunk_is_marked_in_the_progress_line(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="test.rebuild_compression"):
            _run(_stub({"raw": ["a", "b"]}, fail_on_call=1))
        assert any("chunk 'a' FAILED" in r.getMessage() for r in caplog.records)


class TestReadoutMarkup:
    def test_partial_has_the_text_element_next_to_the_thermometer(self) -> None:
        html: str = Jinja2Templates(directory=str(TEMPLATES_DIR)).get_template("partials/timescale_compression_group_list.html").render(groups=[])
        assert 'id="compression-progress-text"' in html and 'id="compression-progress-track"' in html
