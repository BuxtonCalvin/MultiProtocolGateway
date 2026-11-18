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
        pause_compression_job_for_table=lambda session, table: [],
        resume_compression_job_for_table=MagicMock(),
        _log=logging.getLogger("test.rebuild_compression"),
    )


def _run(stub: SimpleNamespace) -> list[dict[str, Any]]:
    return list(HyperTableManager.rebuild_compression(stub, None))  # type: ignore[arg-type]


class TestProgressEvents:
    def test_every_event_carries_running_and_total_chunk_counts(self) -> None:
        events = _run(_stub({"raw": ["_hyper_1_1_chunk", "_hyper_1_2_chunk", "_hyper_1_3_chunk"], "hourly": ["_hyper_2_9_chunk"]}))
        progress = [e for e in events if e["type"] == "progress"]
        assert [e["chunks_done"] for e in progress] == [1, 2, 3, 4]
        assert {e["chunks_total"] for e in progress} == {4}
        assert events[-1]["type"] == "done"

    def test_bytes_still_weighted_and_reach_total_exactly_at_the_end(self) -> None:
        progress = [e for e in _run(_stub({"raw": ["a", "b"], "hourly": ["c"]})) if e["type"] == "progress"]
        assert progress[-1]["bytes_processed"] == progress[-1]["bytes_total"] == 3000
        assert [e["bytes_processed"] for e in progress] == sorted(e["bytes_processed"] for e in progress)

    def test_failed_chunk_still_advances_the_count(self) -> None:
        events = _run(_stub({"raw": ["a", "b", "c"]}, fail_on_call=2))
        progress = [e for e in events if e["type"] == "progress"]
        assert [e["chunks_done"] for e in progress] == [1, 2, 3]
        assert events[-1]["result"]["groups"][0]["chunks_failed"] == 1

    def test_nothing_compressed_means_no_progress_events(self) -> None:
        events = _run(_stub({"raw": []}))
        assert [e["type"] for e in events] == ["done"]


class TestProgressLogging:
    def test_logs_start_each_chunk_and_finish(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="test.rebuild_compression"):
            _run(_stub({"raw": ["_hyper_1_1_chunk", "_hyper_1_2_chunk"]}))
        lines = [r.getMessage() for r in caplog.records]
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
        html = Jinja2Templates(directory=str(TEMPLATES_DIR)).get_template("partials/timescale_compression_group_list.html").render(groups=[])
        assert 'id="compression-progress-text"' in html and 'id="compression-progress-track"' in html
