# Description: routers/timescale.py — TimescaleDB admin-menu pages and wide-table column administration endpoints.
# File: timescale.py
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
routers/timescale.py — TimescaleDB admin-menu pages and wide-table column
administration endpoints.

Everything here backs the "Timescale DB" admin menu — Delete Columns,
Rebuild Rollup Views, Rebuild Compression — as three page shells, their
read-only inventory partials, and the mutation endpoints those screens
call. Unlike most routers in this app, none of this takes a `db: Session`
for its live-bridge data — it comes from the live timescaledb bridge
transport reached via request.app.state.gateway (see services/
bridge_service.py), the same way analysis.py reaches modbus transports
for the Analyze feature. The page shells do open a `session_scope()` for
`get_nav_data()`, same as any other page route.

This is deliberately separate from routers/bridges.py, which owns the
passive "Bridges" pulldown / device-page panels (Bridge Health, Storage
Overview, Indexes, ...) — those never appear on an admin-menu screen and
never feed a mutation here. See routers/bridges.py's module docstring for
the other half of this split.

Checking/unchecking a column checkbox only stages the change in memory
(stage_field()); nothing is deleted until the admin presses the existing
"Commit All Changes" button, which calls commit_staged_deletions() from
routers/commit.py. Rollup and compression rebuilds have no staging step
at all — they run immediately on click, since rollup views and compressed
chunks are always derived/re-creatable from the wide/narrow tables, never
a source of truth.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ...transports.timescaledb import get_machine_timezone
from ...transports.transport_base import transport_base
from ..database import session_scope
from ..services.bridge_service import (
    get_staged_columns,
    get_staged_metric_edits,
    get_timescale_bridge,
    is_timescale_available,
    list_compression_groups,
    list_metric_edit_devices,
    list_metric_edit_fields,
    list_metric_edit_tables,
    list_rollup_view_groups,
    list_wide_table_fields,
    list_wide_tables,
    preview_metric_edit,
    rebuild_all_rollups,
    rebuild_compression,
    refresh_selected_rollups,
    resolve_wide_table_name,
    stage_field_deletion,
    stage_metric_edit,
    staged_deletion_count,
    unstage_metric_edit,
)
from ..services.device_service import NavData, get_nav_data
from .pages import base_context

if TYPE_CHECKING:
    # Deferred at runtime — importing protocol_gateway at module load time
    # risks a circular import, since it's what wires up the WebServer app
    # in the first place (see the same pattern in commit.py/devices.py/
    # bridges.py/pages.py). Only needed here, under TYPE_CHECKING, for the
    # annotations below.
    from protocol_gateway import Protocol_Gateway

_log: logging.Logger = logging.getLogger(__name__)

# No router-level prefix (unlike this file's previous /api/timescale-only
# incarnation) — this router now also owns the /pages/timescale-* page
# shells and /pages/timescale/* inventory partials alongside the
# /api/timescale/* mutation endpoints, so each route spells out its own
# full path instead.
router = APIRouter(tags=["timescale"])

# Local type for the three streaming-response generators below (rollup
# rebuild/refresh, compression rebuild). Each yields one NDJSON-encoded
# line per event; none is ever sent a value or has its return value used,
# so a plain Iterator[str] — not Generator[str, Any, None] — is the
# accurate (and simplest) annotation for them.
EventStream = Iterator[str]

# A number of endpoints below keep dict[str, Any]/list[dict[str, Any]]
# return-shaped locals rather than a tightened union. In every one of
# those cases the dict is a near-verbatim pass-through of a
# services/bridge_service.py introspection call (list_wide_table_fields,
# list_rollup_view_groups, list_compression_groups, ...) — a service
# module, out of this pass's "router modules" scope, and one that's
# reading genuinely heterogeneous data out of Postgres/TimescaleDB system
# catalogs (table names, byte sizes, timestamps, per-column type info)
# with no fixed shape this router file can assert without guessing at
# internals it doesn't own (see the identical note in bridges.py).


