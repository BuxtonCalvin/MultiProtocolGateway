# Description: Unit tests for services/timeshift_service.py (tag dropdown values, EG4 multi-sheet consolidation, unmatched-metric defaults, timezone list).
# File: test_timeshift_service.py
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

"""Unit tests for services/timeshift_service.py."""

# pyright: strict

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from classes.WebServer.services import timeshift_service as ts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workbook_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """An in-memory .xlsx with one sheet per dict entry, like an EG4 export."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False) # type: ignore
    return buf.getvalue()


def _times(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq="5min")


# ---------------------------------------------------------------------------
# Unmatched EG4 metrics default to "Ignore"
# ---------------------------------------------------------------------------

class TestSuggestFieldMappingDefaults:
    FIELDS: list[str] = ["pv1_voltage", "soc"]

    def test_eg4_unmatched_column_starts_ignored(self) -> None:
        rows: list[ts.FieldMappingRow] = ts.suggest_field_mapping(["pv1Voltage", "Totally Unrelated Metric"], self.FIELDS, set(), "eg4")
        by_col: dict[str, ts.FieldMappingRow] = {r.source_column: r for r in rows}
        assert by_col["pv1Voltage"].suggested_field == "pv1_voltage"
        assert by_col["pv1Voltage"].default_ignored is False
        assert by_col["Totally Unrelated Metric"].suggested_field == ""
        assert by_col["Totally Unrelated Metric"].default_ignored is True

    def test_eg4_below_threshold_match_is_ignored_too(self) -> None:
        rows: list[ts.FieldMappingRow] = ts.suggest_field_mapping(["pv1Voltage"], self.FIELDS, set(), "eg4", confidence_threshold=1.5)
        assert rows[0].suggested_field == ""
        assert rows[0].default_ignored is True

    def test_eg4_new_measurement_does_not_ignore_everything(self) -> None:
        """No existing schema -> nothing can 'match', so nothing is pre-ignored on that basis."""
        rows: list[ts.FieldMappingRow] = ts.suggest_field_mapping(["pv1Voltage", "Anything"], [], set(), "eg4")
        assert [r.default_ignored for r in rows] == [False, False]

    def test_tag_and_reserved_columns_still_ignored(self) -> None:
        rows: list[ts.FieldMappingRow] = ts.suggest_field_mapping(["device_name", "time"], self.FIELDS, {"device_name"}, "eg4")
        assert all(r.default_ignored for r in rows)

    def test_influx_csv_unmatched_column_is_not_ignored(self) -> None:
        rows: list[ts.FieldMappingRow] = ts.suggest_field_mapping(["soc", "brand_new"], self.FIELDS, set(), "influx_csv")
        by_col: dict[str, ts.FieldMappingRow] = {r.source_column: r for r in rows}
        assert by_col["soc"].default_ignored is False
        assert by_col["brand_new"].default_ignored is False


# ---------------------------------------------------------------------------
# Multi-sheet EG4 consolidation
# ---------------------------------------------------------------------------

class TestConsolidateSheets:
    def test_sheets_with_different_time_ranges_are_stacked(self) -> None:
        a = pd.DataFrame({"Time": _times("2025-06-02 00:00", 3), "SOC": [50, 51, 52]})
        b = pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [10, 11, 12]})  # earlier, listed second
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"Day2": a, "Day1": b})
        df: pd.DataFrame = result.dataframe
        assert len(df) == 6
        assert list(df.columns) == ["Time", "SOC"]
        assert df["Time"].is_monotonic_increasing          # sorted regardless of sheet order
        assert df["Time"].iloc[0] == pd.Timestamp("2025-06-01 00:00")
        assert [s.name for s in result.sheets_used] == ["Day2", "Day1"]
        assert result.warnings == []

    def test_sheets_with_different_columns_same_times_are_joined(self) -> None:
        t = _times("2025-06-01 00:00", 3)
        a = pd.DataFrame({"Time": t, "SOC": [50, 51, 52]})
        b = pd.DataFrame({"Time": t, "Grid Voltage": [240.0, 241.0, 239.5]})
        df: pd.DataFrame = ts.consolidate_sheets({"Battery": a, "Grid": b}).dataframe
        assert len(df) == 3
        assert set(df.columns) == {"Time", "SOC", "Grid Voltage"}
        assert df["Grid Voltage"].tolist() == [240.0, 241.0, 239.5]
        assert df["SOC"].tolist() == [50, 51, 52]

    def test_time_column_names_may_differ_between_sheets(self) -> None:
        a = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        b = pd.DataFrame({"Date/Time": _times("2025-06-01 00:10", 2), "SOC": [3, 4]})
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"A": a, "B": b})
        assert result.time_column == "Time"                # first usable sheet's name wins
        assert list(result.dataframe.columns) == ["Time", "SOC"]
        assert len(result.dataframe) == 4

    def test_sheet_without_time_column_is_skipped_and_reported(self) -> None:
        data = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        info = pd.DataFrame({"Setting": ["Model", "Serial"], "Value": ["18kPV", "123"]})
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"Data": data, "Info": info})
        assert len(result.dataframe) == 2
        assert [(s.name, s.note) for s in result.sheets_skipped] == [("Info", "no time column found")]

    def test_empty_sheet_is_skipped(self) -> None:
        data = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"Data": data, "Blank": pd.DataFrame()})
        assert [s.name for s in result.sheets_skipped] == ["Blank"]

    def test_no_usable_sheet_raises(self) -> None:
        with pytest.raises(ValueError, match="recognizable time column"):
            ts.consolidate_sheets({"A": pd.DataFrame({"x": [1]}), "B": pd.DataFrame({"y": [2]})}, "f.xlsx")

    def test_conflicting_values_warn_and_first_sheet_wins(self) -> None:
        t: pd.DatetimeIndex = _times("2025-06-01 00:00", 2)
        a = pd.DataFrame({"Time": t, "Voltage": [1.0, 2.0]})
        b = pd.DataFrame({"Time": t, "Voltage": [9.0, 2.0]})
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"A": a, "B": b})
        assert result.dataframe["Voltage"].tolist() == [1.0, 2.0]
        assert len(result.warnings) == 1
        assert "Voltage" in result.warnings[0] and "'A'" in result.warnings[0]

    def test_identical_overlap_is_silent(self) -> None:
        """Adjacent day-sheets that repeat the boundary row shouldn't produce noise."""
        a = pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [1, 2, 3]})
        b = pd.DataFrame({"Time": _times("2025-06-01 00:10", 3), "SOC": [3, 4, 5]})  # 00:10 shared, same value
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"A": a, "B": b})
        assert result.warnings == []
        assert len(result.dataframe) == 5

    def test_rows_without_valid_time_are_dropped_and_noted(self) -> None:
        df = pd.DataFrame({"Time": ["2025-06-01 00:00", "Total", "2025-06-01 00:05"], "SOC": [1, 99, 2]})
        other = pd.DataFrame({"Time": ["2025-06-01 00:10"], "SOC": [3]})
        result: ts.ParsedSpreadsheet = ts.consolidate_sheets({"A": df, "B": other})
        assert len(result.dataframe) == 3
        assert "1 row(s) without a valid time" in result.sheets_used[0].note


