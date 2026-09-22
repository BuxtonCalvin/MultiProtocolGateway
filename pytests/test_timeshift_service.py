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
from datetime import datetime, timedelta
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
        rows = ts.suggest_field_mapping(["pv1Voltage", "Totally Unrelated Metric"], self.FIELDS, set(), "eg4")
        by_col = {r.source_column: r for r in rows}
        assert by_col["pv1Voltage"].suggested_field == "pv1_voltage"
        assert by_col["pv1Voltage"].default_ignored is False
        assert by_col["Totally Unrelated Metric"].suggested_field == ""
        assert by_col["Totally Unrelated Metric"].default_ignored is True

    def test_eg4_below_threshold_match_is_ignored_too(self) -> None:
        rows = ts.suggest_field_mapping(["pv1Voltage"], self.FIELDS, set(), "eg4", confidence_threshold=1.5)
        assert rows[0].suggested_field == ""
        assert rows[0].default_ignored is True

    def test_eg4_new_measurement_does_not_ignore_everything(self) -> None:
        """No existing schema -> nothing can 'match', so nothing is pre-ignored on that basis."""
        rows = ts.suggest_field_mapping(["pv1Voltage", "Anything"], [], set(), "eg4")
        assert [r.default_ignored for r in rows] == [False, False]

    def test_tag_and_reserved_columns_still_ignored(self) -> None:
        rows = ts.suggest_field_mapping(["device_name", "time"], self.FIELDS, {"device_name"}, "eg4")
        assert all(r.default_ignored for r in rows)

    def test_influx_csv_unmatched_column_is_not_ignored(self) -> None:
        rows = ts.suggest_field_mapping(["soc", "brand_new"], self.FIELDS, set(), "influx_csv")
        by_col = {r.source_column: r for r in rows}
        assert by_col["soc"].default_ignored is False
        assert by_col["brand_new"].default_ignored is False


# ---------------------------------------------------------------------------
# Multi-sheet EG4 consolidation
# ---------------------------------------------------------------------------

class TestConsolidateSheets:
    def test_sheets_with_different_time_ranges_are_stacked(self) -> None:
        a = pd.DataFrame({"Time": _times("2025-06-02 00:00", 3), "SOC": [50, 51, 52]})
        b = pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [10, 11, 12]})  # earlier, listed second
        result = ts.consolidate_sheets({"Day2": a, "Day1": b})
        df = result.dataframe
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
        df = ts.consolidate_sheets({"Battery": a, "Grid": b}).dataframe
        assert len(df) == 3
        assert set(df.columns) == {"Time", "SOC", "Grid Voltage"}
        assert df["Grid Voltage"].tolist() == [240.0, 241.0, 239.5]
        assert df["SOC"].tolist() == [50, 51, 52]

    def test_time_column_names_may_differ_between_sheets(self) -> None:
        a = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        b = pd.DataFrame({"Date/Time": _times("2025-06-01 00:10", 2), "SOC": [3, 4]})
        result = ts.consolidate_sheets({"A": a, "B": b})
        assert result.time_column == "Time"                # first usable sheet's name wins
        assert list(result.dataframe.columns) == ["Time", "SOC"]
        assert len(result.dataframe) == 4

    def test_sheet_without_time_column_is_skipped_and_reported(self) -> None:
        data = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        info = pd.DataFrame({"Setting": ["Model", "Serial"], "Value": ["18kPV", "123"]})
        result = ts.consolidate_sheets({"Data": data, "Info": info})
        assert len(result.dataframe) == 2
        assert [(s.name, s.note) for s in result.sheets_skipped] == [("Info", "no time column found")]

    def test_empty_sheet_is_skipped(self) -> None:
        data = pd.DataFrame({"Time": _times("2025-06-01 00:00", 2), "SOC": [1, 2]})
        result = ts.consolidate_sheets({"Data": data, "Blank": pd.DataFrame()})
        assert [s.name for s in result.sheets_skipped] == ["Blank"]

    def test_no_usable_sheet_raises(self) -> None:
        with pytest.raises(ValueError, match="recognizable time column"):
            ts.consolidate_sheets({"A": pd.DataFrame({"x": [1]}), "B": pd.DataFrame({"y": [2]})}, "f.xlsx")

    def test_conflicting_values_warn_and_first_sheet_wins(self) -> None:
        t = _times("2025-06-01 00:00", 2)
        a = pd.DataFrame({"Time": t, "Voltage": [1.0, 2.0]})
        b = pd.DataFrame({"Time": t, "Voltage": [9.0, 2.0]})
        result = ts.consolidate_sheets({"A": a, "B": b})
        assert result.dataframe["Voltage"].tolist() == [1.0, 2.0]
        assert len(result.warnings) == 1
        assert "Voltage" in result.warnings[0] and "'A'" in result.warnings[0]

    def test_identical_overlap_is_silent(self) -> None:
        """Adjacent day-sheets that repeat the boundary row shouldn't produce noise."""
        a = pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [1, 2, 3]})
        b = pd.DataFrame({"Time": _times("2025-06-01 00:10", 3), "SOC": [3, 4, 5]})  # 00:10 shared, same value
        result = ts.consolidate_sheets({"A": a, "B": b})
        assert result.warnings == []
        assert len(result.dataframe) == 5

    def test_rows_without_valid_time_are_dropped_and_noted(self) -> None:
        df = pd.DataFrame({"Time": ["2025-06-01 00:00", "Total", "2025-06-01 00:05"], "SOC": [1, 99, 2]})
        other = pd.DataFrame({"Time": ["2025-06-01 00:10"], "SOC": [3]})
        result = ts.consolidate_sheets({"A": df, "B": other})
        assert len(result.dataframe) == 3
        assert "1 row(s) without a valid time" in result.sheets_used[0].note