def _require_bridge(request: Request) -> transport_base:
    """
    Resolves the live timescaledb bridge or raises 404.

    404 (not 503) because from the UI's point of view "no bridge attached"
    and "no such route" look the same — there's nothing for this screen to
    show either way, and the nav pad that links here is itself hidden when
    this would fail (see timescale_bridge_available() in base.html).
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    bridge: transport_base | None = get_timescale_bridge(gateway)
    if bridge is None:
        _log.warning("No TimescaleDB bridge is attached to this gateway.")
        raise HTTPException(status_code=404, detail="No TimescaleDB bridge is attached to this gateway.")
    return bridge


# ---------------------------------------------------------------------------
# Delete Columns — page shell + inventory partials for the "Timescale DB ->
# Delete Columns" admin screen.
# ---------------------------------------------------------------------------

@router.get("/pages/timescale-delete-columns", response_class=HTMLResponse, response_model=None)
async def timescale_delete_columns_page(request: Request):
    """
    Step 4 of the Delete Columns flow — lists every wide table on the live
    TimescaleDB bridge. Selecting one loads its column checklist via HTMX
    (see timescale_fields_partial below).
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    try:
        wide_tables: list[dict[str, str]] = list_wide_tables(gateway)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/timescale_delete_columns.html",
        context={**base_context(request, nav), "wide_tables": wide_tables},
    )


@router.get("/pages/timescale/fields/{protocol_name}", response_class=HTMLResponse, response_model=None)
async def timescale_fields_partial(protocol_name: str, request: Request):
    """
    Step 5 of the Delete Columns flow — the alpha-ordered, checkbox-ready
    column list for one wide table. `checked` reflects whatever is
    currently staged for deletion, so navigating away and back doesn't
    lose the admin's selections before they commit.
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    staged: set[str] = get_staged_columns(request.app.state, protocol_name)
    try:
        fields: list[dict[str, Any]] = list_wide_table_fields(gateway, protocol_name, staged_columns=staged)
        wide_table_name: str = resolve_wide_table_name(gateway, protocol_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_field_checklist.html",
        context={
            "protocol_name": protocol_name,
            "wide_table_name": wide_table_name,
            "fields": fields,
        },
    )


class StageFieldRequest(BaseModel):
    checked: bool
    wide_table_name: str
    data_type: str = ""


@router.patch("/api/timescale/wide-tables/{protocol_name}/fields/{column_name}/stage")
def stage_field(
    protocol_name: str,
    column_name: str,
    payload: StageFieldRequest,
    request: Request,
    ) -> dict[str, str | bool | int]:
    """
    Stages or un-stages one column for deletion. This is a checkbox toggle
    only — no ALTER TABLE happens here. The actual delete + rollup rebuild
    happens later, in bulk, when the admin commits (see
    services/bridge_service.commit_staged_deletions).
    """
    _require_bridge(request)
    stage_field_deletion(
        request.app.state,
        protocol_name=protocol_name,
        wide_table_name=payload.wide_table_name,
        column_name=column_name,
        data_type=payload.data_type,
        checked=payload.checked,
    )
    _log.debug(f"Field staged for deletion: {column_name}")
    return {
        "protocol_name": protocol_name,
        "column_name": column_name,
        "checked": payload.checked,
        "staged_count": staged_deletion_count(request.app.state),
    }


# ---------------------------------------------------------------------------
# Metrics Edit — page shell + table/device/field pickers + preview +
# staging endpoints for the "Timescale DB -> Metrics Edit" admin screen.
#
# Edits/deletes existing metric VALUES for one device over a caller-chosen
# time range, on either the shared narrow table or one protocol's wide
# table — distinct from Delete Columns above, which only ever changes a
# wide table's SHAPE (dropping whole columns), never a value. Staged the
# same way as Delete Columns (nothing written to TimescaleDB until "Commit
# All Changes"), through an independent staging store (see
# stage_metric_edit / commit_staged_metric_edits in
# services/bridge_service.py) — a Metrics Edit selection is staged as one
# complete, atomic request rather than accumulated checkbox-by-checkbox,
# so there's a "stage" and "unstage by id" rather than a per-field toggle.
# ---------------------------------------------------------------------------

def _parse_local_datetime(value: str) -> datetime:
    """
    Parses a <input type="datetime-local"> value ("YYYY-MM-DDTHH:MM[:SS]")
    from the Metrics Edit screen's date/time range picker into a tz-aware
    datetime, localized to the app's configured machine timezone
    (get_machine_timezone()) — the same timezone _now_tz() stamps every
    row's m_time with when it's written, so a range picked in the browser
    lines up with what's actually stored.
    """
    try:
        naive: datetime = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{value}' is not a valid date/time.")
    return naive.replace(tzinfo=ZoneInfo(get_machine_timezone()))


@router.get("/pages/timescale-metrics-edit", response_class=HTMLResponse, response_model=None)
async def timescale_metrics_edit_page(request: Request)-> Any:
    """
    "Metrics Edit" screen — lists every selectable table (the shared
    narrow table first, then every wide-table protocol). Selecting one
    loads its device picker via HTMX (see
    timescale_metrics_edit_devices_partial below), then its field
    checklist (see timescale_metrics_edit_fields_partial) once a device is
    chosen. The staged-changes panel is pre-populated here too, so
    navigating away and back doesn't lose anything already staged.
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    try:
        tables: list[dict[str, str | None]] = list_metric_edit_tables(gateway)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/timescale_metrics_edit.html",
        context={
            **base_context(request, nav),
            "tables": tables,
            "staged_edits": get_staged_metric_edits(request.app.state),
        },
    )


@router.get("/pages/timescale/metrics-edit/devices", response_class=HTMLResponse, response_model=None)
async def timescale_metrics_edit_devices_partial(
    request: Request, table_kind: str, protocol_name: str | None = None
    ) -> Any:
    """Device picker (<option> list) for one Metrics Edit table selection."""
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(status_code=404, detail="No TimescaleDB bridge is attached to this gateway.")

    try:
        devices: list[dict[str, Any]] = list_metric_edit_devices(gateway, table_kind, protocol_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_metrics_edit_devices.html",
        context={"table_kind": table_kind, "protocol_name": protocol_name, "devices": devices},
    )


@router.get("/pages/timescale/metrics-edit/fields", response_class=HTMLResponse, response_model=None)
async def timescale_metrics_edit_fields_partial(
    request: Request,
    table_kind: str,
    protocol_name: str | None = None,
    device_info_id: int | None = None,
    ) -> Any:
    """Field checklist for one Metrics Edit table + device selection."""
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(status_code=404, detail="No TimescaleDB bridge is attached to this gateway.")

    try:
        fields: list[dict[str, str | None]] = list_metric_edit_fields(
            gateway, table_kind, protocol_name=protocol_name, device_info_id=device_info_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_metrics_edit_fields.html",
        context={"fields": fields},
    )


class MetricEditPreviewRequest(BaseModel):
    table_kind: str
    protocol_name: str | None = None
    device_info_id: int
    field_names: list[str]
    start_time: str    # <input type="datetime-local"> value
    end_time: str


@router.post("/api/timescale/metrics-edit/preview", response_class=HTMLResponse, response_model=None)
def timescale_metrics_edit_preview(payload: MetricEditPreviewRequest, request: Request):
    """
    Read-only "Preview" step — shows the admin what a matching
    edit_metric_values() call would affect (row count + a small sample of
    current values) before anything is staged. Renders straight to HTML
    for the same HTMX-swap pattern the rest of this file uses, rather than
    JSON, so the Metrics Edit screen needs no extra client-side
    templating.
    """
    _require_bridge(request)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    if not payload.field_names:
        raise HTTPException(status_code=400, detail="Select at least one field to preview.")

    start_dt: datetime = _parse_local_datetime(payload.start_time)
    end_dt: datetime = _parse_local_datetime(payload.end_time)
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="End time must not be before start time.")

    try:
        preview: dict[str, Any] = preview_metric_edit(
            gateway,
            payload.table_kind,
            payload.protocol_name,
            payload.device_info_id,
            payload.field_names,
            start_dt,
            end_dt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_metrics_edit_preview.html",
        context={"preview": preview},
    )


class MetricEditStageRequest(BaseModel):
    table_kind: str
    protocol_name: str | None = None
    table_name: str
    device_info_id: int
    device_label: str
    field_names: list[str]
    start_time: str    # <input type="datetime-local"> value
    end_time: str
    action: str        # "delete" or "set_value"
    new_value: str | None = None


@router.post("/api/timescale/metrics-edit/stage", response_class=HTMLResponse, response_model=None)
def timescale_metrics_edit_stage(payload: MetricEditStageRequest, request: Request):
    """
    Stages one Metrics Edit request ("Add to Staged Changes"). Validates
    the replacement value against the field's declared/inferred type
    (see BridgeAdminManager.validate_metric_edit_value, run from
    stage_metric_edit) before anything is added to staging — an invalid
    value (e.g. text into an INTEGER wide column) is rejected here with a
    400 rather than only surfacing at commit time. Nothing is written to
    TimescaleDB until "Commit All Changes" (routers/commit.py) runs
    commit_staged_metric_edits().

    Returns the refreshed staged-changes list partial, for an HTMX swap.
    """
    _require_bridge(request)
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)

    if not payload.field_names:
        raise HTTPException(status_code=400, detail="Select at least one field to edit.")
    if payload.action not in ("delete", "set_value"):
        raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'.")
    if payload.action == "set_value" and not payload.new_value:
        raise HTTPException(status_code=400, detail="Enter a replacement value.")

    start_dt: datetime = _parse_local_datetime(payload.start_time)
    end_dt: datetime = _parse_local_datetime(payload.end_time)
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="End time must not be before start time.")

    try:
        stage_metric_edit(
            gateway,
            request.app.state,
            table_kind=payload.table_kind,
            protocol_name=payload.protocol_name,
            table_name=payload.table_name,
            device_info_id=payload.device_info_id,
            device_label=payload.device_label,
            field_names=payload.field_names,
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
        name="partials/timescale_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )


@router.delete("/api/timescale/metrics-edit/stage/{edit_id}", response_class=HTMLResponse, response_model=None)
def timescale_metrics_edit_unstage(edit_id: str, request: Request):
    """Removes one staged Metrics Edit entry. Returns the refreshed staged-changes list partial for an HTMX swap."""
    unstage_metric_edit(request.app.state, edit_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )


@router.get("/pages/timescale/metrics-edit/staged", response_class=HTMLResponse, response_model=None)
def timescale_metrics_edit_staged_partial(request: Request):
    """Staged-changes list partial, used on the Metrics Edit page's initial load."""
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_metrics_edit_staged.html",
        context={"staged_edits": get_staged_metric_edits(request.app.state)},
    )


# ---------------------------------------------------------------------------
# Rollup views — page shell + inventory partial + mutation endpoints for
# the "Timescale DB -> Rebuild Rollup Views" screen. There's no staging
# step here: rollup views are always derived/re-creatable from the
# wide/narrow tables, never a source of truth, so a rebuild runs
# immediately on click.
# ---------------------------------------------------------------------------

@router.get("/pages/timescale-rebuild-rollups", response_class=HTMLResponse, response_model=None)
async def timescale_rebuild_rollups_page(request: Request):
    """
    "Rebuild Rollup Views" screen — a single-pane page (no left-hand picker,
    unlike Delete Columns: this screen acts on every rollup stack at once,
    not one wide table at a time). The view inventory itself loads via HTMX
    (see timescale_rollups_partial below); this route only renders the
    page shell + "Rebuild Rollups" button.
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/timescale_rebuild_rollups.html",
        context={**base_context(request, nav)},
    )


@router.get("/pages/timescale/rollups", response_class=HTMLResponse, response_model=None)
async def timescale_rollups_partial(request: Request):
    """
    Rollup-view inventory for the Rebuild Rollup Views screen, grouped one
    entry per source table — the shared narrow stack, plus every wide-table
    protocol's own hourly/daily/weekly/monthly stack — each group getting
    one "include in next rebuild" checkbox (see list_rollup_view_groups for
    why selection stops at the group level rather than per view). Loaded on
    page load and again after every "Rebuild Rollups" click (see
    timescale_rebuild_rollups.html).
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    try:
        groups: list[dict[str, Any]] = list_rollup_view_groups(gateway)
    except RuntimeError:
        # Bridge attached but not connected to TimescaleDB yet (rollup_mgr
        # not initialized) — render an empty inventory rather than a hard
        # error; the "load" trigger only fires once, so a transient empty
        # table beats a page that never finishes loading.
        groups = []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_rollup_view_list.html",
        context={"groups": groups},
    )


class RebuildRollupsRequest(BaseModel):
    # protocol_name values as returned by list_rollup_view_groups(), plus
    # the literal "shared_narrow" for the shared narrow stack — one entry
    # per checked group checkbox on the Rebuild Rollup Views screen.
    protocol_names: list[str]
    # False = "Rebuild Rollups" (only touches groups that are actually out
    # of date). True = "Force Rebuild" (purges + re-materializes every
    # selected group regardless of status).
    force: bool = False


@router.patch("/api/timescale/rollups/rebuild")
def rebuild_rollups(payload: RebuildRollupsRequest, request: Request) -> StreamingResponse:
    """
    Runs a rebuild pass across the admin's selected rollup group(s) — each
    group is one source table (the shared narrow stack, or one wide-table
    protocol), never an individual hourly/daily/weekly/monthly view; see
    RollupManager.rebuild_all_rollups for why selection stops at that
    granularity. `force` distinguishes "Rebuild Rollups" (only touch groups
    that are actually out of date) from "Force Rebuild" (always purge +
    re-materialize every selected group).

    STREAMS its result as newline-delimited JSON (media type
    application/x-ndjson), same convention as PATCH /compression/rebuild
    below: a "progress" event after each group finishes (coarser than
    compression's per-chunk events — see RollupManager.rebuild_all_rollups
    for why a whole group is the finest unit observable here), then one
    "done" event carrying the per-group result summary — including each
    group's `changed` flag, so the caller can tell "verified, already
    correct" apart from "actually rebuilt". The caller (the Rebuild Rollup
    Views screen) reports any ok=False entries in that summary to the
    admin rather than treating a partial failure as a 500.

    Bridge/rollup-manager errors raised before the first group are caught
    on the first iteration of the underlying generator and surfaced as an
    {"type": "error"} line rather than an HTTP error status, for the same
    reason PATCH /compression/rebuild does below — by the time any content
    has streamed, the response's status code is already committed.
    """
    _require_bridge(request)
    if not payload.protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one rollup group to rebuild.")

    protocol_names: set[str] = set(payload.protocol_names)
    force: bool = payload.force
    gateway: "Protocol_Gateway | None" = request.app.state.gateway

    def event_stream() -> EventStream:
        try:
            for event in rebuild_all_rollups(gateway, protocol_names=protocol_names, force=force):
                yield json.dumps(event) + "\n"
        except RuntimeError as exc:
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
        except Exception as exc:
            _log.error(f"Rollup rebuild stream failed: {exc}")
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

    _log.info(
        f"Rollup rebuild triggered via admin UI for {len(payload.protocol_names)} group(s) "
        f"(force={force})."
    )
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


class RefreshRollupsRequest(BaseModel):
    # Same group keys as RebuildRollupsRequest.protocol_names.
    protocol_names: list[str]
    # False = normal incremental refresh (each view's configured start_
    # offset window). True = full refresh of each view's entire time range.
    force_full: bool = False


@router.patch("/api/timescale/rollups/refresh")
def refresh_rollups(payload: RefreshRollupsRequest, request: Request) -> StreamingResponse:
    """
    Pulls the latest raw data into the admin's selected rollup group(s)'
    existing views — the lighter, non-structural "Refresh Now" action.
    Unlike /rollups/rebuild, this never drops or recreates a view; it's the
    same kind of refresh the background policy already runs on its own
    schedule, just triggered on demand. A view that doesn't exist yet is
    skipped, not created — use /rollups/rebuild for that.

    STREAMS its result as newline-delimited JSON, same convention as
    /rollups/rebuild above — a "progress" event per view (the finest
    granularity this screen's thermometer gets anywhere, since
    RollupManager.refresh_selected_rollups already loops one view at a
    time), then one "done" event with the per-view result summary.

    Bridge/rollup-manager errors raised before the first view are surfaced
    as an {"type": "error"} line rather than an HTTP error status, for the
    same reason /rollups/rebuild and /compression/rebuild do.
    """
    _require_bridge(request)
    if not payload.protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one rollup group to refresh.")

    protocol_names: set[str] = set(payload.protocol_names)
    force_full: bool = payload.force_full
    gateway: "Protocol_Gateway | None" = request.app.state.gateway

    def event_stream() -> EventStream:
        try:
            for event in refresh_selected_rollups(gateway, protocol_names=protocol_names, force_full=force_full):
                yield json.dumps(event) + "\n"
        except RuntimeError as exc:
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
        except Exception as exc:
            _log.error(f"Rollup refresh stream failed: {exc}")
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

    _log.info(
        f"Rollup refresh triggered via admin UI for {len(payload.protocol_names)} group(s) "
        f"(force_full={force_full})."
    )
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Rebuild Compression — page shell + inventory partial + mutation endpoint
# for the "Timescale DB -> Rebuild Compression" admin screen. Sibling of
# the Rollup Views endpoints above, using the same group keys and
# no-staging, run-immediately-on-click pattern. Fundamentally heavier than
# a rollup rebuild, though: rather than dropping/recreating a view's
# definition, this decompresses and recompresses every already-compressed
# chunk of a group's raw table and rollup views in place, so a
# compress_segmentby/compress_orderby change (or a Delete Columns edit)
# actually takes effect on historical data instead of only new chunks.
# ---------------------------------------------------------------------------

@router.get("/pages/timescale-rebuild-compression", response_class=HTMLResponse, response_model=None)
async def timescale_rebuild_compression_page(request: Request):
    """
    "Rebuild Compression" screen — sibling of Rebuild Rollup Views: a
    single-pane page (no left-hand picker) that acts on every compression
    group at once. The inventory itself loads via HTMX (see
    timescale_compression_partial below); this route only renders the
    page shell + action buttons.
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/timescale_rebuild_compression.html",
        context={**base_context(request, nav)},
    )


@router.get("/pages/timescale/compression", response_class=HTMLResponse, response_model=None)
async def timescale_compression_partial(request: Request):
    """
    Compression inventory for the Rebuild Compression screen, grouped one
    entry per source table — the shared narrow stack, plus every wide-
    table protocol's own raw table + rollup-view stack — each group
    getting one "include in next rebuild" checkbox, same grouping as
    timescale_rollups_partial above. Loaded on page load and again after
    every "Rebuild Compression" click (see timescale_rebuild_compression
    .html).
    """
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    if not is_timescale_available(gateway):
        raise HTTPException(
            status_code=404,
            detail="No TimescaleDB bridge is attached to this gateway.",
        )

    try:
        groups: list[dict[str, Any]] = list_compression_groups(gateway)
    except RuntimeError:
        # Bridge attached but not connected to TimescaleDB yet — render an
        # empty inventory rather than a hard error, same reasoning as
        # timescale_rollups_partial.
        groups = []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/timescale_compression_group_list.html",
        context={"groups": groups},
    )


class RebuildCompressionRequest(BaseModel):
    # protocol_name values as returned by list_compression_groups(), plus
    # the literal "shared_narrow" for the shared narrow stack — one entry
    # per checked group checkbox on the Rebuild Compression screen.
    protocol_names: list[str]


@router.patch("/api/timescale/compression/rebuild")
def rebuild_compression_endpoint(payload: RebuildCompressionRequest, request: Request) -> StreamingResponse:
    """
    Runs a full decompress -> recompress pass across every already-
    compressed chunk of the admin's selected group(s) — the raw table plus
    all four rollup views per group. Unlike /rollups/rebuild, there is no
    lighter "only touch what's out of date" variant here: every already-
    compressed chunk in a selected group is rewritten every time, since
    there's no cheap way to tell whether a given chunk already reflects
    the current compress_segmentby/compress_orderby/column settings short
    of decompressing it.

    STREAMS its result as newline-delimited JSON (one JSON object per
    line, media type application/x-ndjson) over a single long-lived
    response, rather than blocking silently until the whole rebuild
    finishes and returning one dict. Every line is one event from
    services/bridge_service.rebuild_compression: a "progress" event after
    each chunk (weighted by each table's pre-rebuild byte size — see
    RollupManager.rebuild_compression for why chunk COUNT alone would be
    misleading here, given how differently sized a raw table's chunks are
    from its own rollup views' chunks), and exactly one "done" event at
    the end carrying the same per-group/per-table/per-chunk result shape
    this endpoint used to return directly. The browser
    (triggerRebuildCompression in timescale_rebuild_compression.html)
    reads this incrementally to drive a real progress meter instead of
    the fixed-duration guess it used before — no background job or
    second polling endpoint needed, since the request just stays open for
    the duration of the rebuild.

    The one thing this trades away versus a background-job-plus-polling
    design: if the connection drops mid-run (tab closed, laptop sleeps),
    the rebuild keeps going server-side same as always, but there's no
    job ID to reattach to and watch it finish — nothing is an option here
    that a plain admin screen reasonably needs.

    Bridge/rollup-manager errors raised before the first chunk (no bridge
    attached, bridge not yet connected) are caught on the FIRST iteration
    of the underlying generator and surfaced as an {"type": "error"} line
    rather than an HTTP error status, since by the time any content has
    streamed the response's status code is already committed. A caller
    (the Rebuild Compression screen) checks for that line before treating
    the stream as if a "done" event is still coming.
    """
    _require_bridge(request)
    if not payload.protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one group to rebuild.")

    protocol_names: set[str] = set(payload.protocol_names)
    gateway: "Protocol_Gateway | None" = request.app.state.gateway

    def event_stream() -> EventStream:
        try:
            for event in rebuild_compression(gateway, protocol_names=protocol_names):
                yield json.dumps(event) + "\n"
        except RuntimeError as exc:
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
        except Exception as exc:
            _log.error(f"Compression rebuild stream failed: {exc}")
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

    _log.info(
        f"Compression rebuild triggered via admin UI for {len(payload.protocol_names)} group(s)."
    )
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
