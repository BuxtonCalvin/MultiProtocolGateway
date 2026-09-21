# Description: routers/timeshift.py — page shell and upload/mapping/preview/export/import endpoints for the "InfluxDB -> Timeshift Data" admin screen.
# File: timeshift.py
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
routers/timeshift.py — the GUI counterpart of tools/InfluxDateConverter.py,
reachable from the InfluxDB nav dropdown's "Timeshift Data" item for either
InfluxDB v1 or v3. Every hand-edited constant that script requires (host
etc. -- not needed here, since this always uses the gateway's own live
bridge -- measurement, tags, timezone, source/target time range, confidence
threshold, float coercion) is instead a field on this page, submitted with
each request. This file only orchestrates HTTP concerns (uploads, form
payloads, HTML partial rendering); every actual computation lives in
services/timeshift_service.py.

Flow:
  1. GET  /pages/timeshift-data                       -- page shell.
  2. GET  /pages/timeshift/fields                      -- existing InfluxDB
     field names for a measurement (schema scan), for the mapping table's
     suggestion pass and the field <select> options themselves.
  3. POST /api/timeshift/upload                        -- upload an EG4
     spreadsheet or a previously-exported InfluxDB CSV; parses it, stores
     it server-side (see services.timeshift_service upload store), and
     returns the Field Matchup table partial pre-filled with suggestions.
  4. POST /api/timeshift/preview                       -- read-only row/
     field counts + a small sample of what an import with the current
     mapping/tags/time settings would write, without touching InfluxDB.
  5. POST /api/timeshift/import                        -- runs the import
     for real, using the same mapping/tags/time settings.
  6. GET  /api/timeshift/export.csv                    -- streams a
     time-shifted CSV download of an InfluxDB date range (the "or export a
     date range ... to a folder of one's choosing" path) -- the browser's
     own Save dialog is the "folder of one's choosing" picker; a server
     process has no meaningful notion of "the user's folder" in a browser-
     based admin UI, so this deliberately mirrors the existing CSV-download
     convention already used by /api/protocols/.../export.csv rather than
     inventing a server-side file-path field.
  7. DELETE /api/timeshift/upload/{upload_id}           -- discards a
     server-side upload the admin no longer needs (e.g. switched files).

Nothing here is part of the staged-config-changes/"Commit All Changes"
pipeline (routers/commit.py) -- Timeshift Data writes/reads point DATA, not
gateway configuration, so Preview/Import are its own explicit, immediate
steps, same as Timescale DB's "Rebuild Rollup Views" action rather than
Metrics Edit's staging queue.

Typing note: this file (and services/timeshift_service.py) is written for
a clean run under `pyright --strict` (verified during development; see the
project's own reportUnknownMemberType/reportArgumentType suppressions
elsewhere for the established style of narrowing/ignoring the small number
of spots where a third-party library's stubs are themselves incomplete).
Every function has an explicit return type; every local variable whose
type isn't immediately obvious from its initializer is annotated
explicitly, per the same convention routers/influxdb.py already follows.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ..database import session_scope
from ..services.device_service import NavData, get_nav_data
from ..services.influxdb_service import (
    get_influxdb1_bridge,
    get_influxdb3_bridge,
    list_metric_edit_fields,
)
from ..services.timeshift_service import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FieldMappingRow,
    InfluxPointDict,
    SourceKind,
    TimeshiftImportResult,
    TimeshiftUpload,
    build_points,
    compute_time_delta,
    detect_time_column,
    discard_upload,
    export_range_rows,
    get_upload,
    is_eg4_protocol_in_use,
    load_influx_field_types,
    machine_timezone_for,
    measurements_for,
    parse_spreadsheet_bytes,
    store_upload,
    suggest_field_mapping,
    write_points_v1,
    write_points_v3,
)
from .pages import base_context

if TYPE_CHECKING:
    # Deferred at runtime -- same circular-import avoidance as routers/influxdb.py.
    from protocol_gateway import Protocol_Gateway

_log: logging.Logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(tags=["timeshift"])

MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MB -- generous for a single date-range spreadsheet/CSV export


def _require_bridge(request: Request, version: str) -> Any:
    """
    Resolves the live influxdb_out (v1) or influxdb3_out (v3) bridge for
    `version`, or raises 404. See routers/influxdb.py's identical helper
    for why this returns `Any` rather than a Union of the two transport
    classes: every caller here only ever touches `.client`/`.database`,
    which both bridge classes provide but which aren't declared through a
    common typed base in classes/transports/ -- typing this as a Union
    would just push the same `Any`-shaped member access one level down
    into every call site instead of centralizing it here.
    """
    if version not in ("1", "3"):
        raise HTTPException(status_code=404, detail=f"Unknown InfluxDB version '{version}'.")
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    bridge: Any | None = get_influxdb1_bridge(gateway) if version == "1" else get_influxdb3_bridge(gateway)
    if bridge is None:
        raise HTTPException(status_code=404, detail=f"No InfluxDB v{version} bridge is attached to this gateway.")
    return bridge


def _parse_local_datetime(value: str, tz_name: str) -> datetime:
    """Parses a <input type="datetime-local"> value into a tz-aware datetime. See routers/influxdb.py's identical helper."""
    try:
        naive: datetime = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{value}' is not a valid date/time.")
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def _parse_json_object(raw: str, error_detail: str) -> dict[str, object]:
    """
    Parses `raw` as JSON, requiring the top-level value to be a JSON
    object -- raises HTTPException(400) with `error_detail` otherwise.
    json.loads() always produces str keys for a JSON object, but a plain
    `isinstance(parsed, dict)` check only narrows to `dict[Unknown,
    Unknown]` for the type checker, so each key is additionally checked
    here to arrive at a genuinely (not just assumed) `dict[str, object]`.
    """
    try:
        parsed: object = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=error_detail)
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=error_detail)
    result: dict[str, object] = {}
    for key, value in cast("dict[object, object]", parsed).items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail=error_detail)
        result[key] = value
    return result


def _parse_tags_json(tags_json: str) -> dict[str, str]:
    parsed: dict[str, object] = _parse_json_object(
        tags_json, 'Tags must be valid JSON (an object of key/value strings), e.g. {"device_identifier": "..."}.',
    )
    tags: dict[str, str] = {k: str(v) for k, v in parsed.items() if k.strip() and str(v).strip()}
    if "device_identifier" not in tags:
        raise HTTPException(status_code=400, detail="A 'device_identifier' tag is required.")
    return tags


def _parse_mapping_json(mapping_json: str) -> dict[str, str]:
    parsed: dict[str, object] = _parse_json_object(
        mapping_json, "Field mapping must be a JSON object of {source_column: influx_field}.",
    )
    return {k: str(v) for k, v in parsed.items()}


def cast_source_kind(value: str) -> SourceKind:
    """Narrow a validated str to the SourceKind Literal for typed downstream calls."""
    if value not in ("eg4", "influx_csv"):
        raise HTTPException(status_code=400, detail=f"Unknown source kind '{value}'.")
    return value


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

@router.get("/pages/timeshift-data", response_class=HTMLResponse, response_model=None)
async def timeshift_data_page(request: Request, version: str = "1") -> HTMLResponse:
    """
    "Timeshift Data" screen -- lets the admin export a time-shifted date
    range from either InfluxDB version to a CSV download, or import an EG4
    spreadsheet export (only offered when an eg4_* protocol is configured
    on this gateway) or a previously-exported/edited InfluxDB CSV back in,
    with a GUI field-matchup step in place of hand-editing mapped_columns.
    csv.
    """
    if version not in ("1", "3"):
        version = "1"
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    v1_ok: bool = get_influxdb1_bridge(gateway) is not None
    v3_ok: bool = get_influxdb3_bridge(gateway) is not None
    if version == "1" and not v1_ok and v3_ok:
        version = "3"
    elif version == "3" and not v3_ok and v1_ok:
        version = "1"

    measurements: list[str]
    try:
        measurements = measurements_for(gateway, version) if (v1_ok or v3_ok) else []
    except Exception as exc:
        _log.warning(f"[Timeshift] Could not list measurements for v{version}: {exc}")
        measurements = []

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/timeshift_data.html",
        context={
            **base_context(request, nav),
            "version": version,
            "v1_available": v1_ok,
            "v3_available": v3_ok,
            "measurements": measurements,
            "eg4_available": is_eg4_protocol_in_use(nav),
            "default_confidence_threshold": DEFAULT_CONFIDENCE_THRESHOLD,
            "machine_timezone": machine_timezone_for(gateway, version),
        },
    )


@router.get("/pages/timeshift/fields", response_class=HTMLResponse, response_model=None)
async def timeshift_fields_partial(request: Request, version: str, measurement: str) -> HTMLResponse:
    """Returns the raw [{name, data_type}, ...] list for `measurement` -- used client-side to populate each mapping row's target <select>."""
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    fields: list[dict[str, str | None]]
    try:
        fields = list_metric_edit_fields(gateway, version, measurement)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timeshift_fields_options.html",
        context={"fields": fields},
    )


# ---------------------------------------------------------------------------
# Upload + Field Matchup
# ---------------------------------------------------------------------------

@router.post("/api/timeshift/upload", response_class=HTMLResponse, response_model=None)
async def timeshift_upload(
    request: Request,
    version: str = Form(...),
    measurement: str = Form(...),
    source_kind: str = Form(...),          # "eg4" | "influx_csv"
    tags_json: str = Form("{}"),
    confidence_threshold: float = Form(DEFAULT_CONFIDENCE_THRESHOLD),
    file: UploadFile = File(...),
) -> HTMLResponse:
    """
    Parses an uploaded EG4 spreadsheet (.csv/.xls/.xlsx) or a previously-
    exported InfluxDB CSV, stores it server-side, and returns the Field
    Matchup table partial -- the interactive replacement for
    InfluxDateConverter.py's perform_schema_validation()/mapping_needed.csv
    hand-edit cycle.
    """
    _require_bridge(request, version)
    if source_kind not in ("eg4", "influx_csv"):
        raise HTTPException(status_code=400, detail=f"Unknown source kind '{source_kind}'.")

    raw: bytes = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    df: pd.DataFrame
    try:
        df = parse_spreadsheet_bytes(file.filename or "upload", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    tags: dict[str, str] = _parse_tags_json(tags_json)
    resolved_source_kind: SourceKind = cast_source_kind(source_kind)

    influx_fields: list[str]
    try:
        raw_fields: list[dict[str, str | None]] = list_metric_edit_fields(gateway, version, measurement)
        influx_fields = [name for f in raw_fields if (name := f.get("name")) is not None]
    except Exception:
        influx_fields = []  # brand-new measurement -- every column starts unmapped/free-text

    upload: TimeshiftUpload = store_upload(request.app.state, file.filename or "upload", resolved_source_kind, df)

    mapping_rows: list[FieldMappingRow] = suggest_field_mapping(
        upload.columns, influx_fields, set(tags.keys()), resolved_source_kind, confidence_threshold,
    )

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timeshift_field_mapping.html",
        context={
            "upload_id": upload.upload_id,
            "row_count": len(df),
            "mapping_rows": mapping_rows,
            "influx_fields": influx_fields,
            "detected_time_column": detect_time_column(upload.columns),
            "columns": upload.columns,
        },
    )


@router.delete("/api/timeshift/upload/{upload_id}")
def timeshift_discard_upload(upload_id: str, request: Request) -> dict[str, bool]:
    """Discards a server-side upload the admin no longer needs (e.g. picked the wrong file)."""
    return {"discarded": discard_upload(request.app.state, upload_id)}


# ---------------------------------------------------------------------------
# Preview + Import
# ---------------------------------------------------------------------------

class TimeshiftRunRequest(BaseModel):
    upload_id: str
    version: str
    measurement: str
    time_column: str
    mapping_json: str            # {source_column: influx_field_name}, "" target = ignored
    tags_json: str
    local_timezone: str
    source_is_local: bool        # True for an EG4 export (naive local time); False for a re-imported InfluxDB export CSV
    source_start: str            # <input type="datetime-local">
    target_start: str
    allow_float_coercion: bool = True


def _run_common(payload: TimeshiftRunRequest, request: Request) -> tuple[Any, TimeshiftUpload, dict[str, str], dict[str, str], dict[str, str]]:
    """Shared setup for Preview and Import: resolves the bridge, the stored upload, and the tags/mapping/field-type dicts every build_points() call needs. Returns (bridge, upload, tags, mapping, influx_field_types)."""
    bridge: Any = _require_bridge(request, payload.version)
    upload: TimeshiftUpload | None = get_upload(request.app.state, payload.upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="This upload has expired or was discarded -- please upload the file again.")
    if payload.time_column not in upload.columns:
        raise HTTPException(status_code=400, detail=f"Time column '{payload.time_column}' is not a column in the uploaded file.")

    tags: dict[str, str] = _parse_tags_json(payload.tags_json)
    mapping: dict[str, str] = _parse_mapping_json(payload.mapping_json)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    influx_field_types: dict[str, str]
    try:
        influx_field_types = load_influx_field_types(gateway, payload.version, payload.measurement)
    except Exception:
        influx_field_types = {}  # brand-new measurement has no existing schema to coerce against yet

    return bridge, upload, tags, mapping, influx_field_types


