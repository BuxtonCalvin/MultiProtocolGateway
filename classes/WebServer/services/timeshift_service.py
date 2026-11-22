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

import csv
import io
import json
import logging
import math
import queue
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Final,
    Generator,
    Iterable,
    Iterator,
    Literal,
    cast,
)
from zoneinfo import ZoneInfo, available_timezones

import pandas as pd
from fastapi import HTTPException
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

    from protocol_gateway import Protocol_Gateway

    from ...transports.influxdb3_out import influxdb3_out
    from ...transports.influxdb_out import influxdb_out

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress stream -- the transport behind the Timeshift Data thermometers
#
# Follows the convention PATCH /api/timescale/compression/rebuild already set for the Rebuild Compression thermometer:
# one long-lived response of newline-delimited JSON (media type application/x-ndjson), read incrementally by the
# browser, with no job ID and no polling endpoint. Every line is one event:
#
#     {"type": "progress", "label": str, "done": int, "total": int}
#         `done`/`total` size the bar; `total` of 0 means "working, but nothing countable yet" (the bar stays empty and
#         only the label and the elapsed time move). `label` is the complete text shown beside the bar.
#     {"type": "done", ...}
#         Exactly one, last, carrying whatever the job returned (merged into the event).
#     {"type": "error", "detail": str}
#         Exactly one, last, in place of "done". Errors raised before the first event never reach stream_job: the
#         endpoints validate everything they can synchronously so those still come back as ordinary HTTP errors.
#
# The job body runs on its own thread (it is blocking database and file work), and stream_job() relays the events from
# a queue, so the web server's event loop is never held by a Timeshift job.
#
# Cancellation: an export abandons its work when the browser disconnects (nobody is left to receive the file), so it
# asks for cancel_on_disconnect. An import must NOT: InfluxDB writes are not transactional, so stopping one half-way
# would leave a partial import, exactly what the old single blocking request never did.
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[str, int, int], None]
"""(label, done, total) — see this module's docstring for what each means."""

JobResult = dict[str, object]

# Progress events are throttled so a fast writer cannot flood the connection; the final event of a phase always gets through.
_MIN_EVENT_INTERVAL_S: Final[float] = 0.1


class JobCancelledError(Exception):
    """Raised inside a job (by ProgressReporter.raise_if_cancelled) once its browser has gone away."""


class ProgressReporter:
    """What a job body is handed: report progress, and (for cancellable jobs) check whether to stop."""

    def __init__(self, events: queue.Queue[str]) -> None:
        self._events: queue.Queue[str] = events
        self._cancelled: threading.Event = threading.Event()
        self._last_sent_at: float = 0.0

    def report(self, label: str, done: int, total: int) -> None:
        """Queues a progress event, dropping it if the previous one went out under 0.1 s ago (unless the phase is complete)."""
        now: float = time.monotonic()
        finished: bool = total > 0 and done >= total
        if not finished and now - self._last_sent_at < _MIN_EVENT_INTERVAL_S:
            return
        self._last_sent_at = now
        self._events.put(json.dumps({"type": "progress", "label": label, "done": max(done, 0), "total": max(total, 0)}) + "\n")

    def cancel(self) -> None:
        self._cancelled.set()

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise JobCancelledError


def _error_line(detail: str) -> str:
    return json.dumps({"type": "error", "detail": detail}) + "\n"


def stream_job(
    job: Callable[[ProgressReporter], JobResult],
    *,
    name: str,
    cancel_on_disconnect: bool,
    error_prefix: str = "",
    ) -> Generator[str, None, None]:
    """
    Runs `job` on a worker thread and yields its NDJSON events as they happen, ending with one "done" or "error" line.

    A job signals a user-facing failure by raising HTTPException (its detail becomes the error text, the same wording the
    endpoint used to return as the HTTP detail); any other exception is logged and reported as `error_prefix` + its message.
    """
    events: queue.Queue[str] = queue.Queue()
    reporter: ProgressReporter = ProgressReporter(events)
    finished: threading.Event = threading.Event()

    def run() -> None:
        final: str
        try:
            result: JobResult = job(reporter)
            final = json.dumps({"type": "done", **result}) + "\n"
        except JobCancelledError:
            _log.info(f"[Timeshift] {name} cancelled: the browser disconnected.")
            final = _error_line("Cancelled.")
        except HTTPException as exc:
            final = _error_line(str(exc.detail))
        except Exception as exc:
            _log.exception(f"[Timeshift] {name} failed")
            final = _error_line(f"{error_prefix}{exc}")
        finally:
            finished.set()
        events.put(final)

    worker: threading.Thread = threading.Thread(target=run, name=f"timeshift-{name}", daemon=True)
    worker.start()

    try:
        while True:
            line: str = events.get()
            yield line
            if finished.is_set() and events.empty():
                return
    finally:
        # The generator is closed early when the browser disconnects mid-stream.
        if cancel_on_disconnect and not finished.is_set():
            reporter.cancel()


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


def rank_mappings(csv_field: str, influx_candidates: set[str] | list[str]) -> list[tuple[str, float]]:
    """
    EVERY destination field that scores at or above the fuzzy cutoff for `csv_field`, best first, as
    (name, confidence). Fully deterministic: candidates are walked in sorted order and equal scores are ordered
    by name, so the same file and schema always give the same answer. (The previous best-only loop walked a
    `set` in hash order and kept the first of any tie, so ties -- e.g. tradiator3 against tradiator1/tradiator2 --
    resolved differently from one run to the next.)
    """
    normalized_csv: str = normalize_metric_name(csv_field)
    normalized_map: dict[str, str] = {}
    for name in sorted(influx_candidates):
        normalized_map.setdefault(normalize_metric_name(name), name)   # two names that normalize alike: the first (sorted) wins

    ranked: list[tuple[str, float]] = []
    matcher: SequenceMatcher[str] = SequenceMatcher(None, b=normalized_csv)
    tokens_csv: set[str] = set(normalized_csv.split("_"))

    for normalized_candidate, original in normalized_map.items():
        matcher.set_seq1(normalized_candidate)
        if matcher.quick_ratio() >= _FUZZY_CUTOFF:
            score: float = matcher.ratio() + 0.1 * len(tokens_csv & set(normalized_candidate.split("_")))
            if score >= _FUZZY_CUTOFF:
                ranked.append((original, round(score, 3)))

    ranked.sort(key=lambda m: (-m[1], m[0] != csv_field, m[0]))
    return ranked


def guess_mapping(csv_field: str, influx_candidates: set[str]) -> tuple[str, float]:
    """
    Finds the best InfluxDB field-name match and returns (name, confidence).
    Returns ("", 0.0) if no match meets the cutoff.
    """
    ranked: list[tuple[str, float]] = rank_mappings(csv_field, influx_candidates)
    return ranked[0] if ranked else ("", 0.0)


def find_duplicate_targets(mapping: dict[str, str]) -> dict[str, list[str]]:
    """
    {destination field: [source columns...]} for every destination that MORE THAN ONE source column maps to
    (blank targets = ignored columns are skipped). Two columns into one field would silently overwrite each other
    within each row -- the last one wins -- so Preview flags this and Import refuses it.
    """
    by_target: dict[str, list[str]] = {}
    for source, target in mapping.items():
        if target.strip():
            by_target.setdefault(target, []).append(source)
    return {target: sources for target, sources in by_target.items() if len(sources) > 1}