class TestParseUpload:
    def test_multi_sheet_eg4_workbook_is_consolidated(self) -> None:
        data = _workbook_bytes({
            "Jun 1": pd.DataFrame({"Time": _times("2025-06-01 00:00", 3), "SOC": [1, 2, 3]}),
            "Jun 2": pd.DataFrame({"Time": _times("2025-06-02 00:00", 3), "SOC": [4, 5, 6]}),
        })
        parsed = ts.parse_upload("export.xlsx", data, "eg4")
        assert len(parsed.dataframe) == 6
        assert [s.name for s in parsed.sheets_used] == ["Jun 1", "Jun 2"]
        assert ts.earliest_timestamp(parsed.dataframe, parsed.time_column or "", "UTC") == "2025-06-01T00:00:00"

    def test_single_sheet_eg4_workbook_is_untouched(self) -> None:
        # Duplicate timestamps and unsorted rows must survive: the old code path did not dedupe or sort.
        df = pd.DataFrame({"Time": pd.to_datetime(["2025-06-01 00:05", "2025-06-01 00:00", "2025-06-01 00:00"]), "SOC": [1, 2, 3]})
        parsed = ts.parse_upload("export.xlsx", _workbook_bytes({"Only": df}), "eg4")
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
        parsed = ts.parse_upload("x.xlsx", data, "influx_csv")
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
        groups = dict(ts.timezone_groups("America/Los_Angeles"))
        assert "America/Los_Angeles" in groups["America"]
        assert "Europe/London" in groups["Europe"]
        assert "UTC" in groups["Other"]
        assert list(groups)[-1] == "Other"                   # slash-less names sort last

    def test_current_zone_always_present_even_if_unlisted(self) -> None:
        with patch.object(ts, "available_timezones", return_value={"UTC"}):
            groups = dict(ts.timezone_groups("Mars/Olympus_Mons"))
        assert "Mars/Olympus_Mons" in groups["Mars"]

    def test_posix_and_right_mirrors_excluded(self) -> None:
        with patch.object(ts, "available_timezones", return_value={"UTC", "posix/UTC", "right/UTC", "Factory"}):
            groups = dict(ts.timezone_groups(""))
        assert groups == {"Other": ["UTC"]}


# ---------------------------------------------------------------------------
# Existing tag keys / values (mocked bridges)
# ---------------------------------------------------------------------------

class _V1Result:
    def __init__(self, points: list[dict[str, Any]]) -> None:
        self._points = points

    def get_points(self) -> list[dict[str, Any]]:
        return self._points


class _V3Table:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

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
            opts = ts.load_tag_options(None, "1", "device_data", ["device_identifier", "site"])

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
            opts = ts.load_tag_options(None, "3", "device_data", ["device_identifier", "device_model"])

        assert opts.error == ""
        assert opts.tag_values["device_identifier"] == ["a", "b"]
        assert opts.tag_values["device_model"] == []            # not a column of this table -> no query, no values
        assert "site" in opts.tag_keys and "soc" not in opts.tag_keys and "time" not in opts.tag_keys

    def test_failure_is_reported_not_raised(self) -> None:
        client = MagicMock()
        client.query.side_effect = RuntimeError("boom")
        with patch.object(ts, "get_influxdb1_bridge", return_value=SimpleNamespace(client=client, database="mpg")):
            opts = ts.load_tag_options(None, "1", "device_data", ["device_identifier"])
        assert opts.error == "boom"
        assert opts.tag_values == {}
        assert opts.tag_keys == list(ts.STANDARD_TAG_KEYS)      # still offers the standard keys

    def test_blank_measurement_does_no_query(self) -> None:
        with patch.object(ts, "get_influxdb1_bridge") as bridge:
            opts = ts.load_tag_options(None, "1", "  ", ["device_identifier"])
        bridge.assert_not_called()
        assert opts.tag_values == {} and opts.tag_keys == list(ts.STANDARD_TAG_KEYS)

    def test_no_bridge_is_reported(self) -> None:
        with patch.object(ts, "get_influxdb3_bridge", return_value=None):
            opts = ts.load_tag_options(None, "3", "device_data", ["device_identifier"])
        assert "No connected InfluxDB v3 bridge" in opts.error


# ---------------------------------------------------------------------------
# Time-shift math across a DST change
# ---------------------------------------------------------------------------

LA = ZoneInfo("America/Los_Angeles")


def _shifted_first_point_utc(source_local: str, target_local: str, tz_name: str = "America/Los_Angeles") -> str:
    """Runs an EG4-style import whose first row sits at `source_local`, shifted to `target_local`, and returns that row's stored (UTC) time."""
    tz = ZoneInfo(tz_name)
    df = pd.DataFrame({"Time": pd.date_range(source_local, periods=3, freq="5min"), "v": [1, 2, 3]})
    source = datetime.fromisoformat(source_local).replace(tzinfo=tz)
    target = datetime.fromisoformat(target_local).replace(tzinfo=tz)
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