class TestParseUpload:
    def test_multi_sheet_eg4_workbook_is_consolidated(self) -> None:
        data: bytes = _workbook_bytes({
            "Jun 1": pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [1, 2, 3]}),
            "Jun 2": pd.DataFrame({"Time": _times("2025-06-02 00:00", 3), "SOC": [4, 5, 6]}),
        })
        parsed: ts.ParsedSpreadsheet = ts.parse_upload("export.xlsx", data, "eg4")
        assert len(parsed.dataframe) == 6
        assert [s.name for s in parsed.sheets_used] == ["Jun 1", "Jun 2"]
        assert ts.earliest_timestamp(parsed.dataframe, parsed.time_column or "", "UTC") == "2025-06-01T00:00:00"

    def test_single_sheet_eg4_workbook_is_untouched(self) -> None:
        # Duplicate timestamps and unsorted rows must survive: the old code path did not dedupe or sort.
        df = pd.DataFrame({"Time": pd.to_datetime(["2025-06-01 00:05", "2025-06-01 00:00", "2025-06-01 00:00"]), "SOC": [1, 2, 3]})
        parsed: ts.ParsedSpreadsheet = ts.parse_upload("export.xlsx", _workbook_bytes({"Only": df}), "eg4")
        assert len(parsed.dataframe) == 3
        assert parsed.time_column is None
        assert parsed.sheets_used == []

    def test_csv_uses_existing_path(self) -> None:
        parsed = ts.parse_upload("x.csv", b"time,soc\n2025-06-01T00:00:00,5\n", "eg4")
        assert list(parsed.dataframe.columns) == ["time", "soc"]

    def test_influx_csv_workbook_only_reads_first_sheet_like_before(self) -> None:
        data = _workbook_bytes({
            "A": pd.DataFrame({"time": _times("2025-06-01 00:00", 2), "soc": [1, 2]}),
            "B": pd.DataFrame({"time": _times("2025-06-02 00:00", 2), "soc": [3, 4]}),
        })
        parsed: ts.ParsedSpreadsheet = ts.parse_upload("x.xlsx", data, "influx_csv")
        assert len(parsed.dataframe) == 2

    def test_unsupported_extension_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            ts.parse_upload("notes.txt", b"hi", "eg4")


class TestTimeColumnAndEarliest:
    def test_find_time_column_prefers_exact_then_datetime_dtype_then_prefix(self) -> None:
        assert ts.find_time_column(pd.DataFrame({"Time": [1], "Run Time (h)": [2]})) == "Time"
        assert ts.find_time_column(pd.DataFrame({"When": pd.to_datetime(["2025-01-01"]), "x": [1]})) == "When"
        assert ts.find_time_column(pd.DataFrame({"Timestamp (PST)": ["a"], "x": [1]})) == "Timestamp (PST)"

    def test_find_time_column_does_not_pick_a_measurement_with_time_inside(self) -> None:
        assert ts.find_time_column(pd.DataFrame({"Run Time (h)": [1], "Time to Full": [2]})) is None

    def test_earliest_timestamp_formats_for_datetime_local(self) -> None:
        df = pd.DataFrame({"Time": pd.Series([pd.Timestamp("2025-06-03 10:00:07"), pd.Timestamp("2025-06-01 08:30:15"), pd.NaT])})
        assert ts.earliest_timestamp(df, "Time", "America/Los_Angeles") == "2025-06-01T08:30:15"

    def test_earliest_timestamp_converts_tz_aware_to_local(self) -> None:
        df = pd.DataFrame({"Time": pd.to_datetime(["2025-06-01 12:00:00+00:00"])})
        assert ts.earliest_timestamp(df, "Time", "America/Los_Angeles") == "2025-06-01T05:00:00"

    def test_earliest_timestamp_missing_or_unparsable(self) -> None:
        assert ts.earliest_timestamp(pd.DataFrame({"a": [1]}), "Time", "UTC") is None
        assert ts.earliest_timestamp(pd.DataFrame({"Time": ["n/a", "x"]}), "Time", "UTC") is None


# ---------------------------------------------------------------------------
# Timezone dropdown
# ---------------------------------------------------------------------------

class TestTimezoneGroups:
    def test_groups_by_region_and_contains_common_zones(self) -> None:
        groups: dict[str, list[str]] = dict(ts.timezone_groups("America/Los_Angeles"))
        assert "America/Los_Angeles" in groups["America"]
        assert "Europe/London" in groups["Europe"]
        assert "UTC" in groups["Other"]
        assert list(groups)[-1] == "Other"                   # slash-less names sort last

    def test_current_zone_always_present_even_if_unlisted(self) -> None:
        with patch.object(ts, "available_timezones", return_value={"UTC"}):
            groups: dict[str, list[str]] = dict(ts.timezone_groups("Mars/Olympus_Mons"))
        assert "Mars/Olympus_Mons" in groups["Mars"]

    def test_posix_and_right_mirrors_excluded(self) -> None:
        with patch.object(ts, "available_timezones", return_value={"UTC", "posix/UTC", "right/UTC", "Factory"}):
            groups: dict[str, list[str]] = dict(ts.timezone_groups(""))
        assert groups == {"Other": ["UTC"]}


# ---------------------------------------------------------------------------
# Existing tag keys / values (mocked bridges)
# ---------------------------------------------------------------------------

class _V1Result:
    def __init__(self, points: list[dict[str, Any]]) -> None:
        self._points: list[dict[str, Any]] = points

    def get_points(self) -> list[dict[str, Any]]:
        return self._points


class _V3Table:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows: list[dict[str, Any]] = rows

    def to_pylist(self) -> list[dict[str, Any]]:
        return self._rows