def duplicate_targets_message(duplicates: dict[str, list[str]]) -> str:
    """User-facing explanation for find_duplicate_targets()'s result (both Preview and Import show it verbatim)."""
    lines: str = "; ".join(f'"{target}" <- {", ".join(sources)}' for target, sources in sorted(duplicates.items()))
    return (
        f"More than one source column is matched to the same destination field: {lines}. "
        "Their values would overwrite each other row by row, so only one of them would be kept. "
        "In Field Matchup, give each destination field to a single source column and tick Ignore on (or pick a different field for) the others."
    )


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
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> list[FieldMappingRow]:
    """
    Builds the Field Matchup table rows the GUI renders for the admin to
    confirm/adjust -- the interactive replacement for InfluxDateConverter.
    py's perform_schema_validation()/mapping_needed.csv hand-edit cycle.

    Both source kinds ("eg4", and "influx_csv" -- a file this same screen previously exported) are matched the same
    way: each column is fuzzy-matched against `influx_fields` via rank_mappings(); a match scoring at or above
    `confidence_threshold` is pre-filled (an exact name match scores 100%). A column that does NOT reach the
    threshold starts with "Ignore" pre-checked -- an EG4 export carries many columns MPG never records, and
    silently creating a new InfluxDB field for each of them is rarely what the admin wants; a re-imported CSV
    that was edited by hand can likewise carry columns the destination has never had. The row is still shown, so
    the admin can un-check Ignore and either pick an existing field or "+ New field...".

    Exception: when `influx_fields` is empty (a brand-new measurement with
    no schema yet) there is nothing for a column to match, so nothing is
    pre-ignored on that basis -- otherwise every column would start
    ignored and the whole file would be dropped by default.

    No destination field is ever suggested for more than one column (see the one-to-one assignment below), and the
    result is deterministic. The Preview and Import endpoints also refuse a hand-made duplicate (find_duplicate_targets).

    Columns in `tag_keys` (whatever the admin typed into the Tags panel)
    or RESERVED_COLUMN_NAMES are pre-checked "Ignore this column" by
    default, but still shown -- the admin can un-ignore and map them as an
    ordinary field if that's genuinely what they want.
    """
    influx_field_set: set[str] = set(influx_fields)
    rows: list[FieldMappingRow] = []

    # Assign destination fields to columns as a one-to-one matching, best score first (ties: column order, then
    # name), so no two columns are ever pre-filled with the same field. A column whose best match was claimed by a
    # better-scoring column falls back to its next-best free field if that still clears the threshold, else is left
    # unmatched. Without this, ePv1All/ePv2All/ePv3All would all be suggested "epv1_all".
    assigned: dict[str, tuple[str, float]] = {}
    claims: list[tuple[float, int, str]] = []
    for index, col in enumerate(source_columns):
        if col in tag_keys or col.strip().lower() in RESERVED_COLUMN_NAMES:
            continue
        claims.extend((score, index, name) for name, score in rank_mappings(col, influx_field_set) if score >= confidence_threshold)
    claims.sort(key=lambda c: (-c[0], c[1], c[2]))
    taken: set[str] = set()
    for score, index, name in claims:
        col = source_columns[index]
        if col not in assigned and name not in taken:
            assigned[col] = (name, score)
            taken.add(name)

    for col in source_columns:
        default_ignored: bool = col in tag_keys or col.strip().lower() in RESERVED_COLUMN_NAMES

        if default_ignored:
            rows.append(FieldMappingRow(col, "", 0.0, True))  # noqa: FBT003
            continue

        if col in assigned:
            guess, score = assigned[col]
            rows.append(FieldMappingRow(col, guess, score, False))  # noqa: FBT003
        else:
            # Unmatched column (or its match went to a better-scoring column) -> Ignore pre-checked (see docstring),
            # unless there is no schema to match against.
            rows.append(FieldMappingRow(col, "", 0.0, bool(influx_field_set)))

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


def earliest_timestamp(df: pd.DataFrame, time_column: str, local_tz: str, source_is_local: bool = True) -> str | None:
    """
    The earliest valid timestamp in `time_column`, formatted for an
    <input type="datetime-local" step="1"> (YYYY-MM-DDTHH:MM:SS), or None if
    the column is missing or holds no parsable time. Drives the EG4 import's
    "Source Start" default. Naive values (an EG4 export is naive local time)
    are returned as-is; tz-aware ones are converted to `local_tz` first so
    the result is always in the same zone the page interprets it in.

    source_is_local=False is a re-imported export CSV: its naive times are UTC
    (build_points reads them that way), so they are converted to `local_tz`
    too -- otherwise Source Start would show the file's UTC clock reading in a
    box the page reads as local time, and any Target Start typed against it
    would land the data offset by the UTC difference.
    """
    if time_column not in df.columns:
        return None

    # ADDED format="mixed" to eliminate the UserWarning and handle varied string variations
    ts: pd.Series[pd.Timestamp] = pd.to_datetime(df[time_column], format="mixed", errors="coerce").dropna()

    if ts.empty:
        return None
    if ts.dt.tz is None and not source_is_local:
        ts = ts.dt.tz_localize("UTC")
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
class TypeMismatchStat:
    """
    Per-destination-field roll-up of every value dropped for a type conflict
    -- what the Preview/Result panels and the log show instead of one
    anonymous total, so an admin can see WHICH column was rejected, against
    what declared type, and why.
    """
    count: int = 0
    declared_type: str | None = None   # wide column's Postgres type / the InfluxDB field's type bucket; None = unknown
    example_value: str = ""            # first rejected value, repr()'d
    example_time: str = ""             # that value's (time-shifted) row time
    reason: str = ""                   # the first rejection's own reason text


@dataclass
class TimeshiftImportResult:
    points_written: int = 0
    rows_skipped_no_fields: int = 0
    rows_skipped_bad_time: int = 0
    unmapped_columns: set[str] = field(default_factory=set[str])
    type_mismatches: list[str] = field(default_factory=list[str])
    type_mismatch_summary: dict[str, TypeMismatchStat] = field(default_factory=dict[str, TypeMismatchStat])
    warnings: list[str] = field(default_factory=list[str])
    suggest_rounding: bool = False                                  # TimescaleDB: a fractional value hit a whole-number column -- the panels hint at the rounding checkbox
    rounded_counts: dict[str, int] = field(default_factory=dict[str, int])   # TimescaleDB: target column -> values rounded to a whole number (round_to_integer)
    blocking_errors: list[str] = field(default_factory=list[str])   # Preview only: problems that will make the real Import refuse/fail (e.g. retention policy)


def record_type_mismatch(
    result: TimeshiftImportResult, target: str, value: Any, new_time: str, declared_type: str | None, reason: str,
    ) -> None:
    """Appends to result.type_mismatches (legacy flat list) AND rolls the same conflict into result.type_mismatch_summary[target]."""
    result.type_mismatches.append(f"{target}={value!r} (row time {new_time}): {reason}" if reason else f"{target}={value!r} (row time {new_time})")
    stat: TypeMismatchStat = result.type_mismatch_summary.setdefault(target, TypeMismatchStat(declared_type=declared_type))
    if stat.count == 0:
        stat.example_value, stat.example_time, stat.reason = repr(value), new_time, reason
    stat.count += 1


def round_half_up(value: Any) -> int | None:
    """
    `value` (int/float/numeric str) rounded to the nearest whole number, halves away from zero (175.5 -> 176),
    or None when it isn't a finite number at all (text like '[0]', NaN) -- the caller then leaves the value
    alone so the normal type-conflict path reports it.
    """
    if isinstance(value, bool):
        return None
    try:
        dec: Decimal = Decimal(str(value).strip())
        return int(dec.quantize(Decimal(1), rounding=ROUND_HALF_UP)) if dec.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def log_type_mismatch_summary(result: TimeshiftImportResult, destination: str) -> None:
    """One WARNING per rejected field -- without this, a type-conflict skip left nothing in the log at all."""
    for target, stat in sorted(result.type_mismatch_summary.items(), key=lambda kv: -kv[1].count):
        _log.warning(
            f"[Timeshift] {destination}: skipped {stat.count} value(s) for '{target}' (declared type "
            f"{stat.declared_type or 'unknown'}); first was {stat.example_value} at {stat.example_time}: {stat.reason or 'type conflict'}"
        )


class RetentionPolicyConflictError(RuntimeError):
    """
    The import's (time-shifted) date range falls, wholly or partly, outside the
    InfluxDB retention policy the write lands in, so InfluxDB drops those
    points. str(exc) is the complete, user-facing explanation (what's wrong
    and the two ways out) -- routers/timeshift.py shows it verbatim.

    `may_have_partial_write` is True when raised from a failed write rather
    than the pre-flight check: InfluxDB v1 writes in batches of 1000, so
    batches before the offending one are already stored.
    """
    def __init__(self, message: str, *, may_have_partial_write: bool = False) -> None:
        super().__init__(message)
        self.may_have_partial_write: bool = may_have_partial_write


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
                expected_type: str | None = influx_field_types.get(target_field)
                record_type_mismatch(
                    result, target_field, val, new_time.isoformat(), expected_type,
                    f"{type(val).__name__} value doesn't match the existing field type '{expected_type}'.",
                )
                continue
            fields[target_field] = val

        if not fields:
            result.rows_skipped_no_fields += 1
            continue

        points.append(InfluxPointDict(measurement=measurement, time_iso=new_time.isoformat(), fields=fields, tags=tags))

    return points, result


# ---------------------------------------------------------------------------
# InfluxDB retention-policy guard
#
# InfluxDB v1 silently drops (and then reports as a 400 "partial write: points
# beyond retention policy dropped=N") any point older than now - the default
# retention policy's duration. A Timeshift import commonly lands on an old date
# range, so this is checked BEFORE anything is written (preview + import), and
# the same error is trapped at write time as a backstop.
# ---------------------------------------------------------------------------

_INFLUX_DURATION_PART: re.Pattern[str] = re.compile(r"(\d+)([hms])")


def parse_influx_duration(text: str | None) -> timedelta | None:
    """'168h0m0s' -> 7 days; '0s' / '' / unparsable -> None (InfluxDB's own \"keep forever\")."""
    if not text:
        return None
    seconds: int = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in _INFLUX_DURATION_PART.findall(text))
    return timedelta(seconds=seconds) if seconds > 0 else None


