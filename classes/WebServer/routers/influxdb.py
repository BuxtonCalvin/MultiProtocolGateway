# Description: routers/influxdb.py — page shells and mutation endpoints for the "InfluxDB -> Metrics Edit 1.x / 3.x" admin screens.
# File: influxdb.py
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
routers/influxdb.py — the InfluxDB counterpart of routers/timescale.py's
Metrics Edit section, covering both InfluxDB v1 (influxdb_out) and v3
(influxdb3_out) bridges. Every route is version-parameterized ("1" or "3"
in the path) and dispatches to services/influxdb_service.py, which in turn
dispatches to whichever admin manager (InfluxV1AdminManager /
Influx3AdminManager, in classes/transports/influxdb_out.py /
influxdb3_out.py) actually owns the query/write logic for that version —
this file only ever orchestrates HTTP concerns: resolving a live bridge,
turning request bodies into typed calls, and rendering the resulting HTML
partial for the same HTMX-swap pattern routers/timescale.py uses.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..database import session_scope
from ..services.device_service import NavData, get_nav_data
from ..services.influxdb_service import (
    delete_unavailable_reason,
    get_influxdb1_bridge,
    get_influxdb3_bridge,
    get_staged_metric_edits,
    list_metric_edit_devices,
    list_metric_edit_fields,
    list_metric_edit_measurements,
    lookup_metric_edit_device_name,
    preview_metric_edit,
    stage_metric_edit,
    supports_delete,
    unstage_metric_edit,
)
from .pages import base_context

if TYPE_CHECKING:
    # Deferred at runtime — importing protocol_gateway at module load time
    # risks a circular import, since it's what wires up the WebServer app
    # in the first place (see the same pattern in commit.py/devices.py/
    # timescale.py/pages.py).
    from protocol_gateway import Protocol_Gateway

_log: logging.Logger = logging.getLogger(__name__)

router = APIRouter(tags=["influxdb"])


def _machine_timezone(request: Request, version: str) -> str:
    """
    The configured machine_timezone off whichever InfluxDB bridge this
    version resolves to (see influxdb_out.py's/influxdb3_out.py's own
    `self.machine_timezone` — "UTC" if use_utc_timestamp is set, else the
    local zone) — used to localize a <input type="datetime-local">'s
    naive value the same way that bridge stamps every point's own
    timestamp, so a range picked in the browser lines up with what's
    actually stored.

    Falls back to "UTC" if the bridge can't be resolved (the caller's own
    _require_bridge call will already have raised 404 well before this
    matters in practice).
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    bridge: Any | None = get_influxdb1_bridge(gateway) if version == "1" else get_influxdb3_bridge(gateway)
    return getattr(bridge, "machine_timezone", "UTC") if bridge is not None else "UTC"


def _parse_local_datetime(value: str, tz_name: str) -> datetime:
    """
    Parses a <input type="datetime-local"> value ("YYYY-MM-DDTHH:MM[:SS]")
    from the Metrics Edit screen's date/time range picker into a tz-aware
    datetime, localized to tz_name (see _machine_timezone above).
    """
    try:
        naive: datetime = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{value}' is not a valid date/time.")
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def _require_bridge(request: Request, version: str) -> Any:
    """
    Resolves the live influxdb_out (v1) or influxdb3_out (v3) bridge for
    `version`, or raises 404.

    404 (not 503) because from the UI's point of view "no bridge attached"
    and "no such route" look the same — there's nothing for this screen to
    show either way, and the nav item that links here is itself hidden
    when this would fail (see influxdb1_bridge_available()/
    influxdb3_bridge_available() in base.html).
    """
    if version not in ("1", "3"):
        raise HTTPException(status_code=404, detail=f"Unknown InfluxDB version '{version}'.")
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    bridge: Any | None = get_influxdb1_bridge(gateway) if version == "1" else get_influxdb3_bridge(gateway)
    if bridge is None:
        _log.warning(f"No InfluxDB v{version} bridge is attached to this gateway.")
        raise HTTPException(status_code=404, detail=f"No InfluxDB v{version} bridge is attached to this gateway.")
    return bridge


# ---------------------------------------------------------------------------
# Metrics Edit — page shell + measurement/device/field pickers + preview +
# staging endpoints, shared by both InfluxDB versions. See
# services/influxdb_service.py's module docstring for the staging/commit
# story (an independent staging store from TimescaleDB's, applied by the
# same header "Commit All Changes" button via routers/commit.py).
# ---------------------------------------------------------------------------

@router.get("/pages/influxdb-metrics-edit/{version}", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_page(request: Request, version: str):
    """
    "Metrics Edit 1.x"/"Metrics Edit 3.x" screen — lists every measurement
    on the resolved bridge. Selecting one loads its device picker via HTMX
    (see influxdb_metrics_edit_devices_partial below), then its field
    picker once a device is chosen. The staged-changes panel is
    pre-populated here too, so navigating away and back doesn't lose
    anything already staged.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    try:
        measurements: list[str] = list_metric_edit_measurements(gateway, version)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # supports_delete: this InfluxDB version has a delete action at all.
    # delete_unavailable_reason: set when the connected server cannot perform it
    # (InfluxDB 3 Core) -- the page then shows "Delete value(s)" disabled with this text.
    delete_offered: bool = supports_delete(version)
    delete_block_reason: str | None = delete_unavailable_reason(gateway, version)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/influxdb_metrics_edit.html",
        context={
            **base_context(request, nav),
            "version": version,
            "measurements": measurements,
            "supports_delete": delete_offered,
            "delete_unavailable_reason": delete_block_reason,
            "delete_enabled": delete_offered and delete_block_reason is None,
            "staged_edits": get_staged_metric_edits(request.app.state),
        },
    )