class TestLoadTagOptions:
    def test_v1_returns_sorted_distinct_values_and_extra_tag_keys(self) -> None:
        client = MagicMock()

        def query(q: str, database: str = "") -> _V1Result:
            if q.startswith("SHOW TAG KEYS"):
                return _V1Result([{"tagKey": "device_identifier"}, {"tagKey": "site"}])
            assert q.startswith("SHOW TAG VALUES") and 'WITH KEY IN ("device_identifier", "site")' in q
            return _V1Result([
                {"key": "device_identifier", "value": "222"},
                {"key": "device_identifier", "value": "111"},
                {"key": "site", "value": "garage"},
            ])

        client.query.side_effect = query
        bridge = SimpleNamespace(client=client, database="mpg")
        with patch.object(ts, "get_influxdb1_bridge", return_value=bridge):
            opts: ts.TagOptions = ts.load_tag_options(None, "1", "device_data", ["device_identifier", "site"])

        assert opts.error == ""
        assert opts.tag_values == {"device_identifier": ["111", "222"], "site": ["garage"]}
        assert opts.tag_keys[: len(ts.STANDARD_TAG_KEYS)] == list(ts.STANDARD_TAG_KEYS)
        assert opts.tag_keys[-1] == "site"                      # discovered extra key appended once

    def test_v1_measurement_name_is_quoted(self) -> None:
        client = MagicMock()
        client.query.return_value = _V1Result([])
        with patch.object(ts, "get_influxdb1_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            ts.load_tag_options(None, "1", 'we"ird', ["device_identifier"])
        assert 'FROM "we\\"ird"' in client.query.call_args_list[0].args[0]

    def test_v3_uses_distinct_and_detects_dictionary_columns_as_tags(self) -> None:
        client = MagicMock()

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([
                    {"column_name": "time", "data_type": "Timestamp(Nanosecond, None)"},
                    {"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"},
                    {"column_name": "site", "data_type": "Dictionary(Int32, Utf8)"},
                    {"column_name": "soc", "data_type": "Float64"},
                ])
            assert "SELECT DISTINCT" in sql
            if '"device_identifier"' in sql:
                return _V3Table([{"device_identifier": "b"}, {"device_identifier": "a"}, {"device_identifier": None}])
            raise AssertionError(sql)

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier", "device_model"])

        assert opts.error == ""
        assert opts.tag_values["device_identifier"] == ["a", "b"]
        assert opts.tag_values["device_model"] == []            # not a column of this table -> no query, no values
        assert "site" in opts.tag_keys and "soc" not in opts.tag_keys and "time" not in opts.tag_keys

    def test_v3_distinct_query_is_time_bounded_to_the_narrowest_lookback(self) -> None:
        """Regression test: an unbounded SELECT DISTINCT over a whole measurement's history is exactly what trips InfluxDB 3 Core's Parquet file limit on an active/long-lived table."""
        client = MagicMock()

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([{"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"}])
            assert "now() - INTERVAL '3 days'" in sql, sql   # the narrowest window is tried first
            return _V3Table([{"device_identifier": "42"}])

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert opts.error == ""
        assert opts.tag_values == {"device_identifier": ["42"]}

    def test_v3_widens_the_lookback_only_when_the_narrow_one_found_nothing(self) -> None:
        client = MagicMock()
        calls: list[str] = []

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([{"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"}])
            calls.append(sql)
            if "3 days" in sql:
                return _V3Table([])   # nothing recent -- should trigger the wider window
            return _V3Table([{"device_identifier": "42"}])

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert opts.tag_values == {"device_identifier": ["42"]}
        assert len(calls) == 2 and "14 days" in calls[1]

    def test_v3_narrowest_window_file_limit_error_is_raised_not_swallowed(self) -> None:
        """A file-limit error on the FIRST (narrowest) window is unusual enough to surface as a real error, same as list_metric_edit_devices's own asymmetry -- it is not silently treated as 'no values'."""
        client = MagicMock()

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([{"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"}])
            raise _file_limit_error()

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert "file limit" in opts.error.lower()   # surfaced via load_tag_options's own outer catch-all, not raised out of it
        assert opts.tag_values == {}

    def test_v3_wider_window_file_limit_error_is_swallowed(self) -> None:
        """Unlike the narrowest window, a file-limit error on a WIDER window degrades to 'no values found' rather than failing the whole lookup -- some other key may still resolve fine."""
        client = MagicMock()

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([{"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"}])
            if "3 days" in sql:
                return _V3Table([])
            raise _file_limit_error()

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert opts.error == ""                              # not reported as a failure...
        assert opts.tag_values == {"device_identifier": []}  # ...just no suggestions from either window

    def test_v3_non_file_limit_error_on_wider_window_still_raises(self) -> None:
        client = MagicMock()

        def query(sql: str, database: str = "", language: str = "") -> _V3Table:
            if "information_schema.columns" in sql:
                return _V3Table([{"column_name": "device_identifier", "data_type": "Dictionary(Int32, Utf8)"}])
            if "3 days" in sql:
                return _V3Table([])
            raise RuntimeError("connection refused")

        client.query.side_effect = query
        with patch.object(ts, "get_influxdb3_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert "connection refused" in opts.error

    def test_failure_is_reported_not_raised(self) -> None:
        client = MagicMock()
        client.query.side_effect = RuntimeError("boom")
        with patch.object(ts, "get_influxdb1_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts: ts.TagOptions = ts.load_tag_options(None, "1", "device_data", ["device_identifier"])
        assert opts.error == "boom"
        assert opts.tag_values == {}
        assert opts.tag_keys == list(ts.STANDARD_TAG_KEYS)      # still offers the standard keys

    def test_blank_measurement_does_no_query(self) -> None:
        with patch.object(ts, "get_influxdb1_bridge") as bridge:
            opts: ts.TagOptions = ts.load_tag_options(None, "1", "  ", ["device_identifier"])
        bridge.assert_not_called()
        assert opts.tag_values == {} and opts.tag_keys == list(ts.STANDARD_TAG_KEYS)

    def test_no_bridge_is_reported(self) -> None:
        with patch.object(ts, "get_influxdb3_bridge", return_value=None):
            opts: ts.TagOptions = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert "No connected InfluxDB v3 bridge" in opts.error


# ---------------------------------------------------------------------------
# Time-shift math across a DST change
# ---------------------------------------------------------------------------

LA = ZoneInfo("America/Los_Angeles")


def _shifted_first_point_utc(source_local: str, target_local: str, tz_name: str = "America/Los_Angeles") -> str:
    """Runs an EG4-style import whose first row sits at `source_local`, shifted to `target_local`, and returns that row's stored (UTC) time."""
    tz = ZoneInfo(tz_name)
    df = pd.DataFrame({"Time": pd.date_range(source_local, periods=3, freq="5min"), "v": [1, 2, 3]})
    source: datetime = datetime.fromisoformat(source_local).replace(tzinfo=tz)
    target: datetime = datetime.fromisoformat(target_local).replace(tzinfo=tz)
    points, _ = ts.build_points(
        df,
        measurement="m",
        mapping={"v": "v"},
        tags={"device_identifier": "1"},
        time_column="Time",
        source_is_local=True,
        local_tz=tz_name,
        time_delta=ts.compute_time_delta(source, target),
        influx_field_types={},
        allow_float_coercion=True,
    )
    return points[0].time_iso


class TestTimeDeltaAcrossDst:
    def test_summer_to_winter_lands_exactly_on_target_start(self) -> None:
        # 2025-06-01 23:50 PDT (UTC-7) -> 2026-01-01 00:00 PST (UTC-8) == 08:00Z
        assert _shifted_first_point_utc("2025-06-01T23:50:00", "2026-01-01T00:00:00") == "2026-01-01T08:00:00"

    def test_winter_to_summer_lands_exactly_on_target_start(self) -> None:
        # 2025-12-01 12:00 PST (UTC-8) -> 2026-07-01 12:00 PDT (UTC-7) == 19:00Z
        assert _shifted_first_point_utc("2025-12-01T12:00:00", "2026-07-01T12:00:00") == "2026-07-01T19:00:00"

    def test_delta_is_measured_between_real_instants(self) -> None:
        source = datetime(2025, 6, 1, 23, 50, tzinfo=LA)
        target = datetime(2026, 1, 1, 0, 0, tzinfo=LA)
        assert ts.compute_time_delta(source, target) == timedelta(days=213, hours=1, minutes=10)

    def test_no_dst_change_between_dates_is_unaffected(self) -> None:
        source = datetime(2025, 6, 1, 8, 0, tzinfo=LA)
        target = datetime(2025, 7, 4, 8, 0, tzinfo=LA)
        assert ts.compute_time_delta(source, target) == timedelta(days=33)

    def test_no_shift_when_source_equals_target(self) -> None:
        moment = datetime(2025, 11, 2, 0, 30, tzinfo=LA)
        assert ts.compute_time_delta(moment, moment) == timedelta(0)

    def test_zone_without_dst_is_plain_wall_clock_difference(self) -> None:
        assert _shifted_first_point_utc("2025-06-01T00:00:00", "2026-01-01T00:00:00", "Asia/Kolkata") == "2025-12-31T18:30:00"

    def test_mixed_timezones_compare_as_instants(self) -> None:
        source = datetime(2025, 6, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        target = datetime(2025, 6, 1, 12, 0, tzinfo=LA)          # 19:00Z
        assert ts.compute_time_delta(source, target) == timedelta(hours=7)

    def test_naive_datetimes_fall_back_to_plain_subtraction(self) -> None:
        assert ts.compute_time_delta(datetime(2025, 1, 1), datetime(2025, 1, 2)) == timedelta(days=1)  # noqa: DTZ001 -- naive on purpose


class TestExportAcrossDst:
    def test_export_shifts_first_row_onto_target_start(self) -> None:
        """The export path uses the same delta: a row at Source Start must be written at Target Start (as UTC)."""
        row_time = datetime(2025, 6, 2, 6, 50, tzinfo=ZoneInfo("UTC"))          # == 2025-06-01 23:50 PDT
        client = MagicMock()
        client.query.return_value = _V3Table([{"time": row_time, "soc": 50.0, "device_identifier": "1"}])
        bridge = SimpleNamespace(client=client, database="mpg")

        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            header, rows = ts.export_range_rows(
                None, "3", "device_data", None,
                start_time=datetime(2025, 6, 1, 23, 50, tzinfo=LA),
                end_time=datetime(2025, 6, 2, 0, 50, tzinfo=LA),
                target_start=datetime(2026, 1, 1, 0, 0, tzinfo=LA),
                tag_keys={"device_identifier"},
            )

        assert header == ["time", "soc"]
        assert rows[0]["time"] == "2026-01-01T08:00:00"


# ---------------------------------------------------------------------------
# TimescaleDB
# ---------------------------------------------------------------------------

class TestTimescaleListingWrappers:
    """timescale_tables_for/timescale_devices_for/timescale_fields_for/load_timescale_field_types are thin re-exports of bridge_service's own Metrics Edit functions."""

    def test_tables_for_passes_through(self) -> None:
        with patch.object(ts, "_list_timescale_tables", return_value=[{"table_kind": "narrow", "protocol_name": None, "table_name": "device_metrics_narrow"}]) as mock:
            result: list[dict[str, str | None]] = ts.timescale_tables_for(None)
        mock.assert_called_once_with(None)
        assert result == [{"table_kind": "narrow", "protocol_name": None, "table_name": "device_metrics_narrow"}]

    def test_devices_for_passes_through(self) -> None:
        with patch.object(ts, "_list_timescale_devices", return_value=[{"device_info_id": 1}]) as mock:
            result: list[dict[str, str | int | None]] = ts.timescale_devices_for(None, "wide", "eg4_18kpv")
        mock.assert_called_once_with(None, "wide", "eg4_18kpv")
        assert result == [{"device_info_id": 1}]

    def test_fields_for_passes_through_with_device_scope(self) -> None:
        with patch.object(ts, "_list_timescale_fields", return_value=[{"name": "soc", "data_type": "REAL"}]) as mock:
            result: list[dict[str, str | None]] = ts.timescale_fields_for(None, "narrow", None, device_info_id=7)
        mock.assert_called_once_with(None, "narrow", protocol_name=None, device_info_id=7)
        assert result == [{"name": "soc", "data_type": "REAL"}]

    def test_load_field_types_narrow_is_always_empty(self) -> None:
        with patch.object(ts, "timescale_fields_for") as mock:
            result: dict[str, str] = ts.load_timescale_field_types(None, "narrow", None)
        mock.assert_not_called()
        assert result == {}

    def test_load_field_types_wide_maps_name_to_data_type(self) -> None:
        fields: list[dict[str, str] | dict[str, str | None]] = [{"name": "pv1_voltage", "data_type": "DOUBLE PRECISION"}, {"name": "soc", "data_type": "SMALLINT"}, {"name": "no_type", "data_type": None}]
        with patch.object(ts, "timescale_fields_for", return_value=fields):
            result: dict[str, str] = ts.load_timescale_field_types(None, "wide", "eg4_18kpv")
        assert result == {"pv1_voltage": "DOUBLE PRECISION", "soc": "SMALLINT"}


class TestBuildPointsTimescale:
    def _df(self, **cols: list[Any]) -> pd.DataFrame:
        return pd.DataFrame(cols)

    def test_narrow_target_never_coerces(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], soc=["not a number"])
        points, result = ts.build_points_timescale(
            df, table_kind="narrow", mapping={"soc": "soc"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0), wide_field_types={},
        )
        assert len(points) == 1
        assert points[0].fields == {"soc": "not a number"}   # untouched -- narrow sorts numeric vs ascii at write time, not here
        assert result.type_mismatches == []

    def test_wide_target_coerces_against_declared_type(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], soc=["42"], flag=["true"])
        points, result = ts.build_points_timescale(
            df, table_kind="wide", mapping={"soc": "soc", "flag": "flag"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0),
            wide_field_types={"soc": "SMALLINT", "flag": "BOOLEAN"},
            existing_wide_columns={"soc", "flag"},
        )
        assert points[0].fields == {"soc": 42.0, "flag": True}
        assert result.type_mismatches == []

    def test_wide_type_conflict_is_skipped_and_reported(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], soc=["not a number"], ok=["5"])
        points, result = ts.build_points_timescale(
            df, table_kind="wide", mapping={"soc": "soc", "ok": "ok"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0),
            wide_field_types={"soc": "SMALLINT", "ok": "SMALLINT"},
            existing_wide_columns={"soc", "ok"},
        )
        assert points[0].fields == {"ok": 5.0}
        assert len(result.type_mismatches) == 1
        assert "soc" in result.type_mismatches[0]

    def test_wide_out_of_range_integer_is_a_mismatch(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], v=["99999"])
        points, result = ts.build_points_timescale(
            df, table_kind="wide", mapping={"v": "v"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0), wide_field_types={"v": "SMALLINT"},
            existing_wide_columns={"v"},
        )
        assert points == []
        assert result.rows_skipped_no_fields == 1
        assert len(result.type_mismatches) == 1

    def test_wide_target_not_an_existing_column_is_rejected(self) -> None:
        # SECURITY: a mapping target that isn't a real, already-existing wide
        # column must never reach fields (and therefore never reach the raw
        # SQL column list _wide_upsert_sql builds) -- see
        # build_points_timescale's SECURITY note. Regression test for the
        # fix: previously any client-supplied target string was accepted
        # here as long as it type-checked against wide_field_types.get(target)
        # (None for an unknown name, treated as a permissive pass-through).
        df = self._df(Time=["2025-06-01 00:00:00"], evil=["1); DROP TABLE device_metrics_narrow; --"])
        points, result = ts.build_points_timescale(
            df, table_kind="wide", mapping={"evil": "col; DROP TABLE x; --"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0),
            wide_field_types={}, existing_wide_columns={"soc", "pv1_voltage"},
        )
        assert points == []
        assert result.rows_skipped_no_fields == 1
        assert result.unmapped_columns == {"evil"}

    def test_wide_with_no_existing_wide_columns_argument_rejects_everything(self) -> None:
        # existing_wide_columns defaults to None -- treated as "nothing is
        # whitelisted" (reject every wide target) rather than trusting an
        # unchecked name by default when a caller forgets to pass it.
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], soc=["42"])
        points, result = ts.build_points_timescale(
            df, table_kind="wide", mapping={"soc": "soc"}, time_column="Time",
            source_is_local=True, local_tz="UTC", time_delta=timedelta(0), wide_field_types={"soc": "SMALLINT"},
        )
        assert points == []
        assert result.unmapped_columns == {"soc"}

    def test_time_shift_matches_compute_time_delta(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 12:00:00"], v=[1])
        source = datetime(2025, 6, 1, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        target = datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        points, _ = ts.build_points_timescale(
            df, table_kind="narrow", mapping={"v": "v"}, time_column="Time", source_is_local=True,
            local_tz="America/Los_Angeles", time_delta=ts.compute_time_delta(source, target), wide_field_types={},
        )
        assert points[0].time_iso == "2026-01-01T08:00:00"   # same DST-correct math as InfluxDB's build_points

    def test_bad_time_and_no_fields_are_skipped(self) -> None:
        df: pd.DataFrame = self._df(Time=["not a time", "2025-06-01 00:00:00"], v=[1, None])
        points, result = ts.build_points_timescale(
            df, table_kind="narrow", mapping={"v": "v"}, time_column="Time", source_is_local=True,
            local_tz="UTC", time_delta=timedelta(0), wide_field_types={},
        )
        assert points == []
        assert result.rows_skipped_bad_time == 1
        assert result.rows_skipped_no_fields == 1

    def test_unmapped_column_is_reported_once(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], v=[1], extra=["x"])
        _points, result = ts.build_points_timescale(
            df, table_kind="narrow", mapping={"v": "v"}, time_column="Time", source_is_local=True,
            local_tz="UTC", time_delta=timedelta(0), wide_field_types={},
        )
        assert result.unmapped_columns == {"extra"}

    def test_ignored_column_mapped_to_empty_string_is_silent(self) -> None:
        df: pd.DataFrame = self._df(Time=["2025-06-01 00:00:00"], v=[1], tag_col=["x"])
        _points, result = ts.build_points_timescale(
            df, table_kind="narrow", mapping={"v": "v", "tag_col": ""}, time_column="Time", source_is_local=True,
            local_tz="UTC", time_delta=timedelta(0), wide_field_types={},
        )
        assert result.unmapped_columns == set()


class TestWritePointsTimescale:
    def _bridge(self) -> tuple[SimpleNamespace, MagicMock]:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.begin.return_value.__enter__ = MagicMock(return_value=None)
        session.begin.return_value.__exit__ = MagicMock(return_value=False)
        bridge = SimpleNamespace(SessionFactory=MagicMock(return_value=session), tsdb_connected=True)
        return bridge, session

    def test_narrow_only_writes_narrow(self) -> None:
        bridge, session = self._bridge()
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"soc": 50.0, "note": "ok"})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            written, narrow_rows = ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 7, points)
        assert (written, narrow_rows) == (1, 2)
        calls = session.execute.call_args_list
        assert len(calls) == 1   # only the narrow batch -- no wide insert at all
        sql_text, params = calls[0].args
        assert "device_metrics_narrow" in str(sql_text)
        assert len(params) == 2
        by_name: dict[Any, Any] = {p["metric_name"]: p for p in params}
        assert by_name["soc"] == {"m_time": datetime(2025, 6, 1, tzinfo=timezone.utc), "device_info_id": 7, "metric_name": "soc", "metric_value": 50.0, "metric_ascii": None}
        assert by_name["note"]["metric_value"] == 0.0 and by_name["note"]["metric_ascii"] == "ok"

    def test_wide_mirrors_same_fields_into_narrow(self) -> None:
        bridge, session = self._bridge()
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"pv1_voltage": 300.5})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            written, narrow_rows = ts.write_points_timescale(None, "wide", "device_metrics_wide__eg4_18kpv", 7, points)
        assert (written, narrow_rows) == (1, 1)
        assert session.execute.call_count == 2   # one wide upsert, one narrow batch
        wide_sql, wide_params = session.execute.call_args_list[0].args
        assert "device_metrics_wide__eg4_18kpv" in str(wide_sql) and "ON CONFLICT (m_time, device_info_id)" in str(wide_sql)
        assert wide_params == {"m_time": datetime(2025, 6, 1, tzinfo=timezone.utc), "device_info_id": 7, "pv1_voltage": 300.5}
        _narrow_sql, narrow_params = session.execute.call_args_list[1].args
        assert narrow_params == [{"m_time": datetime(2025, 6, 1, tzinfo=timezone.utc), "device_info_id": 7, "metric_name": "pv1_voltage", "metric_value": 300.5, "metric_ascii": None}]

    def test_boolean_field_mirrors_as_zero_or_one_in_narrow(self) -> None:
        bridge, session = self._bridge()
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"is_charging": True})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            ts.write_points_timescale(None, "wide", "device_metrics_wide__x", 1, points)
        narrow_params = session.execute.call_args_list[1].args[1]
        assert narrow_params[0]["metric_value"] == 1.0 and narrow_params[0]["metric_ascii"] is None

    def test_no_points_is_a_no_op(self) -> None:
        bridge, session = self._bridge()
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            result: tuple[int, int] = ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 1, [])
        assert result == (0, 0)
        session.execute.assert_not_called()

    def test_no_bridge_raises(self) -> None:
        with patch.object(ts, "get_timescale_bridge", return_value=None):
            with pytest.raises(RuntimeError, match="No TimescaleDB bridge"):
                ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 1, [ts.TimescalePointDict("2025-01-01T00:00:00", {"v": 1.0})])

    def test_disconnected_bridge_raises(self) -> None:
        bridge = SimpleNamespace(SessionFactory=MagicMock(), tsdb_connected=False)
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            with pytest.raises(RuntimeError, match="not connected"):
                ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 1, [ts.TimescalePointDict("2025-01-01T00:00:00", {"v": 1.0})])

    def test_unknown_table_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown table_kind"):
            ts.write_points_timescale(None, "sideways", "x", 1, [ts.TimescalePointDict("2025-01-01T00:00:00", {"v": 1.0})])

    def test_wide_write_pauses_and_decompresses_both_tables_in_range(self) -> None:
        # A wide import touches both the wide table and device_metrics_narrow
        # -- both should be prepared (paused + decompressed over the batch's
        # own time range) before the write, and both resumed afterward.
        bridge, _session = self._bridge()
        hypertable_mgr = MagicMock()
        hypertable_mgr.pause_compression_job_for_table.return_value = [42]
        bridge.hypertable_mgr = hypertable_mgr
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"pv1_voltage": 300.5})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            ts.write_points_timescale(None, "wide", "device_metrics_wide__eg4_18kpv", 7, points)

        paused_tables: set[Any] = {c.args[1] for c in hypertable_mgr.pause_compression_job_for_table.call_args_list}
        assert paused_tables == {"device_metrics_wide__eg4_18kpv", "device_metrics_narrow"}
        decompressed_tables: set[Any] = {c.args[1] for c in hypertable_mgr.decompress_chunks_in_range.call_args_list}
        assert decompressed_tables == {"device_metrics_wide__eg4_18kpv", "device_metrics_narrow"}
        # Range passed to decompress matches the (single) point's own timestamp.
        for call in hypertable_mgr.decompress_chunks_in_range.call_args_list:
            _session, _table, start, end = call.args
            assert start == end == datetime(2025, 6, 1, tzinfo=timezone.utc)
        # Every job paused (one per table here) is resumed with its own job_ids.
        resumed_job_ids: list[Any] = [c.args[1] for c in hypertable_mgr.resume_compression_job_for_table.call_args_list]
        assert resumed_job_ids == [[42], [42]]

    def test_narrow_only_write_prepares_only_narrow_table(self) -> None:
        bridge, _session = self._bridge()
        hypertable_mgr = MagicMock()
        hypertable_mgr.pause_compression_job_for_table.return_value = []
        bridge.hypertable_mgr = hypertable_mgr
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"soc": 50.0})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 7, points)

        paused_tables: set[Any] = {c.args[1] for c in hypertable_mgr.pause_compression_job_for_table.call_args_list}
        assert paused_tables == {"device_metrics_narrow"}
        # No job was paused (empty list), so nothing should be resumed.
        hypertable_mgr.resume_compression_job_for_table.assert_not_called()

    def test_decompress_failure_is_best_effort_and_does_not_block_the_write(self) -> None:
        bridge, _session = self._bridge()
        hypertable_mgr = MagicMock()
        hypertable_mgr.pause_compression_job_for_table.side_effect = Exception("boom")
        bridge.hypertable_mgr = hypertable_mgr
        points: list[ts.TimescalePointDict] = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"soc": 50.0})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            written, narrow_rows = ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 7, points)
        assert (written, narrow_rows) == (1, 1)   # the write itself still completed

    def test_no_hypertable_mgr_skips_pause_decompress_entirely(self) -> None:
        # SimpleNamespace has no hypertable_mgr attribute -- same as a bridge
        # class whose HyperTableManager hasn't been wired up for some reason;
        # must degrade to the pre-fix behavior (no pause/decompress calls at
        # all) rather than raising an AttributeError.
        bridge, _session = self._bridge()
        points = [ts.TimescalePointDict(time_iso="2025-06-01T00:00:00", fields={"soc": 50.0})]
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            written, narrow_rows = ts.write_points_timescale(None, "narrow", "device_metrics_narrow", 7, points)
        assert (written, narrow_rows) == (1, 1)