def _fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_span(delta: timedelta) -> str:
    days: int = delta.days
    hours: int = delta.seconds // 3600
    if days and hours:
        return f"{days} day(s) {hours} hour(s)"
    return f"{days} day(s)" if days else f"{max(hours, 1)} hour(s)"


def check_influx_retention(
    gateway: "Protocol_Gateway | None", version: str, points: list[InfluxPointDict],
    ) -> RetentionPolicyConflictError | None:
    """
    Pre-flight for an InfluxDB v1 import: returns a RetentionPolicyConflictError (NOT raised -- the caller decides
    whether to show it in Preview or refuse the Import) when any point is older than the database's default
    retention policy allows, else None.

    Best-effort and v1-only: v3 has no equivalent per-write RP lookup here, a v1 bridge that can't be queried
    returns None (the write-time trap in write_points_v1 is the backstop), and an infinite policy never conflicts.
    """
    if version != "1" or not points:
        return None
    bridge: influxdb_out | None = get_influxdb1_bridge(gateway)
    client: Any = bridge.client if bridge is not None else None
    if bridge is None or client is None:
        return None
    try:
        policies: list[dict[str, Any]] = client.get_list_retention_policies(bridge.database)
    except Exception as exc:
        _log.warning(f"[Timeshift] Could not read retention policies for '{bridge.database}' -- skipping the retention pre-check: {exc}")
        return None

    default: dict[str, Any] | None = next((p for p in policies if p.get("default")), None)
    keep: timedelta | None = parse_influx_duration(default.get("duration")) if default else None
    if default is None or keep is None:
        return None

    times: list[datetime] = [datetime.fromisoformat(p.time_iso) for p in points]   # naive UTC, see build_points
    cutoff: datetime = datetime.now(timezone.utc).replace(tzinfo=None) - keep
    outside: int = sum(1 for t in times if t < cutoff)
    if outside == 0:
        return None

    rp_name: str = str(default.get("name"))
    message: str = (
        f'InfluxDB retention policy "{rp_name}" on database "{bridge.database}" only keeps {_fmt_span(keep)} of data '
        f"(oldest accepted timestamp right now: {_fmt_utc(cutoff)}), but {outside} of {len(points)} points in this import are older than that "
        f"(the import spans {_fmt_utc(min(times))} to {_fmt_utc(max(times))}). InfluxDB would discard them. "
        f'To proceed, either extend the retention policy (e.g. ALTER RETENTION POLICY "{rp_name}" ON "{bridge.database}" DURATION <longer> or INF), '
        f"or change Target Start so the whole imported range is newer than {_fmt_utc(cutoff)}. Nothing has been written."
    )
    return RetentionPolicyConflictError(message)


def _is_retention_error(text: str) -> bool:
    lowered: str = text.lower()
    # "retention policy not found: autogen" is a different problem (the RP itself is missing) -- let that one surface as-is.
    return "retention" in lowered and "not found" not in lowered


# ---------------------------------------------------------------------------
# Dynamic timeouts for Timeshift export/import
#
# The influxdb_out/influxdb3_out bridges are built with connection_timeout (10 s
# by default), which is the right budget for the gateway's small live writes but
# far too short for a multi-month SELECT or a bulk import. Timeshift therefore
# opens its own short-lived client per operation, with a timeout scaled to the
# size of the job, and leaves the bridge's long-lived client untouched.
# ---------------------------------------------------------------------------

EXPORT_TIMEOUT_FLOOR_S: float = 30.0       # never less than this, however short the range
EXPORT_TIMEOUT_PER_DAY_S: float = 10.0     # extra seconds allowed per day of the queried range
EXPORT_TIMEOUT_CAP_S: float = 3600.0       # never wait longer than this for one query
V1_WRITE_TIMEOUT_S: float = 120.0          # per HTTP request; v1 writes go out in batches of 1000 points, each its own request
V3_WRITE_TIMEOUT_FLOOR_S: float = 30.0     # v3 sends the whole import as ONE write, so this one scales with the point count
V3_WRITE_TIMEOUT_PER_1K_POINTS_S: float = 2.0
V3_WRITE_TIMEOUT_CAP_S: float = 3600.0


def export_timeout_seconds(start_time: datetime, end_time: datetime) -> float:
    """Query timeout (seconds) for exporting [start_time, end_time]: floor + per-day allowance, capped."""
    days: float = max((end_time - start_time).total_seconds(), 0.0) / 86400.0
    return min(EXPORT_TIMEOUT_CAP_S, EXPORT_TIMEOUT_FLOOR_S + EXPORT_TIMEOUT_PER_DAY_S * days)


def v3_write_timeout_seconds(point_count: int) -> float:
    """Write timeout (seconds) for one InfluxDB v3 write of `point_count` points: floor + per-1000-points allowance, capped."""
    return min(V3_WRITE_TIMEOUT_CAP_S, V3_WRITE_TIMEOUT_FLOOR_S + V3_WRITE_TIMEOUT_PER_1K_POINTS_S * (point_count / 1000.0))


# TimescaleDB: statement_timeout is applied per SQL STATEMENT (SET LOCAL, so it ends with the transaction and never
# leaks back into the bridge's pooled connections). The import is a long run of short statements, so this is a
# generous per-statement ceiling that grows with the import, mostly there to cover the chunk-decompression step.
TS_IMPORT_TIMEOUT_FLOOR_S: float = 60.0
TS_IMPORT_TIMEOUT_PER_1K_POINTS_S: float = 5.0
TS_IMPORT_TIMEOUT_CAP_S: float = 3600.0

# InfluxDB v1 exports are read one window at a time instead of as a single huge SELECT.
V1_EXPORT_WINDOW: timedelta = timedelta(days=7)


def timescale_import_timeout_seconds(point_count: int) -> float:
    """statement_timeout (seconds) for a TimescaleDB import of `point_count` points: floor + per-1000-points allowance, capped."""
    return min(TS_IMPORT_TIMEOUT_CAP_S, TS_IMPORT_TIMEOUT_FLOOR_S + TS_IMPORT_TIMEOUT_PER_1K_POINTS_S * (point_count / 1000.0))


def _set_local_statement_timeout(session: Any, seconds: float) -> None:
    """
    Limits every statement the session runs in its CURRENT transaction to `seconds` (0 would mean "no limit").

    set_config(..., true) is SET LOCAL as a function call: it is transaction-scoped, and unlike a literal
    "SET LOCAL statement_timeout = N" it accepts a bound parameter. The caller must already be inside the
    transaction the limit is meant for (inside `session.begin()`, or on a session that has not yet committed).
    """
    from sqlalchemy import text as _sql_text  # noqa: PLC0415 -- same local-import convention as the rest of this file's Timescale code

    session.execute(_sql_text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(int(seconds * 1000))})


def _open_v1_client(bridge: Any, timeout_s: float) -> Any:
    """A throwaway InfluxDBClient (v1) with the bridge's connection details and the given timeout. Caller must close() it.

    retries=0: a query that already ran for its full (long) timeout should fail and surface, not silently run again.
    """
    from influxdb import InfluxDBClient  # type: ignore[import-untyped]  # noqa: PLC0415 -- same deferral as the other v1 imports in this file

    return InfluxDBClient(
        host=bridge.host,
        port=bridge.port,
        username=bridge.username if bridge.username else "",
        password=bridge.password if bridge.password else "",
        database=bridge.database,
        timeout=timeout_s,
        retries=0,
    )


def _open_v3_client(bridge: Any, *, query_timeout_s: float | None = None, write_timeout_s: float | None = None) -> Any:
    """A throwaway InfluxDBClient3 with the bridge's connection details and the given timeouts (the client takes milliseconds). Caller must close() it."""
    from influxdb_client_3 import InfluxDBClient3  # type: ignore[import-untyped]  # noqa: PLC0415

    kwargs: dict[str, Any] = dict(bridge._build_client_kwargs())  # pyright: ignore[reportPrivateUsage] -- same connection details the bridge itself uses
    if query_timeout_s is not None:
        kwargs["query_timeout"] = int(query_timeout_s * 1000)
    if write_timeout_s is not None:
        kwargs["write_timeout"] = int(write_timeout_s * 1000)
    return InfluxDBClient3(**kwargs)


V1_WRITE_BATCH_SIZE: int = 1000        # points per HTTP request to InfluxDB v1
V3_WRITE_BATCH_SIZE: int = 10_000      # points per write call to InfluxDB v3 (each is its own request, so progress is real)
TS_WRITE_BATCH_POINTS: int = 1_000     # points per TimescaleDB write step (one COPY + merge per table, then one progress event)


