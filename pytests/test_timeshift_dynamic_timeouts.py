# Description: tests for Timeshift's dynamic export/import timeouts and the weekly InfluxDB v1 export windows.
# File: test_timeshift_dynamic_timeouts.py
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

"""
Deliberately does NOT use the `_reuse_bridge_client` fixture the other timeshift test files have: these tests exercise
the real _open_v1_client()/_open_v3_client() helpers and the timeouts they are given.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from influxdb.resultset import ResultSet  # type: ignore[import-untyped]

from classes.WebServer.services import timeshift_service as ts

LA: ZoneInfo = ZoneInfo("America/Los_Angeles")
T0: datetime = datetime(2026, 1, 1, tzinfo=LA)


class TestTimeoutFormulas:
    def test_export_timeout_has_a_floor_scales_per_day_and_is_capped(self) -> None:
        assert ts.export_timeout_seconds(T0, T0) == ts.EXPORT_TIMEOUT_FLOOR_S
        assert ts.export_timeout_seconds(T0, T0 + timedelta(days=7)) == ts.EXPORT_TIMEOUT_FLOOR_S + 7 * ts.EXPORT_TIMEOUT_PER_DAY_S
        assert ts.export_timeout_seconds(T0, T0 + timedelta(days=3650)) == ts.EXPORT_TIMEOUT_CAP_S

    def test_a_six_month_export_gets_far_more_than_the_bridge_default(self) -> None:
        assert ts.export_timeout_seconds(T0, T0 + timedelta(days=183)) > 600

    def test_export_timeout_ignores_a_backwards_range(self) -> None:
        assert ts.export_timeout_seconds(T0 + timedelta(days=5), T0) == ts.EXPORT_TIMEOUT_FLOOR_S

    def test_v3_write_and_timescale_import_timeouts_grow_with_point_count_and_are_capped(self) -> None:
        assert ts.v3_write_timeout_seconds(0) == ts.V3_WRITE_TIMEOUT_FLOOR_S
        assert ts.v3_write_timeout_seconds(500_000) > ts.v3_write_timeout_seconds(1_000)
        assert ts.v3_write_timeout_seconds(10**12) == ts.V3_WRITE_TIMEOUT_CAP_S
        assert ts.timescale_import_timeout_seconds(0) == ts.TS_IMPORT_TIMEOUT_FLOOR_S
        assert ts.timescale_import_timeout_seconds(500_000) > ts.timescale_import_timeout_seconds(1_000)
        assert ts.timescale_import_timeout_seconds(10**12) == ts.TS_IMPORT_TIMEOUT_CAP_S


class TestClientHelpers:
    def test_v1_client_gets_the_requested_timeout_and_no_retries(self) -> None:
        bridge = SimpleNamespace(host="localhost", port=8086, username="u", password="p", database="db")  # noqa: S106 -- dummy value for a client that never connects
        client: Any = ts._open_v1_client(bridge, 1860.0)  # pyright: ignore[reportPrivateUsage]
        try:
            assert client._timeout == 1860.0  # pyright: ignore[reportPrivateUsage]
            assert client._retries == 0  # pyright: ignore[reportPrivateUsage]
        finally:
            client.close()

    def test_v3_client_timeouts_are_passed_in_milliseconds(self) -> None:
        bridge = SimpleNamespace(_build_client_kwargs=lambda: {"host": "http://localhost:8181", "token": "t", "database": "db"})
        with patch("influxdb_client_3.InfluxDBClient3") as ctor:
            ts._open_v3_client(bridge, query_timeout_s=1860.0, write_timeout_s=90.0)  # pyright: ignore[reportPrivateUsage]
        kwargs: dict[str, Any] = dict(ctor.call_args.kwargs)
        assert kwargs["query_timeout"] == 1_860_000
        assert kwargs["write_timeout"] == 90_000
        assert kwargs["host"] == "http://localhost:8181"


class _FakeV1Client:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed: bool = False

    def query(self, q: str, database: str = "", epoch: str = "") -> ResultSet:
        self.queries.append(q)
        payload: dict[str, Any] = {"series": [{"name": "m", "columns": ["time", "soc", "device_identifier"], "values": [[1_700_000_000_000_000_000, 5.0, "d"]]}]}
        return ResultSet(payload)

    def close(self) -> None:
        self.closed = True


class TestV1WeeklyWindows:
    def _run(self, days: float) -> tuple[_FakeV1Client, list[tuple[Any, ...]], list[dict[str, Any]]]:
        client = _FakeV1Client()
        opened: list[tuple[Any, ...]] = []

        def fake_open(bridge: Any, timeout_s: float) -> _FakeV1Client:
            opened.append((timeout_s,))
            return client

        bridge = SimpleNamespace(client=object(), database="db")
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge), patch.object(ts, "_open_v1_client", fake_open):
            _header, rows = ts.export_range_rows(None, "1", "m", None, T0, T0 + timedelta(days=days), T0, {"device_identifier"})
        return client, opened, rows

    def test_six_months_is_read_one_week_at_a_time_with_a_per_window_timeout(self) -> None:
        client, opened, rows = self._run(183)
        assert len(client.queries) == 27 and len(rows) == 27       # 26 full weeks + 1 day
        assert opened == [(ts.export_timeout_seconds(T0, T0 + ts.V1_EXPORT_WINDOW),)]   # one client, sized for one window -- not for 183 days
        assert client.closed

    def test_only_the_last_window_includes_its_end_so_no_boundary_point_is_read_twice(self) -> None:
        client, _opened, _rows = self._run(14)
        assert len(client.queries) == 2
        assert " AND time < '" in client.queries[0]
        assert " AND time <= '" in client.queries[1]

    def test_windows_are_contiguous(self) -> None:
        client, _opened, _rows = self._run(20)
        import re
        stamps: list[list[str]] = [re.findall(r"'(\d{4}-[\d\-T:.]+Z)'", q) for q in client.queries]
        for earlier, later in zip(stamps, stamps[1:]):
            assert earlier[1] == later[0]

    def test_short_range_is_a_single_query(self) -> None:
        client, _opened, _rows = self._run(0.1)
        assert len(client.queries) == 1


class TestStatementTimeout:
    def test_set_local_statement_timeout_uses_set_config_in_milliseconds(self) -> None:
        session = MagicMock()
        ts._set_local_statement_timeout(session, 61.5)  # pyright: ignore[reportPrivateUsage]
        stmt, params = session.execute.call_args.args
        assert "set_config('statement_timeout'" in str(stmt) and str(stmt).rstrip().endswith("true)")  # true == transaction-local (SET LOCAL)
        assert params == {"ms": "61500"}

    def test_timescale_import_sets_the_timeout_first_inside_its_transaction(self) -> None:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.begin.return_value.__enter__ = MagicMock(return_value=None)
        session.begin.return_value.__exit__ = MagicMock(return_value=False)
        bridge = SimpleNamespace(SessionFactory=MagicMock(return_value=session), tsdb_connected=True)
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"soc": 50.0})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge), patch.object(ts, "_copy_upsert"):
            ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 7, points)
        first_sql, first_params = session.execute.call_args_list[0].args
        assert "set_config('statement_timeout'" in str(first_sql)
        assert first_params == {"ms": str(int(ts.timescale_import_timeout_seconds(1) * 1000))}