class TestWideUpsertSql:
    def test_upsert_shape(self) -> None:
        sql: str = ts._wide_upsert_sql("device_metrics_wide__x", ["a", "b"])  # pyright: ignore[reportPrivateUsage] -- exercising the SQL-shape helper directly
        assert sql == (
            "INSERT INTO device_metrics_wide__x (m_time, device_info_id, a, b) "
            "VALUES (:m_time, :device_info_id, :a, :b) "
            "ON CONFLICT (m_time, device_info_id) DO UPDATE SET a = EXCLUDED.a, b = EXCLUDED.b"
        )


class TestNarrowValuePair:
    """Exercises the numeric/text split helper directly."""

    def test_bool_becomes_one_or_zero(self) -> None:
        assert ts._narrow_value_pair(value=True) == (1.0, None)  # pyright: ignore[reportPrivateUsage]
        assert ts._narrow_value_pair(value=False) == (0.0, None)  # pyright: ignore[reportPrivateUsage]

    def test_numeric_stays_numeric(self) -> None:
        assert ts._narrow_value_pair(42) == (42.0, None)  # pyright: ignore[reportPrivateUsage]
        assert ts._narrow_value_pair(3.5) == (3.5, None)  # pyright: ignore[reportPrivateUsage]

    def test_string_goes_to_ascii(self) -> None:
        assert ts._narrow_value_pair("hello") == (0.0, "hello")  # pyright: ignore[reportPrivateUsage]


class TestExportRangeRowsTimescale:
    def _bridge_with_rows(self, rows: list[tuple[Any, ...]]) -> SimpleNamespace:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value = rows
        return SimpleNamespace(SessionFactory=MagicMock(return_value=session), tsdb_connected=True)

    def test_narrow_pivots_metric_rows_into_wide_shaped_rows(self) -> None:
        t0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2025, 6, 1, 0, 5, tzinfo=timezone.utc)
        bridge: SimpleNamespace = self._bridge_with_rows([
            (t0, "soc", 50.0, None), (t0, "note", None, "ok"),
            (t1, "soc", 51.0, None),
        ])
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            header, rows = ts.export_range_rows_timescale(
                None, "narrow", "device_metrics_narrow", None, 1,
                datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc),
            )
        assert header == ["time", "soc", "note"]
        assert rows == [
            {"time": "2025-06-01T00:00:00", "soc": 50.0, "note": "ok"},
            {"time": "2025-06-01T00:05:00", "soc": 51.0},
        ]

    def test_wide_reads_declared_columns_directly(self) -> None:
        t0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
        bridge: SimpleNamespace = self._bridge_with_rows([(t0, 300.5, 12)])
        with patch.object(ts, "get_timescale_bridge", return_value=bridge), \
             patch.object(ts, "timescale_fields_for", return_value=[{"name": "pv1_voltage"}, {"name": "soc"}]):
            header, rows = ts.export_range_rows_timescale(
                None, "wide", "device_metrics_wide__x", "eg4_18kpv", 1,
                datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc),
            )
        assert header == ["time", "pv1_voltage", "soc"]
        assert rows == [{"time": "2025-06-01T00:00:00", "pv1_voltage": 300.5, "soc": 12}]

    def test_applies_the_same_time_shift_as_influxdb_export(self) -> None:
        t0 = datetime(2025, 6, 1, 23, 50, tzinfo=timezone.utc)
        bridge: SimpleNamespace = self._bridge_with_rows([(t0, "soc", 50.0, None)])
        with patch.object(ts, "get_timescale_bridge", return_value=bridge):
            _header, rows = ts.export_range_rows_timescale(
                None, "narrow", "device_metrics_narrow", None, 1,
                datetime(2025, 6, 1, 23, 50, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
            )
        assert rows[0]["time"] == "2026-01-01T08:00:00"

    def test_empty_wide_field_list_yields_no_rows(self) -> None:
        bridge: SimpleNamespace = self._bridge_with_rows([])
        with patch.object(ts, "get_timescale_bridge", return_value=bridge), patch.object(ts, "timescale_fields_for", return_value=[]):
            header, rows = ts.export_range_rows_timescale(
                None, "wide", "x", "p", 1, datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc),
            )
        assert header == ["time"] and rows == []

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError, match="End time"):
            ts.export_range_rows_timescale(None, "narrow", "device_metrics_narrow", None, 1, datetime(2025, 6, 2, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc))

    def test_no_bridge_raises(self) -> None:
        with patch.object(ts, "get_timescale_bridge", return_value=None):
            with pytest.raises(ValueError, match="No connected TimescaleDB"):
                ts.export_range_rows_timescale(None, "narrow", "device_metrics_narrow", None, 1, datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc), datetime(2025, 6, 1, tzinfo=timezone.utc))


class TestMachineTimezoneForTimescale:
    def test_reads_from_transports_timescaledb(self) -> None:
        with patch.object(ts, "get_timescale_bridge", return_value=SimpleNamespace()):
            with patch("classes.transports.timescaledb.get_machine_timezone", return_value="America/Chicago"):
                assert ts.machine_timezone_for(None, "timescale") == "America/Chicago"

    def test_no_bridge_falls_back_to_utc(self) -> None:
        with patch.object(ts, "get_timescale_bridge", return_value=None):
            assert ts.machine_timezone_for(None, "timescale") == "UTC"


# ---------------------------------------------------------------------------
# InfluxDB v3 Core's per-query Parquet file limit (export_range_rows)
# ---------------------------------------------------------------------------

def _file_limit_error() -> Exception:
    return Exception("Query would scan 999 Parquet files, exceeding the file limit")


class TestQueryV3TimeSlices:
    """
    Unit tests for services.timeshift_service._query_v3_time_slices, the
    export-side counterpart of transports.influxdb3_out.Influx3AdminManager.
    _query_time_slices -- reusing that module's own _is_file_limit_error/
    _V3_MIN_SLICE so both agree on what a file-limit error is and how
    narrow is worth retrying.
    """

    def test_whole_range_accepted_is_a_single_call(self) -> None:
        calls: list[tuple[datetime, datetime, bool]] = []

        def run(start: datetime, end: datetime, end_inclusive: bool) -> list[dict[str, Any]]:
            calls.append((start, end, end_inclusive))
            return [{"v": 1}]

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 2, tzinfo=timezone.utc)
        result: list[dict[str, Any]] = ts._query_v3_time_slices(run, start, end)  # pyright: ignore[reportPrivateUsage]

        assert calls == [(start, end, True)]   # whole range, end-inclusive -- matches a single ordinary query
        assert result == [{"v": 1}]

    def test_file_limit_error_bisects_into_two_accepted_halves(self) -> None:
        calls: list[tuple[datetime, datetime, bool]] = []

        def run(start: datetime, end: datetime, end_inclusive: bool) -> list[dict[str, Any]]:
            calls.append((start, end, end_inclusive))
            if (end - start) > timedelta(hours=1):
                raise _file_limit_error()
            return [{"start": start.isoformat()}]

        start = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, 2, 0, tzinfo=timezone.utc)
        result: list[dict[str, Any]] = ts._query_v3_time_slices(run, start, end)  # pyright: ignore[reportPrivateUsage]

        midpoint: datetime = start + timedelta(hours=1)
        assert calls == [
            (start, end, True),                     # whole range -- rejected
            (start, midpoint, False),                # first half -- half-open, so the shared boundary isn't double-queried
            (midpoint, end, True),                   # second half -- keeps the original end_inclusive
        ]
        assert result == [{"start": start.isoformat()}, {"start": midpoint.isoformat()}]

    def test_bisection_recurses_until_narrow_enough(self) -> None:
        accepted_widths: list[timedelta] = []

        def run(start: datetime, end: datetime, end_inclusive: bool) -> list[dict[str, Any]]:
            width = end - start
            if width > timedelta(minutes=20):
                raise _file_limit_error()
            accepted_widths.append(width)
            return [{}]

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        end: datetime = start + timedelta(hours=2)
        result: list[dict[str, Any]] = ts._query_v3_time_slices(run, start, end)  # pyright: ignore[reportPrivateUsage]

        assert all(w <= timedelta(minutes=20) for w in accepted_widths)
        assert len(result) == len(accepted_widths) >= 4   # a 2-hour range needed more than one bisection to fit under 20 minutes

    def test_slice_at_the_floor_still_failing_reraises(self) -> None:
        def run(_start: datetime, _end: datetime, _end_inclusive: bool) -> list[dict[str, Any]]:
            raise _file_limit_error()

        start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        min_slice = timedelta(minutes=10)   # matches transports.influxdb3_out._V3_MIN_SLICE
        with pytest.raises(Exception, match="file limit"):
            ts._query_v3_time_slices(run, start, start + min_slice)  # pyright: ignore[reportPrivateUsage]

    def test_non_file_limit_error_is_not_bisected(self) -> None:
        calls = 0

        def run(_start: datetime, _end: datetime, _end_inclusive: bool) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            raise RuntimeError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            ts._query_v3_time_slices(run, datetime(2025, 6, 1, tzinfo=timezone.utc), datetime(2025, 6, 2, tzinfo=timezone.utc))  # pyright: ignore[reportPrivateUsage]
        assert calls == 1   # never retried/split for a non-file-limit error


class TestExportRangeRowsV3FileLimit:
    """export_range_rows()'s v3 branch, end to end, against a bridge whose query() enforces a (fake, narrow) file limit so the bisection path is actually exercised."""

    def _bridge_with_width_limit(self, limit: timedelta) -> tuple[SimpleNamespace, MagicMock]:
        client = MagicMock()

        def fake_query(sql: str, database: str = "", language: str = "") -> Any:
            literals: list[Any] = re.findall(r"'([\d\-T:.]+Z)'", sql)
            slice_start: datetime = datetime.strptime(literals[0], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            slice_end: datetime = datetime.strptime(literals[1], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            if (slice_end - slice_start) > limit:
                raise _file_limit_error()
            return _V3Table([{"time": slice_start + timedelta(minutes=1), "soc": 50.0, "device_identifier": "42"}])

        client.query.side_effect = fake_query
        return SimpleNamespace(client=client, database="mpg"), client

    def test_wide_range_is_split_and_every_slice_is_collected(self) -> None:
        bridge, client = self._bridge_with_width_limit(timedelta(hours=1))
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            header, rows = ts.export_range_rows(
                None, "3", "device_data", "42",
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), datetime(2025, 6, 1, 5, 0, tzinfo=timezone.utc),
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), set(),
            )
        assert client.query.call_count > 1     # the whole-range attempt failed and was split
        # Bisection halves the range regardless of remainder, so a 5-hour range under a 1-hour limit
        # doesn't split into exactly 5 clean hour-long slices -- just assert every accepted slice's
        # row made it through, and that splitting actually happened (checked above).
        assert len(rows) >= 5
        assert header == ["time", "soc", "device_identifier"]

    def test_narrow_range_needs_no_splitting(self) -> None:
        bridge, client = self._bridge_with_width_limit(timedelta(hours=1))
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            _header, rows = ts.export_range_rows(
                None, "3", "device_data", None,
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), datetime(2025, 6, 1, 0, 30, tzinfo=timezone.utc),
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), set(),
            )
        assert client.query.call_count == 1
        assert len(rows) == 1

    def test_device_identifier_filter_is_applied_to_every_slice(self) -> None:
        bridge, client = self._bridge_with_width_limit(timedelta(hours=1))
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            ts.export_range_rows(
                None, "3", "device_data", "42",
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), datetime(2025, 6, 1, 3, 0, tzinfo=timezone.utc),
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), set(),
            )
        assert client.query.call_count > 1
        assert all("device_identifier = '42'" in call.args[0] for call in client.query.call_args_list)

    def test_time_shift_still_applies_per_slice(self) -> None:
        bridge, _client = self._bridge_with_width_limit(timedelta(hours=1))
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            _header, rows = ts.export_range_rows(
                None, "3", "device_data", None,
                datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc), datetime(2025, 6, 1, 3, 0, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), set(),
            )
        assert all(row["time"].startswith("2026-01-01") for row in rows)   # every slice's rows got the same shift, not just the first

    def test_enterprise_like_bridge_never_splits(self) -> None:
        """A bridge with no per-query limit at all (Enterprise) issues exactly one query regardless of range width."""
        bridge, client = self._bridge_with_width_limit(timedelta(days=3650))
        with patch.object(ts, "get_influxdb3_bridge", return_value=bridge):
            ts.export_range_rows(
                None, "3", "device_data", None,
                datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2020, 1, 1, tzinfo=timezone.utc), set(),
            )
        assert client.query.call_count == 1