def _points_label(done: int, total: int) -> str:
    return f"Writing points · {done:,} of {total:,}"


def write_points_v1(
    gateway: "Protocol_Gateway | None", points: list[InfluxPointDict], allow_float_coercion: bool,
    on_progress: ProgressCallback | None = None,
    ) -> int:
    """
    Writes points to a live influxdb_out (v1) bridge via its own
    InfluxDBClient, same retry-on-type-conflict behavior as
    InfluxDateConverter.write_csv_to_influx()'s write loop.

    Raises:
        RuntimeError: no v1 bridge attached, or not connected.
    """
    bridge: influxdb_out | None = get_influxdb1_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v1 bridge is attached to this gateway.")
    if bridge.client is None:
        raise RuntimeError("InfluxDB v1 bridge is not connected.")

    payload: list[dict[str, Any]] = [
        {"measurement": p.measurement, "time": p.time_iso, "fields": p.fields, "tags": p.tags} for p in points
    ]
    if not payload:
        return 0

    # Own client with a longer timeout than the bridge's (10 s) -- see "Dynamic timeouts" above.
    client = _open_v1_client(bridge, V1_WRITE_TIMEOUT_S)
    try:
        return _write_points_v1_with_client(client, payload, allow_float_coercion, on_progress)
    finally:
        client.close()


def _write_points_v1_with_client(
    client: Any, payload: list[dict[str, Any]], allow_float_coercion: bool, on_progress: ProgressCallback | None = None,
    ) -> int:
    """
    The retry-on-type-conflict write loop of write_points_v1(), against an already-open client. Points go out one batch
    of V1_WRITE_BATCH_SIZE at a time (progress is reported after each). On a field-type conflict the offending field is
    forced to float in every point not yet written and the failing batch is retried; batches already accepted are kept.
    """
    from influxdb.exceptions import InfluxDBClientError  # type: ignore[import-untyped]  # noqa: PLC0415

    total: int = len(payload)
    written: int = 0
    if on_progress is not None:
        on_progress(_points_label(0, total), 0, total)
    while written < total:
        batch: list[dict[str, Any]] = payload[written:written + V1_WRITE_BATCH_SIZE]
        try:
            client.write_points(batch, batch_size=V1_WRITE_BATCH_SIZE)  # type: ignore[reportUnknownMemberType]
        except InfluxDBClientError as e:
            if allow_float_coercion and getattr(e, "code", None) == 400 and "field type conflict" in str(getattr(e, "content", "")):
                match: re.Match[str] | None = re.search(r'input field \\?"([^\\"]+)\\?"', str(e.content))  # type: ignore[reportUnknownArgumentType]  -- InfluxDBClientError.content has no type in the influxdb package's own (untyped) exception class
                if match:
                    offending_field: str = match.group(1)
                    _log.info(f"[Timeshift] Type conflict on field '{offending_field}' -- forcing to float and retrying.")
                    for pt in payload[written:]:
                        val: Any = pt["fields"].get(offending_field)
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            pt["fields"][offending_field] = float(val)
                    continue
            content: str = str(getattr(e, "content", "") or e)
            if getattr(e, "code", None) == 400 and _is_retention_error(content):
                retention_msg: str = (
                    "InfluxDB rejected the import because some points fall outside the database's retention policy "
                    f"(InfluxDB said: {content}). Either extend the retention policy or change Target Start so the imported "
                    "range is within it. Points in batches before the rejected one may already have been written -- "
                    "re-running the import after fixing the cause is safe (same timestamps overwrite)."
                )
                raise RetentionPolicyConflictError(retention_msg, may_have_partial_write=True) from e
            raise
        written += len(batch)
        if on_progress is not None:
            on_progress(_points_label(written, total), written, total)
    return total


def write_points_v3(
    gateway: "Protocol_Gateway | None", points: list[InfluxPointDict], on_progress: ProgressCallback | None = None,
    ) -> int:
    """
    Writes points to a live influxdb3_out (v3) bridge via its own InfluxDBClient3, using the same Point-building
    convention as influxdb3_out._dict_to_influx3_point(). Points go out V3_WRITE_BATCH_SIZE at a time so progress is
    real; a retry after a failure is safe (same timestamps overwrite).
    """
    from influxdb_client_3 import Point  # type: ignore[import-untyped]

    bridge: influxdb3_out | None = get_influxdb3_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v3 bridge is attached to this gateway.")
    if bridge.client is None:
        raise RuntimeError("InfluxDB v3 bridge is not connected.")

    total: int = len(points)
    if not points:
        return 0

    # Each batch is its own write, so the timeout only has to cover one batch (the client default is 10 s).
    client: Any = _open_v3_client(bridge, write_timeout_s=v3_write_timeout_seconds(min(total, V3_WRITE_BATCH_SIZE)))
    written: int = 0
    try:
        if on_progress is not None:
            on_progress(_points_label(0, total), 0, total)
        for offset in range(0, total, V3_WRITE_BATCH_SIZE):
            influx_points: list[Point] = []
            for p in points[offset:offset + V3_WRITE_BATCH_SIZE]:
                pt = Point(p.measurement)
                for tag_key, tag_val in p.tags.items():
                    pt = pt.tag(tag_key, tag_val)  # type: ignore[reportUnknownMemberType]
                for field_key, field_val in p.fields.items():
                    pt = pt.field(field_key, field_val)  # type: ignore[reportUnknownMemberType]
                pt = pt.time(pd.Timestamp(p.time_iso).value)  # type: ignore[reportUnknownMemberType]
                influx_points.append(pt)
            try:
                client.write(record=influx_points, database=bridge.database)  # type: ignore[reportUnknownMemberType]
            except Exception as exc:
                if _is_retention_error(str(exc)):
                    retention_msg: str = (
                        f"InfluxDB rejected the import and mentioned the retention period ({exc}). Either extend the database's "
                        "retention period or change Target Start so the imported range is within it."
                    )
                    raise RetentionPolicyConflictError(retention_msg, may_have_partial_write=written > 0) from exc
                raise
            written += len(influx_points)
            if on_progress is not None:
                on_progress(_points_label(written, total), written, total)
    finally:
        client.close()
    return written


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
    *, end_inclusive: bool = True,
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

    _go(start_time, end_time, end_inclusive=end_inclusive)
    return rows


# ---------------------------------------------------------------------------
# Export, one window at a time
#
# An export is read in time windows and each window is handed to the caller before the next is queried, so memory
# holds one window (not the whole range) and the caller can report progress after every window. The windows are
# contiguous and half-open except the last, so a point exactly on a boundary is read once.
# ---------------------------------------------------------------------------

V3_EXPORT_WINDOW: timedelta = timedelta(days=7)
TS_EXPORT_WINDOW: timedelta = timedelta(days=1)   # a TimescaleDB narrow table holds one row per metric per sample, so its windows are shorter


@dataclass(frozen=True)
class ExportWindow:
    """The rows of one export window, already time-shifted. `index` is 1-based; `total` is the window count of the whole export."""
    index: int
    total: int
    rows: list[dict[str, Any]]


def _plan_windows(start_time: datetime, end_time: datetime, span: timedelta) -> list[tuple[datetime, datetime, bool]]:
    """(window_start, window_end, end_inclusive) covering [start_time, end_time]: contiguous, half-open except the last."""
    windows: list[tuple[datetime, datetime, bool]] = []
    window_start: datetime = start_time
    while True:
        window_end: datetime = min(window_start + span, end_time)
        is_last: bool = window_end >= end_time
        windows.append((window_start, window_end, is_last))
        if is_last:
            return windows
        window_start = window_end


def _header_for(rows: Iterable[dict[str, Any]]) -> list[str]:
    """\"time\" first, then every other column in first-seen order."""
    header: list[str] = ["time"]
    seen: set[str] = {"time"}
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                header.append(k)
    return header


def iter_export_windows(
    gateway: "Protocol_Gateway | None",
    version: str,
    measurement: str,
    device_identifier: str | None,
    start_time: datetime,
    end_time: datetime,
    target_start: datetime,
    tag_keys: set[str],
    ) -> Generator[ExportWindow, None, None]:
    """
    Queries every field for `measurement` (optionally filtered to one device_identifier) between start_time/end_time one
    window at a time, applying the same time-shift delta InfluxDateConverter.export_influx_data_to_csv() does. Tag
    columns (`tag_keys`) are dropped from the output, same as IGNORE_TAGS there -- on re-import, tags are re-supplied as
    fixed values via the Tags panel, not read back from the file.

    Validation happens now, when this is called; the queries run as the returned iterator is consumed, and its client is
    closed when the iterator is exhausted or closed.

    Raises:
        ValueError: end_time before start_time, or no connected bridge.
    """
    if end_time < start_time:
        raise ValueError("End time must not be before start time.")

    time_delta: timedelta = compute_time_delta(start_time, target_start)
    if version == "1":
        v1_bridge: influxdb_out | None = get_influxdb1_bridge(gateway)
        if v1_bridge is None or v1_bridge.client is None:
            raise ValueError("No connected InfluxDB v1 bridge is attached to this gateway.")
        return _iter_v1_windows(v1_bridge, measurement, device_identifier, start_time, end_time, time_delta, tag_keys)

    v3_bridge: influxdb3_out | None = get_influxdb3_bridge(gateway)
    if v3_bridge is None or v3_bridge.client is None:
        raise ValueError("No connected InfluxDB v3 bridge is attached to this gateway.")
    return _iter_v3_windows(v3_bridge, measurement, device_identifier, start_time, end_time, time_delta, tag_keys)