@router.get("/pages/influxdb/metrics-edit/{version}/devices", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_devices_partial(request: Request, version: str, measurement: str):
    """
    Device picker (<option> list) for one Metrics Edit measurement selection, for either InfluxDB version.

    Plain `def`, not `async def`: the InfluxDB clients are blocking, and FastAPI runs
    a plain `def` route in its threadpool. An `async def` route calling them would
    stall the whole event loop (every other request on the UI) until the query returned.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    try:
        devices: list[dict[str, str | None]] = list_metric_edit_devices(gateway, version, measurement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        _log.exception("Metrics Edit device list failed (InfluxDB %s, measurement %s)", version, measurement)
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_devices.html",
        context={"version": version, "measurement": measurement, "devices": devices},
    )


@router.get("/pages/influxdb/metrics-edit/{version}/device-name", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_device_name(request: Request, version: str, measurement: str, device: str):
    """
    Friendly device_name for one picked device, as an escaped plain-text fragment ("" if none).

    InfluxDB v1's device list carries identifiers only (see
    InfluxV1AdminManager.list_metric_edit_devices), so the page fetches the
    name here after a device is chosen. Always "" for v3, whose list already has names.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    try:
        name: str | None = lookup_metric_edit_device_name(gateway, version, measurement, device)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        _log.exception("Metrics Edit device name lookup failed (InfluxDB %s, %s / %s)", version, measurement, device)
        raise HTTPException(status_code=500, detail=str(exc))

    return HTMLResponse(html.escape(name or ""))


@router.get("/pages/influxdb/metrics-edit/{version}/fields", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_fields_partial(request: Request, version: str, measurement: str):
    """Field picker for one Metrics Edit measurement selection, for either InfluxDB version (plain `def` -- see the devices route)."""
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    try:
        fields: list[dict[str, str | None]] = list_metric_edit_fields(gateway, version, measurement)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        _log.exception("Metrics Edit field list failed (InfluxDB %s, measurement %s)", version, measurement)
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_fields.html",
        context={"fields": fields},
    )


class InfluxMetricEditPreviewRequest(BaseModel):
    measurement: str
    device_identifier: str
    field_name: str
    start_time: str    # <input type="datetime-local"> value
    end_time: str