@router.post("/api/timeshift/preview", response_class=HTMLResponse, response_model=None)
async def timeshift_preview(payload: TimeshiftRunRequest, request: Request) -> HTMLResponse:
    """Read-only preview: builds the points the current mapping/settings would write, without touching InfluxDB."""
    _bridge, upload, tags, mapping, influx_field_types = _run_common(payload, request)

    source_start: datetime = _parse_local_datetime(payload.source_start, payload.local_timezone)
    target_start: datetime = _parse_local_datetime(payload.target_start, payload.local_timezone)
    time_delta: timedelta = compute_time_delta(source_start, target_start)

    points: list[InfluxPointDict]
    result: TimeshiftImportResult
    points, result = build_points(
        upload.dataframe,
        measurement=payload.measurement,
        mapping=mapping,
        tags=tags,
        time_column=payload.time_column,
        source_is_local=payload.source_is_local,
        local_tz=payload.local_timezone,
        time_delta=time_delta,
        influx_field_types=influx_field_types,
        allow_float_coercion=payload.allow_float_coercion,
    )

    sample: list[InfluxPointDict] = points[:10]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timeshift_preview.html",
        context={"points_to_write": len(points), "sample": sample, "result": result},
    )


@router.post("/api/timeshift/import", response_class=HTMLResponse, response_model=None)
async def timeshift_import(payload: TimeshiftRunRequest, request: Request) -> HTMLResponse:
    """Runs the import for real -- writes every built point to the resolved bridge, using the same mapping/settings preview validated."""
    _bridge, upload, tags, mapping, influx_field_types = _run_common(payload, request)

    source_start: datetime = _parse_local_datetime(payload.source_start, payload.local_timezone)
    target_start: datetime = _parse_local_datetime(payload.target_start, payload.local_timezone)
    time_delta: timedelta = compute_time_delta(source_start, target_start)

    points: list[InfluxPointDict]
    result: TimeshiftImportResult
    points, result = build_points(
        upload.dataframe,
        measurement=payload.measurement,
        mapping=mapping,
        tags=tags,
        time_column=payload.time_column,
        source_is_local=payload.source_is_local,
        local_tz=payload.local_timezone,
        time_delta=time_delta,
        influx_field_types=influx_field_types,
        allow_float_coercion=payload.allow_float_coercion,
    )

    if not points:
        raise HTTPException(status_code=400, detail="Nothing to write -- every row was skipped (see the preview for why).")

    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    written: int
    try:
        if payload.version == "1":
            written = write_points_v1(gateway, points, payload.allow_float_coercion)
        else:
            written = write_points_v3(gateway, points)
    except Exception as exc:
        _log.exception("[Timeshift] Import failed")
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")

    result.points_written = written
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timeshift_result.html",
        context={"result": result, "action": "import"},
    )


# ---------------------------------------------------------------------------
# Export (InfluxDB date range -> CSV download)
# ---------------------------------------------------------------------------

@router.get("/api/timeshift/export.csv")
def timeshift_export_csv(
    request: Request,
    version: str,
    measurement: str,
    start_time: str,
    end_time: str,
    target_start: str,
    local_timezone: str,
    device_identifier: str | None = None,
    tags_json: str = "{}",
) -> StreamingResponse:
    """
    Streams a time-shifted CSV download of `measurement` between start_time
    and end_time -- the "export a date range of InfluxDB data and save it
    to a folder of one's choosing" path. The browser's own Save/Save-As
    dialog IS the folder picker here (see this module's docstring for why
    a server-side path field isn't offered); this mirrors the existing
    /api/protocols/.../export.csv download convention exactly.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    tags: dict[str, str] = _parse_tags_json(tags_json) if tags_json and tags_json != "{}" else {}

    start_dt: datetime = _parse_local_datetime(start_time, local_timezone)
    end_dt: datetime = _parse_local_datetime(end_time, local_timezone)
    target_dt: datetime = _parse_local_datetime(target_start, local_timezone)

    header: list[str]
    rows: list[dict[str, Any]]
    try:
        header, rows = export_range_rows(
            gateway, version, measurement, device_identifier, start_dt, end_dt, target_dt, set(tags.keys()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    buf: io.StringIO = io.StringIO()
    writer: "csv.DictWriter[str]" = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)

    filename: str = f"{measurement}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}_v{version}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