def _iter_v1_windows(
    bridge: Any, measurement: str, device_identifier: str | None, start_time: datetime, end_time: datetime,
    time_delta: timedelta, tag_keys: set[str],
    ) -> Generator[ExportWindow, None, None]:
    from influxdb.client import quote_ident  # type: ignore[import-untyped]
    from influxdb.resultset import ResultSet  # type: ignore[import-untyped]
        #-- real (runtime) import for the isinstance() check below;
        # safe here (unlike a module-level import) because a v1 bridge has already been confirmed,
        # which means the `influxdb` package is necessarily already installed and importable
        # -- same reasoning as write_points_v1()'s own local `from influxdb.exceptions import InfluxDBClientError`.

    quoted_measurement: str = quote_ident(measurement)
    utc: ZoneInfo = ZoneInfo("UTC")
    windows: list[tuple[datetime, datetime, bool]] = _plan_windows(start_time, end_time, V1_EXPORT_WINDOW)

    def window_query(window_start: datetime, window_end: datetime, end_inclusive: bool) -> str:
        end_op: str = "<=" if end_inclusive else "<"
        window_where: str = (
            f"time >= {_influxql_quote_literal(window_start.astimezone(utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))} "
            f"AND time {end_op} {_influxql_quote_literal(window_end.astimezone(utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))}"
        )
        if device_identifier:
            window_where += f" AND device_identifier = {_influxql_quote_literal(device_identifier)}"
        return f"SELECT * FROM {quoted_measurement} WHERE {window_where}"  # noqa: S608

    # One client serves every window (timeout sized for a full window) and is closed when the iterator ends.
    export_client: Any = _open_v1_client(bridge, export_timeout_seconds(start_time, start_time + V1_EXPORT_WINDOW))
    try:
        for number, (window_start, window_end, end_inclusive) in enumerate(windows, start=1):
            # InfluxDBClient.query() can return a single ResultSet, a list[ResultSet] (only if the query string held
            # multiple ';'-separated statements), or a Generator[ResultSet, Any, None] (only when chunked=True) -- all
            # part of its real signature. Only the plain-ResultSet case should ever occur for one un-chunked SELECT;
            # the others are checked explicitly so a surprise fails with a clear error, not an AttributeError.
            query_result: Generator[ResultSet, Any, None] | ResultSet | list[ResultSet] = export_client.query(window_query(window_start, window_end, end_inclusive), database=bridge.database, epoch="ns")  # type: ignore[reportUnknownMemberType]
            if not isinstance(query_result, ResultSet):
                msg: str = (f"Expected a single, non-chunked result set for measurement '{measurement}', "
                    f"got {type(query_result).__name__}.")
                _log.error(msg)
                raise ValueError(msg)  # noqa: TRY004 -- callers (the export endpoint) report ValueError's message to the user
            points: list[dict[str, Any]] = cast(list[dict[str, Any]], list(query_result.get_points()))  # type: ignore[reportUnknownMemberType]  -- influxdb client's ResultSet.get_points() has no precise element type in its stubs

            rows: list[dict[str, Any]] = []
            for point in points:
                time_ns: Any = point.get("time")
                ts: pd.Timestamp = pd.Timestamp(int(time_ns), unit="ns", tz="UTC")
                new_row: dict[str, Any] = {"time": (ts + time_delta).tz_localize(None).isoformat()}
                for k, v in point.items():
                    if k in tag_keys or k == "time":
                        continue
                    new_row[k] = v
                rows.append(new_row)
            yield ExportWindow(number, len(windows), rows)
    finally:
        export_client.close()


def _iter_v3_windows(
    bridge: Any, measurement: str, device_identifier: str | None, start_time: datetime, end_time: datetime,
    time_delta: timedelta, tag_keys: set[str],
    ) -> Generator[ExportWindow, None, None]:
    quoted_measurement: str = _sql_quote_ident(measurement)
    windows: list[tuple[datetime, datetime, bool]] = _plan_windows(start_time, end_time, V3_EXPORT_WINDOW)

    # A wide range can exceed InfluxDB 3 CORE's per-query Parquet file limit (Enterprise has none), so each window is
    # queried through _query_v3_time_slices, which bisects the window further only if the server rejects it.
    def fetch_slice(slice_start: datetime, slice_end: datetime, end_inclusive: bool) -> list[dict[str, Any]]:
        end_op: str = "<=" if end_inclusive else "<"
        slice_start_lit: str = slice_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        slice_end_lit: str = slice_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        slice_where: str = f"time >= {_sql_quote_literal(slice_start_lit)} AND time {end_op} {_sql_quote_literal(slice_end_lit)}"
        if device_identifier:
            slice_where += f" AND device_identifier = {_sql_quote_literal(device_identifier)}"
        slice_sql: str = f"SELECT * FROM {quoted_measurement} WHERE {slice_where}"  # noqa: S608
        # InfluxDBClient3.query(..., mode="all") (the default we rely on by not passing `mode=`) returns a
        # pyarrow.Table at runtime -- undocumented in its own (untyped) signature, but true of the installed client per
        # its source. It is typed as Any rather than pyarrow.Table because pyarrow ships no py.typed marker, so pyright
        # resolves pyarrow.Table to Unknown even when imported.
        slice_table: Any = export_client.query(slice_sql, database=bridge.database, language="sql")  # type: ignore[reportUnknownMemberType]
        return [cast(dict[str, Any], r) for r in slice_table.to_pylist()]  # type: ignore[reportUnknownMemberType]

    # Own client whose per-query timeout is sized for one window (the client's default is 5 minutes).
    export_client: Any = _open_v3_client(bridge, query_timeout_s=export_timeout_seconds(start_time, start_time + V3_EXPORT_WINDOW))
    try:
        for number, (window_start, window_end, end_inclusive) in enumerate(windows, start=1):
            records: list[dict[str, Any]] = _query_v3_time_slices(fetch_slice, window_start, window_end, end_inclusive=end_inclusive)
            rows: list[dict[str, Any]] = []
            for record_map in records:
                time_value: Any = record_map.get("time")
                if time_value is None:
                    continue  # a record with no time value can't be placed on the timeline -- skip it
                ts: pd.Timestamp = pd.Timestamp(time_value)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                new_row: dict[str, Any] = {"time": (ts.tz_convert("UTC") + time_delta).tz_localize(None).isoformat()}
                for k, v in record_map.items():
                    if k in tag_keys or k == "time":
                        continue
                    new_row[k] = v
                rows.append(new_row)
            yield ExportWindow(number, len(windows), rows)
    finally:
        export_client.close()


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
    """iter_export_windows() collected into one (header, rows) pair -- everything in memory, so only for ranges known to be small."""
    rows: list[dict[str, Any]] = []
    for window in iter_export_windows(gateway, version, measurement, device_identifier, start_time, end_time, target_start, tag_keys):
        rows.extend(window.rows)
    return _header_for(rows), rows


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
    existing_wide_columns: set[str] | None = None,  # required (non-None) when table_kind == "wide" -- see SECURITY note below
    round_to_integer: bool = False,    # wide only: round a fractional value to the nearest whole number when its column is SMALLINT/INTEGER/BIGINT, instead of skipping it
    ) -> tuple[list[TimescalePointDict], TimeshiftImportResult]:
    """
    TimescaleDB counterpart of build_points() -- same row-by-row time-shift
    math (identical to the letter, since it doesn't depend on the
    destination), but coercing each mapped value against a wide column's
    declared Postgres type instead of InfluxDB's own field-type bucket, and
    with no tags (the whole point set shares one device_info_id, attached
    by write_points_timescale() rather than per-point here).

    SECURITY: `mapping`'s values become dict keys on the returned
    TimescalePointDicts, and for table_kind == "wide" those keys are used
    by write_points_timescale()/_upsert_from_stage_sql() to build a raw SQL
    column list -- unlike an InfluxDB field name (just a tag value in a
    parameterized line-protocol write), a wide-table target name is a SQL
    identifier. `mapping` itself is client-supplied JSON
    (routers/timeshift.py's mapping_json), so for table_kind == "wide"
    every target is checked against `existing_wide_columns` (the real,
    already-existing column names for this table, from
    load_timescale_field_types/timescale_fields_for -- themselves sourced
    from metric_catalog, never from the request) before it's allowed into
    `fields`; anything else is reported in result.unmapped_columns and
    dropped, exactly like a column the admin left unmapped. This is the
    same untrusted-name whitelist discipline delete_fields()/
    edit_metric_values() apply before building their own column lists.
    Narrow-table targets become metric_name bind PARAMETER values (see
    _NARROW_COLUMNS), never identifiers, so they carry no such risk and
    existing_wide_columns is ignored for table_kind == "narrow".

    A wide-table value that fails transports.timescaledb._coerce_value_for_
    data_type (wrong shape for its column, e.g. text into a numeric column,
    a fractional value into an INTEGER column, or out of an integer type's
    range) is recorded in result.type_mismatches and dropped from that row,
    the same "skip and report, don't fail the whole import" behavior
    check_field_type() gives build_points() for InfluxDB. With round_to_integer=True a
    fractional value bound for an integer-typed wide column is instead rounded to the nearest whole number
    (halves away from zero) and counted in result.rounded_counts -- opt-in because it's lossy (e.g. EG4's 0.1 kWh
    energy totals into a whole-kWh column); text and out-of-range values are still skipped and reported.
    A narrow-table
    value is never rejected this way -- there is no declared type to
    conflict with; normalize_value() is all it gets, and write_points_
    timescale() sorts it into metric_value/metric_ascii by its Python type
    at write time, exactly as transports.timescaledb._flush_batch_narrow
    does for live data.
    """
    from ...transports.timescaledb import (  # noqa: PLC0415 -- local: see write_points_timescale's note on deferring this transport's imports
        _METRIC_EDIT_INTEGER_TYPES,  # pyright: ignore[reportPrivateUsage] -- same integer-type list the coercion below checks against
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
                # Reject any mapping target that isn't a real, already-existing
                # column on this wide table -- see this function's SECURITY note.
                # existing_wide_columns is required (non-None) for table_kind ==
                # "wide"; treated as empty (reject everything) if a caller ever
                # omits it, rather than trusting an unchecked name by default.
                if target not in (existing_wide_columns or set()):
                    result.unmapped_columns.add(col)
                    continue
                declared: str | None = wide_field_types.get(target)
                if round_to_integer and declared and declared.upper() in _METRIC_EDIT_INTEGER_TYPES:
                    whole: int | None = round_half_up(val)
                    if whole is not None and whole != val:
                        val = whole
                        result.rounded_counts[target] = result.rounded_counts.get(target, 0) + 1
                try:
                    val = _coerce_value_for_data_type(val, declared)
                except ValueError as exc:
                    if "not a whole number" in str(exc):
                        result.suggest_rounding = True
                    record_type_mismatch(result, target, val, new_time.isoformat(), declared, str(exc))
                    continue
            fields[target] = val

        if not fields:
            result.rows_skipped_no_fields += 1
            continue

        points.append(TimescalePointDict(time_iso=new_time.isoformat(), fields=fields))

    if result.rounded_counts:
        detail: str = ", ".join(f"{col} ({n})" for col, n in sorted(result.rounded_counts.items(), key=lambda kv: -kv[1]))
        total: int = sum(result.rounded_counts.values())
        result.warnings.append(f"Rounded {total} fractional value(s) to whole numbers for integer columns: {detail}. (The same rounded values go to the narrow table.)")
        _log.info(f"[Timeshift] TimescaleDB import: rounded {total} fractional value(s) to whole numbers: {detail}")

    return points, result


# device_metrics_narrow's PK is (m_time, device_info_id, metric_name) -- see transports.timescaledb.
# DeviceMetricsNarrow. ON CONFLICT DO UPDATE (not DO NOTHING, unlike the live flush worker's own narrow
# write) so re-running a Timeshift import over the same target range is idempotent/overwriting, the
# same expectation InfluxDB's line-protocol writes already give this screen's other two destinations.
_NARROW_COLUMNS: list[str] = ["m_time", "device_info_id", "metric_name", "metric_value", "metric_ascii"]
_NARROW_KEY: list[str] = ["m_time", "device_info_id", "metric_name"]


def _narrow_value_pair(value: int | float | str | bool) -> tuple[float, str | None]:
    """Mirrors transports.timescaledb._flush_batch_narrow's numeric-vs-ascii split, for the narrow mirror of a wide-table write (and for a narrow-only write)."""
    if isinstance(value, bool):
        return (1.0 if value else 0.0), None
    if isinstance(value, (int, float)):
        return float(value), None
    return 0.0, str(value)


def _copy_cell(value: Any) -> str:
    """
    One value in PostgreSQL's COPY text format: NULL is \\N, and backslash, tab, newline and carriage return are
    backslash-escaped, so a string can never be mistaken for a delimiter or for NULL (a literal \\N in the data
    becomes \\\\N). Numbers go through float()/int() first so a NumPy scalar never writes its repr.
    """
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return repr(float(value))   # 'nan' / 'inf' are accepted by PostgreSQL's float input
    if isinstance(value, int):
        return str(int(value))
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _upsert_from_stage_sql(target: str, stage: str, columns: list[str], key_columns: list[str]) -> str:
    """
    Merges the rows COPYed into `stage` into `target`: ON CONFLICT on the table's own primary key, overwriting the other
    columns: re-running a Timeshift import over the same range overwrites instead of failing, the same
    expectation InfluxDB's line-protocol writes give this screen's other two destinations. `target`/`columns` are trusted SQL identifiers by the time they reach here -- target from
    bridge_service.resolve_wide_table_name() (itself sourced from protocol_registry, only ever a name this app created) or
    the fixed narrow table, columns from metric_catalog.clean_column_name (sanitized at wide-table-creation time) -- the
    same trust level transports.timescaledb.BridgeAdminManager._wide_view_names already extends to a table name it's given.
    """
    col_list: str = ", ".join(columns)
    update_list: str = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in key_columns)
    return (
        f"INSERT INTO {target} ({col_list}) SELECT {col_list} FROM {stage} "  # noqa: S608
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {update_list}"
    )


def _copy_upsert(
    cursor: Any, target: str, stage: str, columns: list[str], key_columns: list[str], rows: list[tuple[Any, ...]],
    ) -> None:
    """
    Bulk-upserts `rows` (tuples in `columns` order, keys unique) into `target`: COPY them into a temporary table shaped
    like `target`, then merge that into `target` with ON CONFLICT (COPY itself cannot upsert). COPY streams the rows in
    one go instead of building and parsing a big INSERT, which is what makes this the fast path. The staging table
    lives only inside the import's transaction and is emptied after each merge.
    """
    if not rows:
        return
    cursor.execute(f"CREATE TEMP TABLE IF NOT EXISTS {stage} (LIKE {target}) ON COMMIT DROP")
    payload: io.StringIO = io.StringIO("".join("\t".join(_copy_cell(v) for v in row) + "\n" for row in rows))
    cursor.copy_expert(f"COPY {stage} ({', '.join(columns)}) FROM STDIN", payload)  # noqa: S608
    cursor.execute(_upsert_from_stage_sql(target, stage, columns, key_columns))
    cursor.execute(f"TRUNCATE {stage}")


_WIDE_KEY: list[str] = ["m_time", "device_info_id"]
_WIDE_STAGE: str = "_mpg_timeshift_stage_wide"
_NARROW_STAGE: str = "_mpg_timeshift_stage_narrow"


def _upsert_wide_batch(
    cursor: Any, table_name: str, device_info_id: int, points: list[TimescalePointDict], times: list[datetime],
    ) -> None:
    """
    Upserts `points` into the wide table. A run of consecutive points that share a column set is one COPY + merge (the
    usual case: every row of a file has the same columns); a change of column set starts a new one, so the order
    points are applied in is the order they appear. Within a run a repeated timestamp keeps its last row, which is
    what applying the rows one after another would have left (PostgreSQL refuses a single statement that updates the
    same row twice).
    """
    run_columns: tuple[str, ...] = ()
    run_rows: dict[datetime, tuple[Any, ...]] = {}

    def flush() -> None:
        if run_rows:
            _copy_upsert(
                cursor, table_name, _WIDE_STAGE, [*_WIDE_KEY, *run_columns], _WIDE_KEY,
                [(m_time, device_info_id, *values) for m_time, values in run_rows.items()],
            )
            run_rows.clear()

    for p, m_time in zip(points, times):
        if not p.fields:
            continue
        columns: tuple[str, ...] = tuple(p.fields)
        if columns != run_columns:
            flush()
            run_columns = columns
        run_rows[m_time] = tuple(p.fields.values())
    flush()


def _narrow_rows_for(points: list[TimescalePointDict], times: list[datetime], device_info_id: int) -> tuple[list[tuple[Any, ...]], int]:
    """(rows, built): the narrow-table rows for `points`, one per field, with a repeated (time, metric) keeping its last value; `built` counts them before that."""
    rows: dict[tuple[datetime, str], tuple[Any, ...]] = {}
    built: int = 0
    for p, m_time in zip(points, times):
        for name, value in p.fields.items():
            metric_value, metric_ascii = _narrow_value_pair(value)
            rows[(m_time, name)] = (m_time, device_info_id, name, metric_value, metric_ascii)
            built += 1
    return list(rows.values()), built


def write_points_timescale(
    gateway: "Protocol_Gateway | None",
    table_kind: str,
    table_name: str,
    device_info_id: int,
    points: list[TimescalePointDict],
    on_progress: ProgressCallback | None = None,
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

    A Timeshift import commonly targets an older date range -- that's the
    point of shifting data onto or off of a past window -- which is
    exactly the kind of range TimescaleDB's own compression policy has
    usually already compressed by the time an admin gets to it. Before the
    write, this function pauses the compression job (if any) for every
    table it's about to touch and decompresses just the chunks overlapping
    the batch's own time range, the same range-scoped pattern
    BridgeAdminManager.edit_metric_values() uses for Metrics Edit -- rather
    than relying on however the connected TimescaleDB server happens to
    handle DML against a still-compressed chunk. This best-effort step
    never fails the import: a table with no compression configured, or no
    compressed chunks in range, simply no-ops (see
    HyperTableManager.pause_compression_job_for_table/
    decompress_chunks_in_range). The paused job(s) are always resumed in a
    `finally`, whether or not the write succeeded; a decompressed chunk is
    recompressed later by the normal compression schedule, or immediately
    via Rebuild Compression (section 4.6) if you want it done sooner.

    Raises:
        RuntimeError: no TimescaleDB bridge is attached to this gateway, or it isn't connected.
        ValueError: table_kind isn't "narrow"/"wide".
    """
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

    times: list[datetime] = [datetime.fromisoformat(p.time_iso).replace(tzinfo=timezone.utc) for p in points]
    range_start, range_end = min(times), max(times)

    # Every table this batch will actually write to -- device_metrics_narrow
    # always, plus the wide table when table_kind == "wide" (table_name IS
    # "device_metrics_narrow" already for a narrow-only write, so this is a
    # single-element set in that case).
    tables_to_prepare: set[str] = {table_name, "device_metrics_narrow"}
    if on_progress is not None:
        on_progress("Preparing: pausing compression and decompressing chunks in the imported range", 0, 0)
    import_timeout_s: float = timescale_import_timeout_seconds(len(points))
    t_start: float = time.perf_counter()
    hypertable_mgr: Any | None = getattr(bridge, "hypertable_mgr", None)
    paused_job_ids_by_table: dict[str, list[int]] = {}
    if hypertable_mgr is not None:
        for t in tables_to_prepare:
            try:
                with bridge.SessionFactory() as session:
                    paused: list[int] = hypertable_mgr.pause_compression_job_for_table(session, t)
                    session.commit()
                if paused:
                    paused_job_ids_by_table[t] = paused
                with bridge.SessionFactory() as session:
                    _set_local_statement_timeout(session, import_timeout_s)
                    hypertable_mgr.decompress_chunks_in_range(session, t, range_start, range_end)
                    session.commit()
            except Exception as e:
                # Best-effort, same as Metrics Edit's identical step -- a table
                # with no compression configured, or a transient failure here,
                # should not block the import; worst case the write below hits
                # whatever the connected server does with a compressed chunk.
                _log.warning(f"[Timeshift] Could not prepare compressed chunks for '{t}' in [{range_start}, {range_end}]: {e}")

    t_prepared: float = time.perf_counter()
    _log.info(
        f"[Timeshift] TimescaleDB import: prepared {sorted(tables_to_prepare)} (paused/decompressed for "
        f"{range_start:%Y-%m-%d %H:%M} to {range_end:%Y-%m-%d %H:%M} UTC) in {t_prepared - t_start:.1f}s; writing {len(points)} point(s)."
    )
    narrow_built: int = 0
    total: int = len(points)

    try:
        with bridge.SessionFactory() as session:
            with session.begin():
                _set_local_statement_timeout(session, import_timeout_s)
                # The raw DBAPI cursor of this same transaction, so the COPY + merge below are inside it. One COPY per
                # batch instead of one statement per point (and per narrow row) turns a network round trip per value
                # into a handful per thousand points.
                cursor: Any = session.connection().connection.cursor()
                try:
                    for offset in range(0, total, TS_WRITE_BATCH_POINTS):
                        batch: list[TimescalePointDict] = points[offset:offset + TS_WRITE_BATCH_POINTS]
                        batch_times: list[datetime] = times[offset:offset + TS_WRITE_BATCH_POINTS]
                        # p.time_iso is a naive-UTC ISO string (see TimescalePointDict/build_points_timescale,
                        # and InfluxPointDict's identical convention); `times` re-attaches UTC explicitly, rather
                        # than binding the naive value, because these columns are TIMESTAMPTZ: a naive value would
                        # otherwise be interpreted in the DB session's own timezone setting (not necessarily UTC),
                        # silently shifting every written timestamp.
                        if table_kind == "wide":
                            _upsert_wide_batch(cursor, table_name, device_info_id, batch, batch_times)
                        narrow_rows, built = _narrow_rows_for(batch, batch_times, device_info_id)
                        narrow_built += built
                        _copy_upsert(cursor, "device_metrics_narrow", _NARROW_STAGE, _NARROW_COLUMNS, _NARROW_KEY, narrow_rows)
                        written_so_far: int = min(offset + TS_WRITE_BATCH_POINTS, total)
                        if on_progress is not None:
                            on_progress(_points_label(written_so_far, total), written_so_far, total)
                finally:
                    cursor.close()
    finally:
        # hypertable_mgr can't actually be None here when paused_job_ids_by_table
        # is non-empty (it's only ever populated inside the `if hypertable_mgr is
        # not None:` block above), but the type checker can't see that across the
        # try/finally boundary, so this is checked explicitly rather than assumed.
        if hypertable_mgr is not None:
            for t, job_ids in paused_job_ids_by_table.items():
                try:
                    with bridge.SessionFactory() as session:
                        hypertable_mgr.resume_compression_job_for_table(session, job_ids)
                        session.commit()
                except Exception as e:
                    _log.warning(f"[Timeshift] Could not resume compression job(s) {job_ids} for '{t}': {e}")

    _log.info(
        f"[Timeshift] TimescaleDB import: wrote {total} point(s) / {narrow_built} narrow row(s) in "
        f"{time.perf_counter() - t_prepared:.1f}s (total {time.perf_counter() - t_start:.1f}s)."
    )
    return total, narrow_built


def iter_export_windows_timescale(
    gateway: "Protocol_Gateway | None",
    table_kind: str,
    table_name: str,
    protocol_name: str | None,
    device_info_id: int,
    start_time: datetime,
    end_time: datetime,
    target_start: datetime,
    ) -> Generator[ExportWindow, None, None]:
    """
    TimescaleDB counterpart of iter_export_windows() -- same time-shift math and window shape, for one table + device.

    A wide table is already column-shaped, so its rows come back as-is (minus m_time, replaced by the shifted "time").
    A narrow table stores one row per (m_time, metric_name) -- these are pivoted into the same wide-shaped rows (one row
    per distinct shifted timestamp, one column per distinct metric_name) so a narrow export and a wide export produce an
    identically-shaped file, and so the file re-imports the same way through this same screen regardless of which table
    it came from. A timestamp falls in exactly one window, so pivoting per window loses nothing.

    Validation happens now, when this is called; the queries run as the returned iterator is consumed.

    Raises:
        ValueError: end_time before start_time, no bridge attached/connected, or table_kind isn't "narrow"/"wide".
    """
    if end_time < start_time:
        raise ValueError("End time must not be before start time.")
    if table_kind not in ("narrow", "wide"):
        msg: str = f"Unknown table_kind '{table_kind}' -- expected 'narrow' or 'wide'."
        raise ValueError(msg)

    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None or not getattr(bridge, "tsdb_connected", True):
        raise ValueError("No connected TimescaleDB bridge is attached to this gateway.")

    return _iter_timescale_windows(bridge, gateway, table_kind, table_name, protocol_name, device_info_id, start_time, end_time, target_start)


def _iter_timescale_windows(
    bridge: Any, gateway: "Protocol_Gateway | None", table_kind: str, table_name: str, protocol_name: str | None,
    device_info_id: int, start_time: datetime, end_time: datetime, target_start: datetime,
    ) -> Generator[ExportWindow, None, None]:
    from sqlalchemy import (  # noqa: PLC0415 -- local: see build_points_timescale's identical note on deferring this transport's imports
        text as _sql_text,
    )

    time_delta: timedelta = compute_time_delta(start_time, target_start)
    windows: list[tuple[datetime, datetime, bool]] = _plan_windows(start_time, end_time, TS_EXPORT_WINDOW)

    def shift(raw_time: Any) -> str:
        ts: pd.Timestamp = pd.Timestamp(raw_time)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return (ts.tz_convert("UTC") + time_delta).tz_localize(None).isoformat()

    field_names: list[str] = []
    if table_kind == "wide":
        field_names = [name for f in timescale_fields_for(gateway, "wide", protocol_name) if (name := f.get("name"))]

    with bridge.SessionFactory() as session:
        # Ends with this (never-committed) transaction, so the pooled connection goes back unchanged. The limit applies to
        # each statement, i.e. to one window.
        _set_local_statement_timeout(session, export_timeout_seconds(start_time, start_time + TS_EXPORT_WINDOW))
        for number, (window_start, window_end, end_inclusive) in enumerate(windows, start=1):
            end_op: str = "<=" if end_inclusive else "<"
            bounds: dict[str, Any] = {"did": device_info_id, "start": window_start, "end": window_end}
            rows: list[dict[str, Any]] = []
            if table_kind == "narrow":
                result: Any = session.execute(
                    _sql_text(
                        "SELECT m_time, metric_name, metric_value, metric_ascii FROM device_metrics_narrow "  # noqa: S608
                        f"WHERE device_info_id = :did AND m_time >= :start AND m_time {end_op} :end ORDER BY m_time"  # noqa: S608 -- end_op is one of two constants
                    ),
                    bounds,
                )
                by_time: dict[str, dict[str, Any]] = {}
                for m_time, metric_name, metric_value, metric_ascii in result:
                    new_row: dict[str, Any] = by_time.setdefault(shift(m_time), {})
                    new_row[metric_name] = metric_ascii if metric_ascii is not None else metric_value
                rows = [{"time": t, **fields} for t, fields in by_time.items()]
            elif field_names:
                select_cols: str = ", ".join(_sql_quote_ident(c) for c in field_names)
                result = session.execute(
                    _sql_text(
                        f"SELECT m_time, {select_cols} FROM {table_name} "  # noqa: S608 -- table_name/select_cols are trusted identifiers, see _upsert_from_stage_sql's docstring
                        f"WHERE device_info_id = :did AND m_time >= :start AND m_time {end_op} :end ORDER BY m_time"
                    ),
                    bounds,
                )
                for record in result:
                    wide_row: dict[str, Any] = {"time": shift(record[0])}
                    for name, value in zip(field_names, record[1:]):
                        wide_row[name] = value
                    rows.append(wide_row)
            yield ExportWindow(number, len(windows), rows)


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
    """iter_export_windows_timescale() collected into one (header, rows) pair -- everything in memory, so only for ranges known to be small."""
    rows: list[dict[str, Any]] = []
    for window in iter_export_windows_timescale(gateway, table_kind, table_name, protocol_name, device_info_id, start_time, end_time, target_start):
        rows.extend(window.rows)
    return _header_for(rows), rows


# ---------------------------------------------------------------------------
# Export to a CSV file on disk, with progress
#
# The column set of the CSV is only known once every window has been read (a column appears the first time any row has
# it), but the header must come first. So each window's rows are spooled to a temporary JSON-lines file as they arrive
# (memory holds one window), then a second pass writes the real CSV. The finished file waits in a temp folder, keyed by
# a token, until the browser fetches it.
# ---------------------------------------------------------------------------

EXPORT_FILE_TTL: timedelta = timedelta(hours=1)
_EXPORT_DIR_NAME: str = "mpg_timeshift_exports"

# Bar units: reading the windows is most of the work, so it fills the first 95% of the bar and the second (CSV) pass the
# rest. Each window is worth _READ_UNITS bar units and the CSV pass _WRITE_UNITS in total, per window of the export.
_READ_UNITS: int = 19
_WRITE_UNITS: int = 1


@dataclass
class ExportFile:
    token: str
    path: Path
    filename: str
    row_count: int
    size_bytes: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _export_dir() -> Path:
    directory: Path = Path(tempfile.gettempdir()) / _EXPORT_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _export_store(app_state: State) -> dict[str, ExportFile]:
    if not hasattr(app_state, "timeshift_exports"):
        setattr(app_state, "timeshift_exports", {})
    return cast(dict[str, ExportFile], getattr(app_state, "timeshift_exports"))


def _purge_stale_exports(app_state: State) -> None:
    """Deletes finished exports nobody collected, and spool/CSV files left behind by an interrupted export, once they are older than EXPORT_FILE_TTL."""
    cutoff: datetime = datetime.now(timezone.utc) - EXPORT_FILE_TTL
    with _upload_lock(app_state):
        store: dict[str, ExportFile] = _export_store(app_state)
        for token in [t for t, e in store.items() if e.created_at < cutoff]:
            store.pop(token).path.unlink(missing_ok=True)
    for leftover in _export_dir().iterdir():
        try:
            if leftover.stat().st_mtime < cutoff.timestamp():
                leftover.unlink(missing_ok=True)
        except OSError:
            continue  # being written or removed right now -- the next purge will see it


def new_export_paths(app_state: State) -> tuple[Path, Path]:
    """(csv_path, spool_path) for a new export, after clearing out stale ones."""
    _purge_stale_exports(app_state)
    stem: str = uuid.uuid4().hex
    return _export_dir() / f"{stem}.csv", _export_dir() / f"{stem}.spool"


def register_export_file(app_state: State, path: Path, filename: str, row_count: int) -> ExportFile:
    export: ExportFile = ExportFile(uuid.uuid4().hex, path, filename, row_count, path.stat().st_size)
    with _upload_lock(app_state):
        _export_store(app_state)[export.token] = export
    return export


def get_export_file(app_state: State, token: str) -> ExportFile | None:
    with _upload_lock(app_state):
        return _export_store(app_state).get(token)


def discard_export_file(app_state: State, token: str) -> None:
    """Forgets the export and deletes its file (called once the browser has it)."""
    with _upload_lock(app_state):
        export: ExportFile | None = _export_store(app_state).pop(token, None)
    if export is not None:
        export.path.unlink(missing_ok=True)


def write_export_csv(
    windows: Iterator[ExportWindow],
    csv_path: Path,
    spool_path: Path,
    on_progress: ProgressCallback,
    check_cancelled: Callable[[], None],
    ) -> int:
    """
    Consumes `windows` into the CSV at `csv_path` (header: "time" first, then columns in first-seen order -- the same
    shape and encoding the old in-memory export produced) and returns the number of data rows written. Reports progress
    after every window and then through the CSV pass; calls `check_cancelled` between windows so a cancelled export
    stops reading. The spool file is always removed; the CSV is removed too if anything fails.
    """
    header: list[str] = ["time"]
    seen: set[str] = {"time"}
    row_count: int = 0
    window_total: int = 1
    try:
        with spool_path.open("w", encoding="utf-8", newline="\n") as spool:
            for window in windows:
                check_cancelled()
                window_total = window.total
                for row in window.rows:
                    for column in row:
                        if column not in seen:
                            seen.add(column)
                            header.append(column)
                    # default=str writes any non-JSON value (datetime, Decimal, bytes) as str(), exactly what csv would have.
                    spool.write(json.dumps(row, default=str))
                    spool.write("\n")
                row_count += len(window.rows)
                on_progress(
                    f"Reading data: window {window.index} of {window.total} · {row_count:,} row{'s' if row_count != 1 else ''}",
                    window.index * _READ_UNITS, window.total * (_READ_UNITS + _WRITE_UNITS),
                )

        bar_total: int = window_total * (_READ_UNITS + _WRITE_UNITS)
        written: int = 0
        with spool_path.open("r", encoding="utf-8", newline="\n") as spooled, csv_path.open("w", encoding="utf-8", newline="") as out:
            writer: "csv.DictWriter[str]" = csv.DictWriter(out, fieldnames=header)
            writer.writeheader()
            for line in spooled:
                writer.writerow(cast(dict[str, Any], json.loads(line)))
                written += 1
                if written % 5000 == 0:
                    check_cancelled()
                    on_progress(
                        f"Writing CSV · {written:,} of {row_count:,} rows",
                        window_total * _READ_UNITS + (window_total * _WRITE_UNITS * written) // max(row_count, 1), bar_total,
                    )
        on_progress(f"Writing CSV · {row_count:,} of {row_count:,} rows", bar_total, bar_total)
    except BaseException:
        csv_path.unlink(missing_ok=True)
        raise
    finally:
        spool_path.unlink(missing_ok=True)
    return row_count