@router.post("/api/influxdb/metrics-edit/{version}/preview", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_preview(payload: InfluxMetricEditPreviewRequest, request: Request, version: str):
    """
    Read-only "Preview" step — shows the admin what a matching
    edit_metric_values() call would affect (row count + a small sample of
    current values) before anything is staged. Renders straight to HTML
    for the same HTMX-swap pattern routers/timescale.py uses.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    if not payload.field_name:
        raise HTTPException(status_code=400, detail="Select a field to preview.")

    tz_name: str = _machine_timezone(request, version)
    start_dt: datetime = _parse_local_datetime(payload.start_time, tz_name)
    end_dt: datetime = _parse_local_datetime(payload.end_time, tz_name)
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="End time must not be before start time.")

    try:
        preview: dict[str, Any] = preview_metric_edit(
            gateway, version, payload.measurement, payload.device_identifier, payload.field_name, start_dt, end_dt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_preview.html",
        context={"preview": preview},
    )


class InfluxMetricEditStageRequest(BaseModel):
    measurement: str
    device_identifier: str
    device_label: str
    field_name: str | None = None
    start_time: str    # <input type="datetime-local"> value
    end_time: str
    action: str        # "delete" (v1 only) or "set_value"
    new_value: str | None = None


@router.post("/api/influxdb/metrics-edit/{version}/stage", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_stage(payload: InfluxMetricEditStageRequest, request: Request, version: str):
    """
    Stages one InfluxDB Metrics Edit request ("Add to Staged Changes").
    Validates the replacement value against the field's reported type
    (see the admin managers' validate_metric_edit_value) before anything
    is added to staging — an invalid value, or "delete" requested for v3
    (see supports_delete()), is rejected here with a 400 rather than only
    surfacing at commit time. Nothing is written to InfluxDB until "Commit
    All Changes" (routers/commit.py) runs commit_staged_metric_edits().

    Returns the refreshed staged-changes list partial, for an HTMX swap.
    """
    _require_bridge(request, version)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    if payload.action not in ("delete", "set_value"):
        raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'.")
    if payload.action == "delete":
        if not supports_delete(version):
            raise HTTPException(status_code=400, detail="Deleting is not supported for InfluxDB v3.")
        block_reason: str | None = delete_unavailable_reason(gateway, version)
        if block_reason:
            raise HTTPException(status_code=400, detail=block_reason)
    if payload.action == "set_value":
        if not payload.field_name:
            raise HTTPException(status_code=400, detail="Select a field to edit.")
        if not payload.new_value:
            raise HTTPException(status_code=400, detail="Enter a replacement value.")

    tz_name: str = _machine_timezone(request, version)
    start_dt: datetime = _parse_local_datetime(payload.start_time, tz_name)
    end_dt: datetime = _parse_local_datetime(payload.end_time, tz_name)
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="End time must not be before start time.")

    try:
        stage_metric_edit(
            gateway,
            request.app.state,
            version=version,
            measurement=payload.measurement,
            device_identifier=payload.device_identifier,
            device_label=payload.device_label,
            field_name=payload.field_name,
            start_time=start_dt,
            end_time=end_dt,
            action=payload.action,
            new_value=payload.new_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )


@router.delete("/api/influxdb/metrics-edit/stage/{edit_id}", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_unstage(edit_id: str, request: Request):
    """
    Removes one staged InfluxDB Metrics Edit entry. Not version-scoped in
    the path — edit_id alone identifies the entry (staged v1 and v3
    entries share one store, see services/influxdb_service.py). Returns
    the refreshed staged-changes list partial for an HTMX swap.
    """
    unstage_metric_edit(request.app.state, edit_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )


@router.get("/pages/influxdb/metrics-edit/staged", response_class=HTMLResponse, response_model=None)
def influxdb_metrics_edit_staged_partial(request: Request):
    """Staged-changes list partial, used on either Metrics Edit page's initial load."""
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/influxdb_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )
