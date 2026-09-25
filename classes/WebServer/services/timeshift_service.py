# Description: services/timeshift_service.py — Runtime helpers for the "InfluxDB -> Timeshift Data" admin screen: spreadsheet/CSV parsing, fuzzy field-name matching, time-shifted export, and mapped re-import for InfluxDB v1/v3.
# File: timeshift_service.py
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
services/timeshift_service.py — Runtime helpers backing routers/timeshift.py's
"InfluxDB -> Timeshift Data" admin screen.

This is the GUI-driven counterpart of tools/InfluxDateConverter.py. Every
hand-edited constant at the top of that script (HOST/PORT/USER/PASSWORD/
DATABASE, MEASUREMENT, LOCAL_TIME_ZONE, SOURCE_START_TIME/SOURCE_END_TIME/
TARGET_START_TIME, STATIC_TAGS, ALLOW_FLOAT_COERCION, CONFIDENCE_THRESHOLD)
becomes a field the admin fills in on the page instead -- nothing here reads
a module-level constant. Connection details are never re-entered: this
module always dispatches through the live influxdb_out (v1) / influxdb3_out
(v3) bridge already attached to the gateway, the same bridge
services/influxdb_service.py's Metrics Edit screen uses, via that module's
own get_influxdb1_bridge()/get_influxdb3_bridge() and
list_metric_edit_measurements()/list_metric_edit_fields() -- this file never
re-implements schema discovery, only the parts InfluxDateConverter.py adds
on top of it: fuzzy CSV<->field matching, value normalization/coercion, the
time-shift math, and the actual export/import I/O.

Two source kinds feed the same mapping UI:
  - "eg4": a spreadsheet exported from the EG4 monitoring website (or any
    similarly-shaped export) -- column names rarely match the InfluxDB
    schema, so normalize_metric_name()/guess_mapping() (ported verbatim
    from InfluxDateConverter.py) drive the suggested mapping.
  - "influx_csv": a CSV previously produced by this same screen's own
    Export step (optionally hand-edited in a spreadsheet) -- column names
    already ARE the InfluxDB field names (InfluxDateConverter.py's
    is_influx_source_csv branch), so the mapping is the identity mapping,
    shown for review/override rather than guessed.

Only two admin-manager-shaped calls are new here (schema listing is
entirely reused, per above): raw point writes (write_points_v1/
write_points_v3 below) and a raw ranged read for export
(export_range_rows below) -- InfluxV1AdminManager/Influx3AdminManager
intentionally expose no generic "read every field for a device in this
range" or "write arbitrary new points" call, since Metrics Edit only ever
edits/deletes what's already stored. Both go straight through each
bridge's own `.client`, same access pattern InfluxV1AdminManager/
Influx3AdminManager already use internally.

TimescaleDB is a third, structurally different destination (see the
"TimescaleDB" section far below): schema discovery is reused from
services/bridge_service.py's own Metrics Edit functions (get_timescale_
bridge/list_metric_edit_tables/list_metric_edit_devices/
list_metric_edit_fields) exactly the way this file reuses influxdb_
service's, but there is no free-typed "measurement" -- the admin picks a
TABLE (the one shared narrow table, or one protocol's wide table, whichever
exist) and a DEVICE from the same pickers Metrics Edit itself uses, in
place of InfluxDB's free-typed measurement and Tags panel. Writing to a
wide table always mirrors the same field values into the narrow table too
(build_points_timescale/write_points_timescale below), since the narrow
table is the durable long-format record and the wide table is a derived,
schema-limited convenience -- see WIDE_TABLE_COLUMN_LIMIT in transports/
timescaledb.py for why a heavily-instrumented protocol can lose its wide
table entirely and fall back to narrow-only.

Uploaded spreadsheets are parsed once and held server-side (in
app.state, same locked-dict pattern services/influxdb_service.py uses for
staged Metrics Edit entries) so the mapping GUI, preview, and import steps
all operate on one already-parsed DataFrame instead of re-uploading or
re-parsing the file at every step.
"""

# pyright: strict

from __future__ import annotations

import io
import logging
import math
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Callable, Generator, Iterable, Literal, cast
from zoneinfo import ZoneInfo, available_timezones

import pandas as pd
from starlette.datastructures import State

from .bridge_service import get_timescale_bridge
from .bridge_service import list_metric_edit_devices as _list_timescale_devices
from .bridge_service import list_metric_edit_fields as _list_timescale_fields
from .bridge_service import list_metric_edit_tables as _list_timescale_tables
from .device_service import NavData
from .influxdb_service import (
    get_influxdb1_bridge,
    get_influxdb3_bridge,
    list_metric_edit_fields,
    list_metric_edit_measurements,
)

if TYPE_CHECKING:
    from influxdb.resultset import ResultSet  # type: ignore[import-untyped]
    from influxdb_client_3 import InfluxDBClient3  # type: ignore[import-untyped]

    from protocol_gateway import Protocol_Gateway

    from ...transports.influxdb3_out import influxdb3_out
    from ...transports.influxdb_out import influxdb_out

_log: logging.Logger = logging.getLogger(__name__)

SourceKind = Literal["eg4", "influx_csv"]
InfluxVersion = Literal["1", "3"]

# The "version" string used throughout this module and routers/timeshift.py
# is really "which destination bridge" -- InfluxDB v1, v3, or the single
# TimescaleDB bridge. Kept as a plain str (not this Literal) in most
# function signatures for the same reason InfluxVersion above isn't used
# everywhere either: FastAPI path/query/body params arrive as str, and
# every function here already validates the value itself (or delegates to
# one that does) rather than trusting a static annotation alone.
DestinationVersion = Literal["1", "3", "timescale"]

TimescaleTableKind = Literal["narrow", "wide"]

# Columns that are never offered as mapping targets/sources -- these are
# either reserved (the timestamp itself) or become InfluxDB TAGS (supplied
# separately, as fixed values, by the "Tags" panel on the page) rather than
# FIELDS, mirroring STATIC_TAGS/IGNORE_TAGS in InfluxDateConverter.py. The
# actual tag KEYS in use are whatever the admin typed into the Tags panel
# (fully dynamic now, see routers/timeshift.py's TimeshiftTagsInput) -- this
# set only covers names that are reserved regardless of the tags chosen.
RESERVED_COLUMN_NAMES: set[str] = {"time", "measurement"}

DEFAULT_CONFIDENCE_THRESHOLD = 0.85
_FUZZY_CUTOFF = 0.55

# The tag keys MPG itself writes on every point (same six as
# InfluxDateConverter.py's STATIC_TAGS and influxdb3_out._INFLUX3_TAG_NAMES).
# Seeded as rows on the Tags panel, and always offered as tag-key suggestions
# even for a brand-new measurement that has no schema to discover yet.
STANDARD_TAG_KEYS: tuple[str, ...] = (
    "device_identifier", "device_name", "device_manufacturer",
    "device_model", "device_serial_number", "transport",
)

# Upper bound on how many distinct values are offered per tag key in the
# Tags panel's dropdowns -- real tag cardinality here (a handful of devices)
# is tiny; this only stops a mistyped/high-cardinality key from producing a
# multi-thousand-entry <select>.
MAX_TAG_VALUES: int = 500


# ---------------------------------------------------------------------------
# EG4 protocol detection -- gates whether "Import EG4 Spreadsheet" is even
# offered as a source option (per the page spec: only selectable if an EG4
# protocol is in use in this MPG instance).
# ---------------------------------------------------------------------------

def is_eg4_protocol_in_use(nav: NavData) -> bool:
    """
    True when at least one configured scraper's protocol_version starts
    with "eg4" (eg4_18kpv, eg4_3000ehv_v1, eg4_gridboss_re, eg4_ll_s,
    eg4_v58, ...ANY future eg4_* map). Drives whether the "Import EG4
    Spreadsheet" source option is enabled on the Timeshift Data screen --
    the EG4 field-name conventions normalize_metric_name() targets
    (pv1Voltage, BatStatus0_BMS, etc.) only apply to this family of maps.
    """
    return any(s.protocol_version.lower().startswith("eg4") for s in nav.scrapers)


def machine_timezone_for(gateway: "Protocol_Gateway | None", version: str) -> str:
    """
    The configured machine_timezone for whichever bridge `version` resolves
    to -- same helper as routers/influxdb.py's _machine_timezone, duplicated
    here (rather than imported) since that one is private to its module.
    Falls back to "UTC" if the bridge can't be resolved.

    TimescaleDB doesn't carry its timezone on the bridge object the way the
    InfluxDB transports do -- transports.timescaledb.get_machine_timezone()
    is a free function reading the module's own _TZengine singleton
    instead (see routers/timescale.py's identical call) -- so that branch
    calls it directly rather than reading a `.machine_timezone` attribute
    that wouldn't exist there.
    """
    if version == "timescale":
        if get_timescale_bridge(gateway) is None:
            return "UTC"
        from ...transports.timescaledb import (  # noqa: PLC0415 -- local: see write_points_timescale's identical note on deferring this transport's imports
            get_machine_timezone,
        )
        try:
            return get_machine_timezone()
        except Exception:
            return "UTC"
    bridge: Any | None = get_influxdb1_bridge(gateway) if version == "1" else get_influxdb3_bridge(gateway)
    return getattr(bridge, "machine_timezone", "UTC") if bridge is not None else "UTC"


def timezone_groups(current: str = "") -> list[tuple[str, list[str]]]:
    """
    Every IANA zone name the runtime knows, grouped by region ("America",
    "Europe", ...; slash-less names such as "UTC" land in "Other") for the
    Local Machine Timezone <select>'s <optgroup>s -- about 600 names, far
    easier to scan grouped. The posix/ and right/ mirror directories (and
    "Factory") that some tzdata layouts expose are left out. `current` (the
    bridge's configured zone) is always included even if the runtime doesn't
    list it, so the page can still preselect it rather than silently
    defaulting to whichever zone sorts first.
    """
    names: set[str] = {n for n in available_timezones() if not n.startswith(("posix/", "right/")) and n != "Factory"}
    if current:
        names.add(current)
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        region: str = name.split("/", 1)[0] if "/" in name else "Other"
        groups.setdefault(region, []).append(name)
    return [(region, groups[region]) for region in sorted(groups, key=lambda r: (r == "Other", r))]


# ---------------------------------------------------------------------------
# Fuzzy field-name matching -- ported verbatim (typing tightened) from
# tools/InfluxDateConverter.py's normalize_metric_name()/guess_mapping().
# ---------------------------------------------------------------------------

def normalize_metric_name(name: str) -> str:
    """
    Normalize metric names for fuzzy comparison.

    Handles:
        pv1Voltage        -> pv1_voltage
        AC Voltage (V)    -> ac_voltage
        BatStatus0_BMS    -> batstatus0_bms
        GridFrequencyHz   -> grid_frequency_hz
        TempC             -> temp_c
    """
    name = name.strip()
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([a-zA-Z])(\d)", r"\1_\2", name)
    name = re.sub(r"(\d)([a-zA-Z])", r"\1_\2", name)
    name = re.sub(r"[ \-]+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = re.sub(r"(voltage|volt)$", "voltage", name, flags=re.I)
    name = re.sub(r"(current|amp|amps|ampere)$", "current", name, flags=re.I)
    name = re.sub(r"(frequency|hz)$", "frequency", name, flags=re.I)
    name = re.sub(r"(percent|pct|%)$", "percent", name, flags=re.I)
    return name.lower().strip("_")


def guess_mapping(csv_field: str, influx_candidates: set[str]) -> tuple[str, float]:
    """
    Finds the best InfluxDB field-name match and returns (name, confidence).
    Returns ("", 0.0) if no match meets the cutoff.
    """
    normalized_csv: str = normalize_metric_name(csv_field)
    normalized_map: dict[str, str] = {normalize_metric_name(name): name for name in influx_candidates}

    best_match = ""
    best_score = 0.0
    matcher: SequenceMatcher[str] = SequenceMatcher(None, b=normalized_csv)

    for normalized_candidate in normalized_map:
        matcher.set_seq1(normalized_candidate)
        if matcher.quick_ratio() >= _FUZZY_CUTOFF:
            score: float = matcher.ratio()
            tokens_csv: set[str] = set(normalized_csv.split("_"))
            tokens_cand: set[str] = set(normalized_candidate.split("_"))
            score += 0.1 * len(tokens_csv & tokens_cand)
            if score >= _FUZZY_CUTOFF and score > best_score:
                best_score: float = score
                best_match: str = normalized_map[normalized_candidate]

    return best_match, round(best_score, 3)


@dataclass
class FieldMappingRow:
    """One row of the Field Matchup table shown by partials/timeshift_field_mapping.html."""
    source_column: str
    suggested_field: str            # "" if no confident guess (or identity, for influx_csv)
    confidence: float
    default_ignored: bool           # pre-checked "Ignore this column" (reserved/tag-shaped names, and unmatched EG4 metrics)


def suggest_field_mapping(
    source_columns: list[str],
    influx_fields: list[str],
    tag_keys: set[str],
    source_kind: SourceKind,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> list[FieldMappingRow]:
    """
    Builds the Field Matchup table rows the GUI renders for the admin to
    confirm/adjust -- the interactive replacement for InfluxDateConverter.
    py's perform_schema_validation()/mapping_needed.csv hand-edit cycle.

    For source_kind == "influx_csv" (a file this same screen previously
    exported), the mapping is the identity mapping for every column that
    already matches an InfluxDB field name exactly -- mirroring
    write_csv_to_influx()'s is_influx_source_csv branch, which skips fuzzy
    matching entirely since the columns already ARE the field names.

    For source_kind == "eg4", each column is fuzzy-matched against
    `influx_fields` via guess_mapping(); a match scoring at or above
    `confidence_threshold` is pre-filled. A column that does NOT reach the
    threshold starts with "Ignore" pre-checked -- an EG4 export carries
    many columns MPG never records, and silently creating a new InfluxDB
    field for each of them is rarely what the admin wants. The row is
    still shown, so the admin can un-check Ignore and either pick an
    existing field or "+ New field..." for it.

    Exception: when `influx_fields` is empty (a brand-new measurement with
    no schema yet) there is nothing for a column to match, so nothing is
    pre-ignored on that basis -- otherwise every column would start
    ignored and the whole file would be dropped by default.

    Columns in `tag_keys` (whatever the admin typed into the Tags panel)
    or RESERVED_COLUMN_NAMES are pre-checked "Ignore this column" by
    default, but still shown -- the admin can un-ignore and map them as an
    ordinary field if that's genuinely what they want.
    """
    influx_field_set: set[str] = set(influx_fields)
    rows: list[FieldMappingRow] = []

    for col in source_columns:
        default_ignored: bool = col in tag_keys or col.strip().lower() in RESERVED_COLUMN_NAMES

        if source_kind == "influx_csv":
            if col in influx_field_set:
                rows.append(FieldMappingRow(col, col, 1.0, default_ignored))
            else:
                rows.append(FieldMappingRow(col, "", 0.0, default_ignored))
            continue

        if default_ignored:
            rows.append(FieldMappingRow(col, "", 0.0, True))  # noqa: FBT003
            continue

        guess, score = guess_mapping(col, influx_field_set)
        if score >= confidence_threshold:
            rows.append(FieldMappingRow(col, guess, score, False))  # noqa: FBT003
        else:
            # Unmatched EG4 metric -> Ignore pre-checked (see docstring), unless there is no schema to match against.
            rows.append(FieldMappingRow(col, "", score, bool(influx_field_set)))

    return rows


# ---------------------------------------------------------------------------
# Spreadsheet parsing
# ---------------------------------------------------------------------------

def parse_spreadsheet_bytes(filename: str, data: bytes) -> pd.DataFrame:
    """
    Parses an uploaded EG4-export or previously-exported InfluxDB CSV into
    a DataFrame, dispatching on file extension -- .csv is read directly,
    .xls/.xlsx (the EG4 website's own export format, as in the example
    upload this screen was built against) via pandas' Excel readers.

    Raises:
        ValueError: unsupported extension, or the file has no columns.
    """
    lower: str = filename.lower()
    buf = io.BytesIO(data)

    if lower.endswith(".csv"):
        df: pd.DataFrame = pd.read_csv(buf)
    elif lower.endswith((".xlsx", ".xlsm")):
        df = pd.read_excel(buf, engine="openpyxl")  # type: ignore[reportUnknownMemberType]
    elif lower.endswith(".xls"):
        df = pd.read_excel(buf)  # type: ignore[reportUnknownMemberType]
    else:
        msg: str = f"Unsupported file type for '{filename}' -- expected .csv, .xls, or .xlsx."
        raise ValueError(msg)

    if df.empty and len(df.columns) == 0:
        msg: str = f"'{filename}' has no columns to import."
        raise ValueError(msg)

    return df


def detect_time_column(columns: list[str]) -> str | None:
    """Best-guess timestamp column for the Time Column picker's default selection."""
    lowered: dict[str, str] = {c.lower(): c for c in columns}
    for candidate in ("time", "timestamp", "date/time", "date"):
        if candidate in lowered:
            return lowered[candidate]
    return None


# A header that IS a time word, optionally followed by a parenthetical or a timezone word:
# "Date Time", "DateTime", "Time (UTC)", "Timestamp (PST)", "Date/Time Local". Anchored at both
# ends so measurements that merely start with "Time" ("Time to Full", "Time Remaining") don't match.
_TIME_NAME_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:date\s*/?\s*time|datetime|timestamp|time|date)(?:\s*\([^)]*\))?(?:\s+(?:local|utc|gmt|[ecmp][sd]t))?\s*$",
    re.IGNORECASE,
)


def find_time_column(df: pd.DataFrame) -> str | None:
    """
    Best-guess timestamp column of a parsed sheet/file. Tried in order of
    confidence: (1) an exact detect_time_column() name; (2) the first column
    pandas already typed as a datetime (what a real Excel date/time cell
    becomes, whatever the header says); (3) the first column whose header
    is a time word, optionally with a unit/timezone ("Date Time",
    "Timestamp (PST)", ...). Deliberately strict rather than a substring
    match -- "Run Time (h)" or "Time to Full" are measurements, not the
    timeline, and picking one of those silently would be worse than
    finding nothing.
    """
    columns: list[str] = [str(c) for c in df.columns]
    exact: str | None = detect_time_column(columns)
    if exact is not None:
        return exact
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return str(col)
    for name in columns:
        if _TIME_NAME_RE.match(name):
            return name
    return None


def earliest_timestamp(df: pd.DataFrame, time_column: str, local_tz: str) -> str | None:
    """
    The earliest valid timestamp in `time_column`, formatted for an
    <input type="datetime-local" step="1"> (YYYY-MM-DDTHH:MM:SS), or None if
    the column is missing or holds no parsable time. Drives the EG4 import's
    "Source Start" default. Naive values (an EG4 export is naive local time)
    are returned as-is; tz-aware ones are converted to `local_tz` first so
    the result is always in the same zone the page interprets it in.
    """
    if time_column not in df.columns:
        return None
    ts: pd.Series[pd.Timestamp] = pd.to_datetime(df[time_column], errors="coerce").dropna()
    if ts.empty:
        return None
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(ZoneInfo(local_tz)).dt.tz_localize(None)
    return ts.min().strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class SheetSummary:
    """One workbook sheet's outcome in a multi-sheet consolidation, shown in the Field Matchup panel."""
    name: str
    rows: int = 0
    note: str = ""


@dataclass
class ParsedSpreadsheet:
    """parse_upload()'s result: the DataFrame to store, plus what happened while building it."""
    dataframe: pd.DataFrame
    time_column: str | None = None          # set when consolidation normalized the time column's name
    sheets_used: list[SheetSummary] = field(default_factory=list[SheetSummary])
    sheets_skipped: list[SheetSummary] = field(default_factory=list[SheetSummary])
    warnings: list[str] = field(default_factory=list[str])


def consolidate_sheets(sheets: dict[str, pd.DataFrame], filename: str = "the workbook") -> ParsedSpreadsheet:
    """
    Merges every sheet of an EG4 workbook into ONE import table, keyed on
    timestamp.

    Each sheet needs a time column (found via find_time_column()); a sheet
    without one (a "Summary"/"Info" tab, say) or without any data is skipped
    and reported rather than failing the whole upload. The time column is
    renamed to the first usable sheet's name for it, so the result has a
    single time column no matter how each sheet labelled its own.

    The merge is an outer join on timestamp, which covers both layouts an
    export can plausibly have without having to guess which one it is:
      - sheets that hold DIFFERENT time ranges with the same columns
        (e.g. one sheet per day) -> the rows are simply stacked; and
      - sheets that hold DIFFERENT columns for the SAME timestamps
        (e.g. one sheet per device group) -> the columns are joined up
        side by side.
    Where two sheets both have a value for the same column at the same
    timestamp, the earlier sheet's non-blank value wins; if those values
    actually differ, a warning names the sheets and columns, so a
    same-named-but-different metric doesn't get silently blended.

    Raises:
        ValueError: no sheet has both data and a recognizable time column.
    """
    used: list[SheetSummary] = []
    skipped: list[SheetSummary] = []
    warnings: list[str] = []
    canonical: str | None = None
    indexed: list[tuple[str, pd.DataFrame]] = []

    for raw_name, raw_df in sheets.items():
        name: str = str(raw_name)
        df: pd.DataFrame = raw_df.copy()
        df.columns = [str(c) for c in df.columns]
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            skipped.append(SheetSummary(name, 0, "no data"))
            continue

        time_col: str | None = find_time_column(df)
        if time_col is None:
            skipped.append(SheetSummary(name, 0, "no time column found"))
            continue
        if canonical is None:
            canonical = time_col

        times: pd.Series[pd.Timestamp] = pd.to_datetime(df[time_col], errors="coerce")
        if times.dt.tz is not None:
            times = times.dt.tz_localize(None)  # keep wall-clock: the rest of the pipeline treats EG4 times as naive local
        valid: pd.Series[bool] = times.notna()
        bad_rows: int = int((~valid).sum())
        df = df.loc[valid].drop(columns=[time_col])
        if canonical in df.columns:
            warnings.append(f"Sheet '{name}' has a second '{canonical}' column; it was dropped in favour of the sheet's time column.")
            df = df.drop(columns=[canonical])
        if df.empty:
            skipped.append(SheetSummary(name, 0, "no rows with a valid time"))
            continue
        df.index = pd.DatetimeIndex(times.loc[valid], name=canonical)
        if df.index.has_duplicates:
            df = df.groupby(level=0, sort=False).first()  # repeated timestamps within one sheet -> first non-blank value per column

        note: str = f"{bad_rows} row(s) without a valid time were dropped" if bad_rows else ""
        used.append(SheetSummary(name, len(df), note))
        indexed.append((name, df))

    if not indexed or canonical is None:
        msg: str = f"None of the {len(sheets)} sheet(s) in '{filename}' has both data and a recognizable time column."
        raise ValueError(msg)

    for i, (name_a, a) in enumerate(indexed):
        for name_b, b in indexed[i + 1:]:
            common_cols: list[str] = [c for c in a.columns if c in b.columns]
            common_times: pd.Index[Any] = a.index.intersection(b.index)
            if not common_cols or common_times.empty:
                continue
            sa: pd.DataFrame = a.loc[common_times, common_cols]
            sb: pd.DataFrame = b.loc[common_times, common_cols]
            differs: pd.DataFrame = sa.notna() & sb.notna() & sa.ne(sb)
            conflicting: list[str] = [c for c in common_cols if bool(differs[c].any())]
            if conflicting:
                shown: str = ", ".join(conflicting[:5]) + (", ..." if len(conflicting) > 5 else "")
                warnings.append(
                    f"Sheets '{name_a}' and '{name_b}' both have values for {shown} at the same timestamps, "
                    f"and they differ -- the value from '{name_a}' was kept."
                )

    combined: pd.DataFrame = pd.concat([d for _, d in indexed])
    if combined.index.has_duplicates:
        combined = combined.groupby(level=0, sort=False).first()
    combined = combined.sort_index()
    combined.index.name = canonical
    combined = combined.reset_index()

    return ParsedSpreadsheet(combined, canonical, used, skipped, warnings)


_EXCEL_SUFFIXES: tuple[str, ...] = (".xlsx", ".xlsm", ".xls")


def parse_upload(filename: str, data: bytes, source_kind: SourceKind) -> ParsedSpreadsheet:
    """
    Entry point used by the upload endpoint. An EG4 workbook (.xlsx/.xlsm/.xls)
    with more than one sheet is read in full and consolidated (see
    consolidate_sheets()); everything else -- a .csv, a single-sheet
    workbook, or a re-import of this screen's own export -- goes through
    parse_spreadsheet_bytes() exactly as before.

    Raises:
        ValueError: unsupported extension, no columns, or (multi-sheet) no usable sheet.
    """
    lower: str = filename.lower()
    if source_kind != "eg4" or not lower.endswith(_EXCEL_SUFFIXES):
        return ParsedSpreadsheet(parse_spreadsheet_bytes(filename, data))

    engine: str | None = "openpyxl" if lower.endswith((".xlsx", ".xlsm")) else None
    sheets: dict[str, pd.DataFrame] = pd.read_excel(io.BytesIO(data), sheet_name=None, engine=engine)  # pyright: ignore[reportUnknownMemberType]
    if not sheets:
        msg: str = f"'{filename}' has no sheets to import."
        raise ValueError(msg)
    if len(sheets) == 1:
        only: pd.DataFrame = next(iter(sheets.values()))
        if only.empty and len(only.columns) == 0:
            msg = f"'{filename}' has no columns to import."
            raise ValueError(msg)
        return ParsedSpreadsheet(only)
    return consolidate_sheets(sheets, filename)


# ---------------------------------------------------------------------------
# Server-side upload store -- same locked-dict-in-app.state pattern
# services/influxdb_service.py uses for staged Metrics Edit entries
# (_influx_edit_store/_influx_edit_lock), applied here to parsed uploads so
# the mapping/preview/import steps share one DataFrame without re-uploading.
# ---------------------------------------------------------------------------

@dataclass
class TimeshiftUpload:
    upload_id: str
    filename: str
    source_kind: SourceKind
    dataframe: pd.DataFrame
    columns: list[str] = field(default_factory=list[str])
    uploaded_at: datetime = field(default_factory=datetime.now)


def _upload_store(app_state: State) -> dict[str, TimeshiftUpload]:
    if not hasattr(app_state, "timeshift_uploads"):
        setattr(app_state, "timeshift_uploads", {})
    return cast(dict[str, TimeshiftUpload], getattr(app_state, "timeshift_uploads"))


def _upload_lock(app_state: State) -> threading.RLock:
    if not hasattr(app_state, "timeshift_uploads_lock"):
        app_state.timeshift_uploads_lock = threading.RLock()
    return cast(threading.RLock, app_state.timeshift_uploads_lock)


def store_upload(app_state: State, filename: str, source_kind: SourceKind, df: pd.DataFrame) -> TimeshiftUpload:
    upload = TimeshiftUpload(
        upload_id=uuid.uuid4().hex,
        filename=filename,
        source_kind=source_kind,
        dataframe=df,
        columns=[str(c) for c in df.columns],
    )
    with _upload_lock(app_state):
        _upload_store(app_state)[upload.upload_id] = upload
    return upload


def get_upload(app_state: State, upload_id: str) -> TimeshiftUpload | None:
    with _upload_lock(app_state):
        return _upload_store(app_state).get(upload_id)


def discard_upload(app_state: State, upload_id: str) -> bool:
    with _upload_lock(app_state):
        return _upload_store(app_state).pop(upload_id, None) is not None


# ---------------------------------------------------------------------------
# Value normalization / type coercion -- ported verbatim (typing tightened)
# from tools/InfluxDateConverter.py.
# ---------------------------------------------------------------------------

def normalize_value(raw: Any) -> int | float | str | None:
    """Normalize a raw spreadsheet/CSV cell into a type safe for InfluxDB (see InfluxDateConverter.normalize_value)."""
    if raw is None:
        return None
    try:
        if isinstance(raw, float) and math.isnan(raw):
            return None
    except Exception:
        _log.exception("Error when checking for NaN in normalize_value()")
        pass

    if isinstance(raw, str):
        str_val: str = raw.strip()
        if not str_val:
            return None
        if str_val.startswith(("0x", "0X")):
            try:
                return int(str_val, 16)
            except ValueError:
                return str_val
        if str_val.endswith("%"):
            try:
                return float(str_val[:-1])
            except ValueError:
                return str_val
        if str_val.isdigit():
            try:
                return int(str_val)
            except ValueError:
                pass
        try:
            return float(str_val)
        except ValueError:
            return str_val

    return cast("int | float | str | None", raw)


def _normalize_influx_type_name(version: str, reported_type: str | None) -> str | None:
    """
    Maps each InfluxDB version's own field-type vocabulary onto the
    v1-style bucket names ("float"/"integer"/"string"/"boolean")
    coerce_to_influx_type()/check_field_type() below expect -- v1's SHOW
    FIELD KEYS already reports these names directly; v3's information_
    schema reports Arrow types (Float64, Int64, UInt64, Utf8, Boolean, ...).
    """
    if reported_type is None:
        return None
    if version == "1":
        return reported_type
    arrow_map: dict[str, str] = {
        "float64": "float", "float32": "float",
        "int64": "integer", "int32": "integer", "uint64": "integer", "uint32": "integer",
        "utf8": "string", "large_utf8": "string",
        "boolean": "boolean", "bool": "boolean",
    }
    return arrow_map.get(reported_type.lower(), reported_type.lower())


def coerce_to_influx_type(field_name: str, value: Any, influx_types: dict[str, str]) -> Any:
    """Coerce a value toward the expected InfluxDB field type (see InfluxDateConverter.coerce_to_influx_type)."""
    expected: str | None = influx_types.get(field_name)
    if expected is None:
        return value
    try:
        if expected == "string":
            return str(value)
        if expected == "float":
            return float(value)
        if expected == "integer":
            if isinstance(value, float) and 0 <= value < 1:
                return int(round(value * 100))
            return int(value)
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                v = value.lower().strip()
                if v in ("true", "1", "yes", "on"):
                    return True
                if v in ("false", "0", "no", "off"):
                    return False
    except (ValueError, TypeError):
        pass
    return value


def check_field_type(field_name: str, value: Any, influx_field_names: dict[str, str]) -> bool:
    """Reject a value whose type conflicts with the existing InfluxDB field type (see InfluxDateConverter.check_field_type)."""
    if field_name not in influx_field_names:
        return True
    expected: str = influx_field_names[field_name]
    if expected == "float" and not isinstance(value, (int, float)):
        return False
    if expected == "integer" and not isinstance(value, int):
        return False
    if expected == "boolean" and not isinstance(value, bool):
        return False
    if expected == "string" and not isinstance(value, str):
        return False
    return True


# ---------------------------------------------------------------------------
# Import (mapped spreadsheet/CSV -> InfluxDB), with the same time-shift math
# as InfluxDateConverter.write_csv_to_influx().
# ---------------------------------------------------------------------------

@dataclass
class InfluxPointDict:
    measurement: str
    time_iso: str
    fields: dict[str, int | float | str | bool]
    tags: dict[str, str]


@dataclass
class TimeshiftImportResult:
    points_written: int = 0
    rows_skipped_no_fields: int = 0
    rows_skipped_bad_time: int = 0
    unmapped_columns: set[str] = field(default_factory=set[str])
    type_mismatches: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])


def compute_time_delta(source_start: datetime, target_start: datetime) -> timedelta:
    """
    The same "hourly range on a different day" shift InfluxDateConverter.py
    applies to every point/row: the amount that moves `source_start` onto
    `target_start`.

    Timezone-aware inputs are compared as real instants (both converted to
    UTC first). This matters when the two dates fall on opposite sides of a
    daylight-saving change: Python's own `aware - aware` ignores the UTC
    offsets whenever both values share the same tzinfo object -- which they
    do here, both being stamped with the page's one Local Machine Timezone
    -- and subtracts the wall-clock readings alone. Since every stored
    timestamp is UTC, that left the shifted data an hour away from the
    Target Start whenever a DST boundary lay between Source and Target
    (e.g. a June source moved onto a January target).

    Naive inputs (no tzinfo) are subtracted as-is.
    """
    if source_start.tzinfo is not None and target_start.tzinfo is not None:
        utc = ZoneInfo("UTC")
        return target_start.astimezone(utc) - source_start.astimezone(utc)
    return target_start - source_start


def build_points(
    df: pd.DataFrame,
    *,
    measurement: str,
    mapping: dict[str, str],          # source_column -> influx_field_name (only mapped, non-ignored columns)
    tags: dict[str, str],
    time_column: str,
    source_is_local: bool,            # True: EG4 export, naive local time. False: a re-imported InfluxDB export CSV (already UTC-normalized on export, still shown/edited in local time -- see export_range_rows).
    local_tz: str,
    time_delta: timedelta,
    influx_field_types: dict[str, str],
    allow_float_coercion: bool,
    ) -> tuple[list[InfluxPointDict], TimeshiftImportResult]:
    """
    Builds one InfluxPointDict per input row, applying the same value
    normalization / type coercion / time-shift logic as
    InfluxDateConverter.write_csv_to_influx() -- generalized to take its
    field mapping and tag set as data (from the GUI) rather than the
    module's global field_mapper/STATIC_TAGS.
    """
    result = TimeshiftImportResult()
    points: list[InfluxPointDict] = []
    tz = ZoneInfo(local_tz)

    for row in df.to_dict("records"):
        time_val: Any = row.get(time_column)
        if time_val is None or (isinstance(time_val, float) and math.isnan(time_val)):
            result.rows_skipped_bad_time += 1
            continue

        try:
            ts: pd.Timestamp = pd.to_datetime(time_val)
            if ts.tzinfo is None:
                ts = ts.tz_localize(tz, nonexistent="shift_forward") if source_is_local else ts.tz_localize("UTC")
            new_time: pd.Timestamp = (ts.tz_convert("UTC") + time_delta).tz_localize(None)
        except (ValueError, TypeError):
            result.rows_skipped_bad_time += 1
            continue

        if pd.isna(new_time):
            result.rows_skipped_bad_time += 1
            continue

        fields: dict[str, int | float | str | bool] = {}
        for col, raw_val in row.items():
            col = str(col)
            if col == time_column or col in tags:
                continue
            target_field: str | None = mapping.get(col)
            if not target_field:
                if col in mapping:
                    continue  # admin explicitly mapped this column to "" (Ignore checked) -- silently skip, no warning
                result.unmapped_columns.add(col)
                continue

            val: Any = normalize_value(raw_val)
            if allow_float_coercion:
                val = coerce_to_influx_type(target_field, val, influx_field_types)
            if val is None:
                continue
            if not check_field_type(target_field, val, influx_field_types):
                result.type_mismatches.append(f"{target_field}={val!r} (row time {new_time.isoformat()})")
                continue
            fields[target_field] = val

        if not fields:
            result.rows_skipped_no_fields += 1
            continue

        points.append(InfluxPointDict(measurement=measurement, time_iso=new_time.isoformat(), fields=fields, tags=tags))

    return points, result


def write_points_v1(gateway: "Protocol_Gateway | None", points: list[InfluxPointDict], allow_float_coercion: bool) -> int:
    """
    Writes points to a live influxdb_out (v1) bridge via its own
    InfluxDBClient, same retry-on-type-conflict behavior as
    InfluxDateConverter.write_csv_to_influx()'s write loop.

    Raises:
        RuntimeError: no v1 bridge attached, or not connected.
    """
    from influxdb.exceptions import InfluxDBClientError  # type: ignore[import-untyped]

    bridge: influxdb_out | None = get_influxdb1_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v1 bridge is attached to this gateway.")
    client = bridge.client
    if client is None:
        raise RuntimeError("InfluxDB v1 bridge is not connected.")

    payload: list[dict[str, Any]] = [
        {"measurement": p.measurement, "time": p.time_iso, "fields": p.fields, "tags": p.tags} for p in points
    ]
    if not payload:
        return 0

    while True:
        try:
            client.write_points(payload, batch_size=1000)  # type: ignore[reportUnknownMemberType]
            return len(payload)
        except InfluxDBClientError as e:
            if allow_float_coercion and getattr(e, "code", None) == 400 and "field type conflict" in str(getattr(e, "content", "")):
                match: re.Match[str] | None = re.search(r'input field \\?"([^\\"]+)\\?"', str(e.content))  # type: ignore[reportUnknownArgumentType]  -- InfluxDBClientError.content has no type in the influxdb package's own (untyped) exception class
                if match:
                    offending_field: str = match.group(1)
                    _log.info(f"[Timeshift] Type conflict on field '{offending_field}' -- forcing to float and retrying.")
                    for pt in payload:
                        val: Any = pt["fields"].get(offending_field)
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            pt["fields"][offending_field] = float(val)
                    continue
            raise


def write_points_v3(gateway: "Protocol_Gateway | None", points: list[InfluxPointDict]) -> int:
    """Writes points to a live influxdb3_out (v3) bridge via its own InfluxDBClient3, using the same Point-building convention as influxdb3_out._dict_to_influx3_point()."""
    from influxdb_client_3 import Point  # type: ignore[import-untyped]

    bridge: influxdb3_out | None = get_influxdb3_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v3 bridge is attached to this gateway.")
    client: InfluxDBClient3 | None = bridge.client
    if client is None:
        raise RuntimeError("InfluxDB v3 bridge is not connected.")

    if not points:
        return 0

    influx_points: list[Point] = []
    for p in points:
        pt = Point(p.measurement)
        for tag_key, tag_val in p.tags.items():
            pt = pt.tag(tag_key, tag_val)  # type: ignore[reportUnknownMemberType]
        for field_key, field_val in p.fields.items():
            pt = pt.field(field_key, field_val)  # type: ignore[reportUnknownMemberType]
        pt = pt.time(pd.Timestamp(p.time_iso).value)  # type: ignore[reportUnknownMemberType]
        influx_points.append(pt)

    client.write(record=influx_points, database=bridge.database)  # type: ignore[reportUnknownMemberType]
    return len(influx_points)


def load_influx_field_types(gateway: "Protocol_Gateway | None", version: str, measurement: str) -> dict[str, str]:
    """[field_name] -> normalized type bucket ("float"/"integer"/"string"/"boolean"), for coerce/check above."""
    raw_fields: list[dict[str, str | None]] = list_metric_edit_fields(gateway, version, measurement)
    result: dict[str, str] = {}
    for f in raw_fields:
        name: str | None = f.get("name")
        normalized: str | None = _normalize_influx_type_name(version, f.get("data_type"))
        if name and normalized:
            result[name] = normalized
    return result


def measurements_for(gateway: "Protocol_Gateway | None", version: str) -> list[str]:
    """Thin re-export of influxdb_service.list_metric_edit_measurements(), for routers/timeshift.py's own naming."""
    return list_metric_edit_measurements(gateway, version)


# ---------------------------------------------------------------------------
# Existing tag keys / values -- feeds the Tags panel's dropdowns, so the admin
# picks the same device_identifier/device_name/... strings already stored
# rather than retyping them (and creating a near-duplicate series with a typo).
# Goes through each bridge's own `.client`, same as export_range_rows().
# ---------------------------------------------------------------------------

@dataclass
class TagOptions:
    tag_keys: list[str] = field(default_factory=list[str])                  # keys to suggest for the Tags panel's key inputs
    tag_values: dict[str, list[str]] = field(default_factory=dict[str, list[str]])   # requested key -> distinct stored values, sorted
    error: str = ""                                                          # non-empty if a query failed (whatever was found is still returned)


def _v3_column_types(bridge: Any, measurement: str) -> dict[str, str]:
    """[column_name] -> Arrow data_type for one v3 table, from information_schema.columns ({} for an unknown table)."""
    sql: str = (
        "SELECT column_name, data_type FROM information_schema.columns "  # noqa: S608
        f"WHERE table_schema NOT IN ('information_schema', 'system') AND table_name = {_sql_quote_literal(measurement)}"
    )
    table: Any = bridge.client.query(sql, database=bridge.database, language="sql")
    result: dict[str, str] = {}
    for row in table.to_pylist():
        record: dict[str, Any] = cast(dict[str, Any], row)
        column: Any = record.get("column_name")
        if column:
            result[str(column)] = str(record.get("data_type") or "")
    return result


def load_tag_options(
    gateway: "Protocol_Gateway | None",
    version: str,
    measurement: str,
    tag_keys: Iterable[str] = (),
    ) -> TagOptions:
    """
    Existing tag keys and, for each key in `tag_keys`, its distinct stored
    values in `measurement`.

    `tag_keys` in the result always starts with STANDARD_TAG_KEYS (so a
    brand-new measurement still gets sensible suggestions), followed by any
    other tag keys discovered on the measurement:
      - v1: SHOW TAG KEYS / SHOW TAG VALUES ... WITH KEY IN (...), both
        answered from the tag index (fast regardless of data volume).
      - v3: tags are the table's dictionary-typed columns in
        information_schema.columns; each requested key's values come from a
        SELECT DISTINCT over a recent, widening look-back window (see the
        inline comment above that loop) rather than the table's whole
        history, since an unbounded SELECT DISTINCT can trip InfluxDB 3
        Core's per-query Parquet file limit on an active/long-lived
        measurement -- a value that hasn't been written in the widest
        window tried is simply not offered as a suggestion; the admin can
        still type it in as a new value.

    Never raises for a query problem: a brand-new/unknown measurement just
    yields no values, and any other failure is logged and reported in
    `.error` alongside whatever was found, so the Tags panel degrades to
    plain "type a new value" instead of breaking.
    """
    options = TagOptions(tag_keys=list(STANDARD_TAG_KEYS))
    wanted: list[str] = list(dict.fromkeys(k for k in tag_keys if k and k.strip()))
    if not measurement.strip():
        return options

    def add_discovered(found: Iterable[str]) -> None:
        for key in sorted(set(found)):
            if key not in options.tag_keys:
                options.tag_keys.append(key)

    bridge: Any = get_influxdb1_bridge(gateway) if version == "1" else get_influxdb3_bridge(gateway)
    if bridge is None or bridge.client is None:
        options.error = f"No connected InfluxDB v{version} bridge is attached to this gateway."
        return options

    try:
        if version == "1":
            from influxdb.client import (  # pyright: ignore[reportMissingTypeStubs]
                quote_ident,  # pyright: ignore[reportUnknownVariableType]
            )

            quoted_measurement: str = quote_ident(measurement)
            key_result: Any = bridge.client.query(f"SHOW TAG KEYS FROM {quoted_measurement}", database=bridge.database)
            add_discovered(str(p["tagKey"]) for p in key_result.get_points() if "tagKey" in p)

            if wanted:
                keys_in: str = ", ".join(quote_ident(k) for k in wanted)
                value_result: Any = bridge.client.query(
                    f"SHOW TAG VALUES FROM {quoted_measurement} WITH KEY IN ({keys_in})", database=bridge.database,
                )
                found_values: dict[str, set[str]] = {k: set() for k in wanted}
                for point in value_result.get_points():
                    key: Any = point.get("key")
                    value: Any = point.get("value")
                    if key in found_values and value not in (None, ""):
                        found_values[key].add(str(value))
                options.tag_values = {k: sorted(v)[:MAX_TAG_VALUES] for k, v in found_values.items()}
        else:
            column_types: dict[str, str] = _v3_column_types(bridge, measurement)
            add_discovered(
                c for c, t in column_types.items()
                if c != "time" and (t.startswith("Dictionary") or c in STANDARD_TAG_KEYS)
            )

            # column_types comes from information_schema.columns (metadata only, cheap) and is safe
            # unbounded; the per-key SELECT DISTINCT below reads actual data, so -- like
            # transports.influxdb3_out.Influx3AdminManager.list_metric_edit_devices()'s own device
            # picker -- it must be time-bounded, or an active/long-lived measurement can trip
            # InfluxDB 3 CORE's per-query Parquet file limit ("Query would scan N Parquet files,
            # exceeding the file limit") on a query with no time filter at all. Reusing that same
            # look-back/widen sequence (_V3_DEVICE_LOOKBACKS, narrowest first, widening only while a
            # key has found nothing) keeps every windowed v3 query in this codebase agreeing on how
            # far back is worth trying, and _is_file_limit_error on what a file-limit failure even is.
            from ...transports.influxdb3_out import (  # noqa: PLC0415 -- local: see build_points_timescale's identical note on deferring a transport's imports
                _V3_DEVICE_LOOKBACKS,  # pyright: ignore[reportPrivateUsage] -- reused deliberately, see the comment above
                _is_file_limit_error,  # pyright: ignore[reportPrivateUsage]
            )

            for key in wanted:
                if key not in column_types:
                    options.tag_values[key] = []
                    continue
                quoted_key: str = _sql_quote_ident(key)
                values: set[str] = set()
                for index, window in enumerate(_V3_DEVICE_LOOKBACKS):
                    sql = (
                        f"SELECT DISTINCT {quoted_key} FROM {_sql_quote_ident(measurement)} "  # noqa: S608
                        f"WHERE time >= now() - INTERVAL '{window}' AND {quoted_key} IS NOT NULL LIMIT {MAX_TAG_VALUES}"
                    )
                    try:
                        table = bridge.client.query(sql, database=bridge.database, language="sql")
                    except Exception as exc:
                        # Only a WIDER window's file-limit failure is swallowed (same asymmetry as
                        # list_metric_edit_devices): the narrowest window is expected to always fit,
                        # so a file-limit error there is unusual enough to surface as a real error
                        # rather than silently reporting "no values" for it.
                        if index > 0 and _is_file_limit_error(exc):
                            _log.warning(
                                f"[Timeshift] Tag-value look-back of {window} exceeded InfluxDB's query "
                                f"file limit for '{key}' on '{measurement}'; showing values from the narrower window only."
                            )
                            break
                        raise
                    values = {
                        str(cell) for row in table.to_pylist()
                        if (cell := cast(dict[str, Any], row).get(key)) not in (None, "")
                    }
                    if values:
                        break
                options.tag_values[key] = sorted(values)
    except Exception as exc:
        _log.warning(f"[Timeshift] Could not load tag values for '{measurement}' (v{version}): {exc}")
        options.error = str(exc)

    return options


# ---------------------------------------------------------------------------
# Export (InfluxDB date range -> CSV), with the same time-shift math as
# InfluxDateConverter.export_influx_data_to_csv().
# ---------------------------------------------------------------------------

def _influxql_quote_literal(value: str) -> str:
    """Single-quotes an InfluxQL string literal, doubling any embedded single quote -- the counterpart of quote_ident (imported from the influxdb client below) for values rather than identifiers."""
    return "'" + value.replace("'", "''") + "'"


def _sql_quote_ident(name: str) -> str:
    """Double-quotes a DataFusion SQL identifier (table/column name), doubling any embedded double quote -- v3's counterpart of InfluxQL's quote_ident."""
    return '"' + name.replace('"', '""') + '"'


def _sql_quote_literal(value: str) -> str:
    """Single-quotes a DataFusion SQL string literal, doubling any embedded single quote."""
    return "'" + value.replace("'", "''") + "'"


def _query_v3_time_slices(
    run: Callable[[datetime, datetime, bool], list[dict[str, Any]]], start_time: datetime, end_time: datetime,
    ) -> list[dict[str, Any]]:
    """
    Runs `run(slice_start, slice_end, end_inclusive)` over [start_time,
    end_time] and concatenates the results, transparently splitting the
    range when InfluxDB 3 CORE rejects a query for touching too many
    Parquet files ("Query would scan N Parquet files, exceeding the file
    limit") -- Enterprise has no such per-query limit, so there this is a
    no-op: the first attempt succeeds and `run` is called exactly once.

    This is the same file-limit workaround transports.influxdb3_out.
    Influx3AdminManager._query_time_slices() already gives Metrics Edit's
    own device/field/edit queries (list_metric_edit_devices,
    preview_metric_edit, edit_metric_values) -- export_range_rows()'s v3
    branch was the one query in this file still shaped as a single SELECT
    over the whole requested range, which is exactly the shape that trips
    Core's limit on a wide date range. Reusing the transport's own
    _is_file_limit_error/_V3_MIN_SLICE (rather than re-deciding what
    counts as a file-limit error, or how narrow is worth trying, a second
    time here) keeps both call sites in agreement should either ever
    change; the recursive bisect-and-retry logic itself is duplicated
    (not imported) since the original is a bound method closed over an
    admin-manager instance this file has no reason to construct just to
    borrow one method, and specialized to concatenating row lists rather
    than Influx3AdminManager._query_time_slices()'s generic per-slice
    result (a plain sum() or list-concatenation is all every caller of
    that one has ever needed, including this one).

    Starts with the whole range in one call; on a file-limit error it
    bisects the range and retries each half, recursing until every slice
    is accepted (or a slice is already as narrow as _V3_MIN_SLICE, in
    which case the server's own error is re-raised). Any other error is
    raised immediately. Slices are half-open except the last, so a row
    exactly on a boundary is never counted twice.
    """
    from ...transports.influxdb3_out import (  # noqa: PLC0415 -- local: see build_points_timescale's identical note on deferring a transport's imports
        _V3_MIN_SLICE,  # pyright: ignore[reportPrivateUsage] -- reused deliberately, see this function's docstring
        _is_file_limit_error,  # pyright: ignore[reportPrivateUsage]
    )

    rows: list[dict[str, Any]] = []

    def _go(slice_start: datetime, slice_end: datetime, *, end_inclusive: bool) -> None:
        try:
            rows.extend(run(slice_start, slice_end, end_inclusive))
        except Exception as exc:
            if not _is_file_limit_error(exc) or (slice_end - slice_start) <= _V3_MIN_SLICE:
                raise
            midpoint: datetime = slice_start + (slice_end - slice_start) / 2
            _log.info(
                f"[Timeshift] Export query over [{slice_start}, {slice_end}] exceeded InfluxDB's file limit -- splitting the range."
            )
            _go(slice_start, midpoint, end_inclusive=False)
            _go(midpoint, slice_end, end_inclusive=end_inclusive)
            return

    _go(start_time, end_time, end_inclusive=True)
    return rows


def export_range_rows(
    gateway: "Protocol_Gateway | None",
    version: str,
    measurement: str,
    device_identifier: str | None,
    start_time: datetime,
    end_time: datetime,
    target_start: datetime,
    tag_keys: set[str],
    ) -> tuple[list[str], list[dict[str, Any]]]:
    """
    Queries every field for `measurement` (optionally filtered to one
    device_identifier) between start_time/end_time, applies the same
    time-shift delta InfluxDateConverter.export_influx_data_to_csv() does,
    and returns (header, rows) ready for CSV writing. Tag columns
    (`tag_keys`) are dropped from the output, same as IGNORE_TAGS there --
    on re-import, tags are re-supplied as fixed values via the Tags panel,
    not read back from the file.

    Raises:
        ValueError: end_time before start_time, or no bridge attached.
    """
    if end_time < start_time:
        raise ValueError("End time must not be before start time.")

    time_delta: timedelta = compute_time_delta(start_time, target_start)
    rows: list[dict[str, Any]] = []
    bridge: influxdb_out | influxdb3_out | None = None
    if version == "1":
        bridge = get_influxdb1_bridge(gateway)
        if bridge is None or bridge.client is None:
            raise ValueError("No connected InfluxDB v1 bridge is attached to this gateway.")

        from influxdb.client import quote_ident  # type: ignore[import-untyped]
        from influxdb.resultset import ResultSet  # type: ignore[import-untyped]
            #-- real (runtime) import for the isinstance() check below;
            # safe here (unlike a module-level import) because this line only runs once
            # get_influxdb1_bridge() has already confirmed a v1 bridge exists,
            # which means the `influxdb` package is necessarily already installed and importable
            # -- same reasoning as write_points_v1()'s own local `from influxdb.exceptions import InfluxDBClientError`.


        quoted_measurement: str = quote_ident(measurement)
        where: str = (
            f"time >= {_influxql_quote_literal(start_time.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ'))} "
            f"AND time <= {_influxql_quote_literal(end_time.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ'))}"
        )
        if device_identifier:
            where += f" AND device_identifier = {_influxql_quote_literal(device_identifier)}"
        query: str = f"SELECT * FROM {quoted_measurement} WHERE {where}"  # noqa: S608
        # InfluxDBClient.query() can return a single ResultSet, a
        # list[ResultSet] (only if the query string held multiple
        # ';'-separated statements), or a Generator[ResultSet, Any, None]
        # (only when chunked=True, which we never pass) -- all three are
        # part of its real signature, even though `query` above is always
        # exactly one SELECT with no chunking, so only the plain-ResultSet
        # case should ever actually occur here. The other two are still
        # checked explicitly rather than assumed away, since calling
        # .get_points() on a list or a bare generator would otherwise fail
        # with a confusing AttributeError instead of this clear error.
        query_result: Generator[ResultSet, Any, None] | ResultSet | list[ResultSet] = bridge.client.query(query, database=bridge.database, epoch="ns")  # type: ignore[reportUnknownMemberType]
        if not isinstance(query_result, ResultSet):
            msg: str = (f"Expected a single, non-chunked result set for measurement '{measurement}', "
                f"got {type(query_result).__name__}.")
            _log.error(msg)
            raise ValueError(msg)
        points: list[dict[str, Any]] = cast(list[dict[str, Any]], list(query_result.get_points()))  # type: ignore[reportUnknownMemberType]  -- influxdb client's ResultSet.get_points() has no precise element type in its stubs

        for point in points:
            time_ns: Any = point.get("time")
            ts = pd.Timestamp(int(time_ns), unit="ns", tz="UTC")
            new_time: pd.Timestamp = (ts + time_delta).tz_localize(None)
            new_row: dict[str, Any] = {"time": new_time.isoformat()}
            for k, v in point.items():
                if k in tag_keys or k in ("time",):
                    continue
                new_row[k] = v
            rows.append(new_row)

    else:
        bridge = get_influxdb3_bridge(gateway)
        if bridge is None or bridge.client is None:
            raise ValueError("No connected InfluxDB v3 bridge is attached to this gateway.")

        v3_bridge: Any = bridge  # narrowed non-None/non-.client-None just above; re-bound so the closure below doesn't need to re-prove that on every call
        quoted_measurement: str = _sql_quote_ident(measurement)

        # A wide range can exceed InfluxDB 3 CORE's per-query Parquet file limit
        # (Enterprise has none), so the query runs per time slice via
        # _query_v3_time_slices -- one slice, i.e. one query, whenever the whole
        # range is accepted as-is -- and every slice's rows are concatenated.
        def _fetch_v3_slice(slice_start: datetime, slice_end: datetime, end_inclusive: bool) -> list[dict[str, Any]]:
            end_op: str = "<=" if end_inclusive else "<"
            slice_start_lit: str = slice_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            slice_end_lit: str = slice_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            slice_where: str = f"time >= {_sql_quote_literal(slice_start_lit)} AND time {end_op} {_sql_quote_literal(slice_end_lit)}"
            if device_identifier:
                slice_where += f" AND device_identifier = {_sql_quote_literal(device_identifier)}"
            slice_sql: str = f"SELECT * FROM {quoted_measurement} WHERE {slice_where}"  # noqa: S608
            # InfluxDBClient3.query(..., mode="all") (the default we rely on by
            # not passing `mode=`) returns a pyarrow.Table at runtime --
            # undocumented in its own (untyped) signature, but true of the
            # installed client per its source; `.to_pylist()` below is a real
            # pyarrow.Table method, not a dynamic/guessed one. It's typed as
            # Any rather than pyarrow.Table itself because pyarrow ships no
            # py.typed marker and its Table class is a Cython/C-extension type
            # pyright can't introspect -- annotating it "Table" wouldn't add
            # real checking, only the appearance of it (confirmed: pyright
            # resolves pyarrow.Table itself to Unknown even when imported).
            slice_table: Any = v3_bridge.client.query(slice_sql, database=v3_bridge.database, language="sql")  # type: ignore[reportUnknownMemberType]
            return [cast(dict[str, Any], r) for r in slice_table.to_pylist()]  # type: ignore[reportUnknownMemberType]  -- pyarrow.Table ships no py.typed marker, so pyright can't resolve its methods even through an Any-typed reference

        for record_map in _query_v3_time_slices(_fetch_v3_slice, start_time, end_time):
            time_value: Any = record_map.get("time")
            if time_value is None:
                continue  # a record with no time value can't be placed on the timeline -- skip it
            ts = pd.Timestamp(time_value)
            if ts.tzinfo is None:
                ts: pd.Timestamp = ts.tz_localize("UTC")
            new_time = (ts.tz_convert("UTC") + time_delta).tz_localize(None)
            new_row = {"time": new_time.isoformat()}
            for k, v in record_map.items():
                if k in tag_keys or k == "time":
                    continue
                new_row[k] = v
            rows.append(new_row)

    header: list[str] = ["time"]
    seen: set[str] = {"time"}
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                header.append(k)

    return header, rows


# ---------------------------------------------------------------------------
# TimescaleDB -- a structurally different destination from InfluxDB v1/v3.
# There is no free-typed measurement: the admin picks a TABLE (the shared
# narrow table, or one protocol's wide table -- table listing/resolution is
# entirely reused from services/bridge_service.py's Metrics Edit functions,
# same as this file already reuses influxdb_service's for InfluxDB) and a
# DEVICE (also reused from Metrics Edit) in place of InfluxDB's Tags panel.
#
# Writing to a wide table always mirrors the same field values into the
# narrow table too (build_points_timescale/write_points_timescale below) --
# the narrow table is the durable long-format record of everything a wide
# table also holds, and every wide column's underlying metric_name is
# exactly its own column_name (see transports.timescaledb._process_raw_
# metrics, whose `clean_key` feeds both), so "the same data" is a direct,
# unambiguous mirror with no separate name mapping to maintain.
# ---------------------------------------------------------------------------

def timescale_tables_for(gateway: "Protocol_Gateway | None") -> list[dict[str, str | None]]:
    """
    Thin re-export of bridge_service.list_metric_edit_tables(), named for
    this module's own "Table" picker on the Timeshift Data screen --
    [{table_kind, protocol_name, table_name}, ...], the shared narrow table
    first, then every protocol that has a wide table. A protocol with no
    wide table (narrow-only, e.g. it exceeds transports.timescaledb.
    timescaledb.WIDE_TABLE_COLUMN_LIMIT) simply has no entry of its own
    here -- its data still lives in the one shared narrow entry -- which is
    exactly what keeps a wide-table CHOICE off the picker unless a wide
    table genuinely exists, with no extra filtering needed on this end.
    """
    return _list_timescale_tables(gateway)


def timescale_devices_for(
    gateway: "Protocol_Gateway | None", table_kind: str, protocol_name: str | None = None,
    ) -> list[dict[str, str | int | None]]:
    """
    Thin re-export of bridge_service.list_metric_edit_devices() -- the
    Device picker that replaces InfluxDB's Tags panel for a TimescaleDB
    destination, scoped to whichever table is currently selected exactly
    the way the Metrics Edit screen's own device picker is.

    Raises:
        ValueError: unknown table_kind, or table_kind="wide" with an
                    unregistered/narrow-only protocol_name.
    """
    return _list_timescale_devices(gateway, table_kind, protocol_name)


def timescale_fields_for(
    gateway: "Protocol_Gateway | None",
    table_kind: str,
    protocol_name: str | None = None,
    device_info_id: int | None = None,
    ) -> list[dict[str, str | None]]:
    """
    Thin re-export of bridge_service.list_metric_edit_fields() -- existing
    column names (wide) or distinct metric_name values (narrow, optionally
    scoped to one device) for the selected table, feeding the Field Matchup
    step's target-field list the same way influx_fields does for InfluxDB.

    Raises:
        ValueError: see timescale_devices_for.
    """
    return _list_timescale_fields(gateway, table_kind, protocol_name=protocol_name, device_info_id=device_info_id)


def load_timescale_field_types(gateway: "Protocol_Gateway | None", table_kind: str, protocol_name: str | None) -> dict[str, str]:
    """
    [column_name] -> declared Postgres type string (e.g. "DOUBLE
    PRECISION", "SMALLINT", "TEXT") for a wide table's existing columns,
    for build_points_timescale's value coercion
    (transports.timescaledb._coerce_value_for_data_type). Always {} for a
    narrow table -- there is no per-metric declared type there (every
    metric shares the same two nullable columns), so no coercion is needed
    or possible; build_points_timescale falls back to write_points_
    timescale's own isinstance-based numeric/text split for those, the same
    decision transports.timescaledb._flush_batch_narrow makes for live data.
    """
    if table_kind != "wide":
        return {}
    fields: list[dict[str, str | None]] = timescale_fields_for(gateway, "wide", protocol_name)
    return {name: dtype for f in fields if (name := f.get("name")) and (dtype := f.get("data_type"))}


@dataclass
class TimescalePointDict:
    """
    One source row's contribution, ready for write_points_timescale().
    Unlike InfluxPointDict there is no per-point measurement/tags -- every
    point in one import shares the same table_kind/table_name/protocol_name
    and device_info_id (a Timeshift import always targets one table and one
    device), so those are write_points_timescale() parameters instead of
    being repeated on every point.

    `fields` keys are column_name (wide) or metric_name (narrow) -- see the
    module-section docstring above for why those are the same string
    either way. Wide-table values have already been coerced to match each
    column's declared Postgres type (transports.timescaledb._coerce_value_
    for_data_type); narrow-table values have not, since narrow has no
    declared per-metric type to coerce against.
    """
    time_iso: str
    fields: dict[str, int | float | str | bool]


def build_points_timescale(
    df: pd.DataFrame,
    *,
    table_kind: str,
    mapping: dict[str, str],           # source_column -> column_name (wide) or metric_name (narrow); only mapped, non-ignored columns
    time_column: str,
    source_is_local: bool,
    local_tz: str,
    time_delta: timedelta,
    wide_field_types: dict[str, str],  # column_name -> declared Postgres type; {} for a narrow target (see load_timescale_field_types)
    ) -> tuple[list[TimescalePointDict], TimeshiftImportResult]:
    """
    TimescaleDB counterpart of build_points() -- same row-by-row time-shift
    math (identical to the letter, since it doesn't depend on the
    destination), but coercing each mapped value against a wide column's
    declared Postgres type instead of InfluxDB's own field-type bucket, and
    with no tags (the whole point set shares one device_info_id, attached
    by write_points_timescale() rather than per-point here).

    A wide-table value that fails transports.timescaledb._coerce_value_for_
    data_type (wrong shape for its column, e.g. text into a numeric column,
    a fractional value into an INTEGER column, or out of an integer type's
    range) is recorded in result.type_mismatches and dropped from that row,
    the same "skip and report, don't fail the whole import" behavior
    check_field_type() gives build_points() for InfluxDB. A narrow-table
    value is never rejected this way -- there is no declared type to
    conflict with; normalize_value() is all it gets, and write_points_
    timescale() sorts it into metric_value/metric_ascii by its Python type
    at write time, exactly as transports.timescaledb._flush_batch_narrow
    does for live data.
    """
    from ...transports.timescaledb import (  # noqa: PLC0415 -- local: see write_points_timescale's note on deferring this transport's imports
        _coerce_value_for_data_type,  # pyright: ignore[reportPrivateUsage] -- reused deliberately, see this function's own docstring
    )

    result = TimeshiftImportResult()
    points: list[TimescalePointDict] = []
    tz = ZoneInfo(local_tz)

    for row in df.to_dict("records"):
        time_val: Any = row.get(time_column)
        if time_val is None or (isinstance(time_val, float) and math.isnan(time_val)):
            result.rows_skipped_bad_time += 1
            continue

        try:
            ts: pd.Timestamp = pd.to_datetime(time_val)
            if ts.tzinfo is None:
                ts = ts.tz_localize(tz, nonexistent="shift_forward") if source_is_local else ts.tz_localize("UTC")
            new_time: pd.Timestamp = (ts.tz_convert("UTC") + time_delta).tz_localize(None)
        except (ValueError, TypeError):
            result.rows_skipped_bad_time += 1
            continue

        if pd.isna(new_time):
            result.rows_skipped_bad_time += 1
            continue

        fields: dict[str, int | float | str | bool] = {}
        for col, raw_val in row.items():
            col = str(col)
            if col == time_column:
                continue
            target: str | None = mapping.get(col)
            if not target:
                if col in mapping:
                    continue  # admin explicitly mapped this column to "" (Ignore checked) -- silently skip, no warning
                result.unmapped_columns.add(col)
                continue

            val: Any = normalize_value(raw_val)
            if val is None:
                continue

            if table_kind == "wide":
                try:
                    val = _coerce_value_for_data_type(val, wide_field_types.get(target))
                except ValueError as exc:
                    result.type_mismatches.append(f"{target}={val!r} (row time {new_time.isoformat()}): {exc}")
                    continue
            fields[target] = val

        if not fields:
            result.rows_skipped_no_fields += 1
            continue

        points.append(TimescalePointDict(time_iso=new_time.isoformat(), fields=fields))

    return points, result


# device_metrics_narrow's PK is (m_time, device_info_id, metric_name) -- see transports.timescaledb.
# DeviceMetricsNarrow. ON CONFLICT DO UPDATE (not DO NOTHING, unlike the live flush worker's own narrow
# write) so re-running a Timeshift import over the same target range is idempotent/overwriting, the
# same expectation InfluxDB's line-protocol writes already give this screen's other two destinations.
_NARROW_UPSERT_SQL: str = (
    "INSERT INTO device_metrics_narrow (m_time, device_info_id, metric_name, metric_value, metric_ascii) "
    "VALUES (:m_time, :device_info_id, :metric_name, :metric_value, :metric_ascii) "
    "ON CONFLICT (m_time, device_info_id, metric_name) DO UPDATE SET "
    "metric_value = EXCLUDED.metric_value, metric_ascii = EXCLUDED.metric_ascii"
)


def _narrow_value_pair(value: int | float | str | bool) -> tuple[float, str | None]:
    """Mirrors transports.timescaledb._flush_batch_narrow's numeric-vs-ascii split, for the narrow mirror of a wide-table write (and for a narrow-only write)."""
    if isinstance(value, bool):
        return (1.0 if value else 0.0), None
    if isinstance(value, (int, float)):
        return float(value), None
    return 0.0, str(value)


def _wide_upsert_sql(table_name: str, columns: list[str]) -> str:
    """
    ON CONFLICT (m_time, device_info_id) DO UPDATE -- that pair is the wide
    table's own primary key (see transports.timescaledb.timescaledb.
    _ensure_wide_table_exists), so this is the same idempotent-overwrite
    reasoning _NARROW_UPSERT_SQL's docstring gives, for the wide side of a
    dual write. `table_name`/`columns` are trusted SQL identifiers by the
    time they reach here -- table_name from bridge_service.
    resolve_wide_table_name() (itself sourced from protocol_registry, only
    ever a name this app created), columns from metric_catalog.
    clean_column_name (sanitized at wide-table-creation time) -- the same
    trust level transports.timescaledb.BridgeAdminManager._wide_view_names
    already extends to a wide_table_name it's given.
    """
    col_list: str = ", ".join(["m_time", "device_info_id", *columns])
    val_list: str = ", ".join([":m_time", ":device_info_id", *(f":{c}" for c in columns)])
    update_list: str = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    return (
        f"INSERT INTO {table_name} ({col_list}) VALUES ({val_list}) "  # noqa: S608
        f"ON CONFLICT (m_time, device_info_id) DO UPDATE SET {update_list}"
    )


def write_points_timescale(
    gateway: "Protocol_Gateway | None",
    table_kind: str,
    table_name: str,
    device_info_id: int,
    points: list[TimescalePointDict],
    ) -> tuple[int, int]:
    """
    Writes points to the live TimescaleDB bridge -- for table_kind="wide",
    each point's fields are upserted into `table_name` AND mirrored into
    device_metrics_narrow with the same field/value pairs (see this
    section's module docstring); for table_kind="narrow", only the narrow
    write happens.

    Returns (rows_written, narrow_metric_rows_written):
      - rows_written counts one per point (the same "one point per source
        row" convention TimeshiftImportResult.points_written already uses
        for InfluxDB), regardless of table_kind.
      - narrow_metric_rows_written counts individual (m_time, device_info_
        id, metric_name) rows placed into device_metrics_narrow -- always
        >= rows_written whenever any point has more than one field, since
        every field of every point becomes its own narrow row.

    Both writes for one point share a single transaction (a wide row and
    its narrow mirror are never left half-written), and the whole batch
    shares one more (all points, or none, per the same all-or-nothing
    expectation write_points_v1/write_points_v3 give InfluxDB imports).

    Raises:
        RuntimeError: no TimescaleDB bridge is attached to this gateway, or it isn't connected.
        ValueError: table_kind isn't "narrow"/"wide".
    """
    from sqlalchemy import (  # noqa: PLC0415 -- local: see build_points_timescale's identical note on deferring this transport's imports
        text as _sql_text,
    )

    if table_kind not in ("narrow", "wide"):
        msg: str = f"Unknown table_kind '{table_kind}' -- expected 'narrow' or 'wide'."
        raise ValueError(msg)

    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if not getattr(bridge, "tsdb_connected", True):
        raise RuntimeError("TimescaleDB bridge is not connected.")
    if not points:
        return 0, 0

    narrow_rows: list[dict[str, Any]] = []
    rows_written = 0

    with bridge.SessionFactory() as session:
        with session.begin():
            for p in points:
                # p.time_iso is a naive-UTC ISO string (see TimescalePointDict/build_points_timescale,
                # and InfluxPointDict's identical convention). Explicitly re-attaching UTC here, rather
                # than binding the naive string/datetime directly, matters because these columns are
                # TIMESTAMPTZ: a naive value would otherwise be interpreted in the DB session's own
                # timezone setting (not necessarily UTC), silently shifting every written timestamp.
                m_time: datetime = datetime.fromisoformat(p.time_iso).replace(tzinfo=timezone.utc)
                if table_kind == "wide":
                    sql: str = _wide_upsert_sql(table_name, list(p.fields.keys()))
                    params: dict[str, Any] = {"m_time": m_time, "device_info_id": device_info_id, **p.fields}
                    session.execute(_sql_text(sql), params)
                for name, value in p.fields.items():
                    metric_value, metric_ascii = _narrow_value_pair(value)
                    narrow_rows.append({
                        "m_time": m_time, "device_info_id": device_info_id,
                        "metric_name": name, "metric_value": metric_value, "metric_ascii": metric_ascii,
                    })
                rows_written += 1

            if narrow_rows:
                session.execute(_sql_text(_NARROW_UPSERT_SQL), narrow_rows)

    return rows_written, len(narrow_rows)


def export_range_rows_timescale(
    gateway: "Protocol_Gateway | None",
    table_kind: str,
    table_name: str,
    protocol_name: str | None,
    device_info_id: int,
    start_time: datetime,
    end_time: datetime,
    target_start: datetime,
    ) -> tuple[list[str], list[dict[str, Any]]]:
    """
    TimescaleDB counterpart of export_range_rows() -- same time-shift math
    and (header, rows) shape, for one table + device over a date range.

    A wide table is already column-shaped, so its rows come back as-is
    (minus m_time, replaced by the shifted "time"). A narrow table stores
    one row per (m_time, metric_name) -- these are pivoted into the same
    wide-shaped CSV rows (one row per distinct shifted timestamp, one
    column per distinct metric_name seen in the range) so a narrow export
    and a wide export produce an identically-shaped file, and so the file
    re-imports the same way through this same screen's "influx_csv"-style
    source path regardless of which table it came from.

    Raises:
        ValueError: end_time before start_time, no bridge attached/
                    connected, or table_kind isn't "narrow"/"wide".
    """
    if end_time < start_time:
        raise ValueError("End time must not be before start time.")
    if table_kind not in ("narrow", "wide"):
        msg: str = f"Unknown table_kind '{table_kind}' -- expected 'narrow' or 'wide'."
        raise ValueError(msg)

    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None or not getattr(bridge, "tsdb_connected", True):
        raise ValueError("No connected TimescaleDB bridge is attached to this gateway.")

    from sqlalchemy import (  # noqa: PLC0415 -- local: see build_points_timescale's identical note on deferring this transport's imports
        text as _sql_text,
    )

    time_delta: timedelta = compute_time_delta(start_time, target_start)
    rows: list[dict[str, Any]] = []

    def shift(raw_time: Any) -> str:
        ts = pd.Timestamp(raw_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return (ts.tz_convert("UTC") + time_delta).tz_localize(None).isoformat()

    with bridge.SessionFactory() as session:
        if table_kind == "narrow":
            result: Any = session.execute(
                _sql_text(
                    "SELECT m_time, metric_name, metric_value, metric_ascii FROM device_metrics_narrow "
                    "WHERE device_info_id = :did AND m_time BETWEEN :start AND :end ORDER BY m_time"
                ),
                {"did": device_info_id, "start": start_time, "end": end_time},
            )
            by_time: dict[str, dict[str, Any]] = {}
            for m_time, metric_name, metric_value, metric_ascii in result:
                new_row: dict[str, Any] = by_time.setdefault(shift(m_time), {})
                new_row[metric_name] = metric_ascii if metric_ascii is not None else metric_value
            rows = [{"time": t, **fields} for t, fields in by_time.items()]
        else:
            field_names: list[str] = [name for f in timescale_fields_for(gateway, "wide", protocol_name) if (name := f.get("name"))]
            if field_names:
                select_cols: str = ", ".join(_sql_quote_ident(c) for c in field_names)
                result = session.execute(
                    _sql_text(
                        f"SELECT m_time, {select_cols} FROM {table_name} "  # noqa: S608 -- table_name/select_cols are trusted identifiers, see _wide_upsert_sql's docstring
                        "WHERE device_info_id = :did AND m_time BETWEEN :start AND :end ORDER BY m_time"
                    ),
                    {"did": device_info_id, "start": start_time, "end": end_time},
                )
                for record in result:
                    new_row = {"time": shift(record[0])}
                    for name, value in zip(field_names, record[1:]):
                        new_row[name] = value
                    rows.append(new_row)

    header: list[str] = ["time"]
    seen: set[str] = {"time"}
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                header.append(k)

    return header, rows
