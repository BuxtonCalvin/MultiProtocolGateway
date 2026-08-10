# Description: routers/timescale.py — TimescaleDB wide-table column administration endpoints.
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
routers/timescale.py — TimescaleDB wide-table column administration
endpoints.

These back the "Timescale DB → Delete Columns" admin screen. Unlike most
routers in this app, these do NOT take a `db: Session` — there's nothing in
the staging (SQLite) DB to read here. Everything comes from the live
timescaledb bridge transport reached via request.app.state.gateway (see
services/bridge_service.py), the same way analysis.py reaches modbus
transports for the Analyze feature.

Checking/unchecking a column checkbox only stages the change in memory
(stage_field()); nothing is deleted until the admin presses the existing
"Commit All Changes" button, which calls commit_staged_deletions() from
routers/commit.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services.bridge_service import (
    get_all_staged,
    get_background_jobs,
    get_compression_retention_summary,
    get_staged_columns,
    get_storage_overview,
    get_timescale_bridge,
    get_timescale_health,
    has_staged_deletions,
    list_compression_groups,
    list_rollup_views,
    list_wide_table_fields,
    list_wide_tables,
    rebuild_all_rollups,
    rebuild_compression,
    refresh_selected_rollups,
    stage_field_deletion,
    staged_deletion_count,
)

_log: logging.Logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/timescale", tags=["timescale"])


def _require_bridge(request: Request) -> Any:
    """
    Resolves the live timescaledb bridge or raises 404.

    404 (not 503) because from the UI's point of view "no bridge attached"
    and "no such route" look the same — there's nothing for this screen to
    show either way, and the nav pad that links here is itself hidden when
    this would fail (see timescale_bridge_available() in base.html).
    """
    gateway: Any | None = getattr(request.app.state, "gateway", None)
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        _log.warning("No TimescaleDB bridge is attached to this gateway.")
        raise HTTPException(status_code=404, detail="No TimescaleDB bridge is attached to this gateway.")
    return bridge


@router.get("/wide-tables")
def get_wide_tables(request: Request) -> list[dict[str, str]]:
    """Returns [{protocol_name, wide_table_name}] for the wide-table picker."""
    _require_bridge(request)
    try:
        return list_wide_tables(request.app.state.gateway)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/wide-tables/{protocol_name}/fields")
def get_wide_table_fields(protocol_name: str, request: Request) -> dict[str, Any]:
    """Returns the alpha-ordered, checkbox-ready column list for one wide table."""
    _require_bridge(request)
    staged: set[str] = get_staged_columns(request.app.state, protocol_name)
    try:
        fields: list[dict[str, Any]] = list_wide_table_fields(
            request.app.state.gateway, protocol_name, staged_columns=staged
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"protocol_name": protocol_name, "fields": fields}


class StageFieldRequest(BaseModel):
    checked: bool
    wide_table_name: str
    data_type: str = ""


@router.patch("/wide-tables/{protocol_name}/fields/{column_name}/stage")
def stage_field(
    protocol_name: str,
    column_name: str,
    payload: StageFieldRequest,
    request: Request,
) -> dict[str, Any]:
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


@router.get("/staged")
def get_staged(request: Request) -> dict[str, Any]:
    """Summary of everything currently staged for deletion, for a review/diff panel."""
    _log.debug("get_staged() called")
    return {
        "has_staged": has_staged_deletions(request.app.state),
        "count": staged_deletion_count(request.app.state),
        "staged": get_all_staged(request.app.state),
    }


# ---------------------------------------------------------------------------
# Rollup views — back the "Timescale DB -> Rebuild Rollup Views" screen.
# Unlike the column-deletion endpoints above, there's no staging step here:
# rollup views are always derived/re-creatable from the wide/narrow tables,
# never a source of truth, so a rebuild runs immediately on click.
# ---------------------------------------------------------------------------

@router.get("/rollups")
def get_rollups(request: Request) -> dict[str, Any]:
    """Returns the current rollup-view inventory for the Rebuild Rollup Views screen."""
    _require_bridge(request)
    try:
        views: list[dict[str, Any]] = list_rollup_views(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"views": views}


class RebuildRollupsRequest(BaseModel):
    # protocol_name values as returned by list_rollup_view_groups(), plus
    # the literal "shared_narrow" for the shared narrow stack — one entry
    # per checked group checkbox on the Rebuild Rollup Views screen.
    protocol_names: list[str]
    # False = "Rebuild Rollups" (only touches groups that are actually out
    # of date). True = "Force Rebuild" (purges + re-materializes every
    # selected group regardless of status).
    force: bool = False


@router.patch("/rollups/rebuild")
def rebuild_rollups(payload: RebuildRollupsRequest, request: Request) -> dict[str, Any]:
    """
    Runs a rebuild pass across the admin's selected rollup group(s) — each
    group is one source table (the shared narrow stack, or one wide-table
    protocol), never an individual hourly/daily/weekly/monthly view; see
    RollupManager.rebuild_all_rollups for why selection stops at that
    granularity. `force` distinguishes "Rebuild Rollups" (only touch groups
    that are actually out of date) from "Force Rebuild" (always purge +
    re-materialize every selected group). Runs synchronously and returns a
    per-group result summary — including each group's `changed` flag — so
    the caller can tell "verified, already correct" apart from "actually
    rebuilt"; the caller (the Rebuild Rollup Views screen) also reports any
    ok=False entries to the admin rather than treating a partial failure as
    a 500.
    """
    _require_bridge(request)
    if not payload.protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one rollup group to rebuild.")
    try:
        result: dict[str, Any] = rebuild_all_rollups(
            request.app.state.gateway,
            protocol_names=set(payload.protocol_names),
            force=payload.force,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _log.info(
        f"Rollup rebuild triggered via admin UI for {len(payload.protocol_names)} group(s) "
        f"(force={payload.force})."
    )
    return result


class RefreshRollupsRequest(BaseModel):
    # Same group keys as RebuildRollupsRequest.protocol_names.
    protocol_names: list[str]
    # False = normal incremental refresh (each view's configured start_
    # offset window). True = full refresh of each view's entire time range.
    force_full: bool = False


@router.patch("/rollups/refresh")
def refresh_rollups(payload: RefreshRollupsRequest, request: Request) -> dict[str, Any]:
    """
    Pulls the latest raw data into the admin's selected rollup group(s)'
    existing views — the lighter, non-structural "Refresh Now" action.
    Unlike /rollups/rebuild, this never drops or recreates a view; it's the
    same kind of refresh the background policy already runs on its own
    schedule, just triggered on demand. A view that doesn't exist yet is
    skipped, not created — use /rollups/rebuild for that. Runs
    synchronously and returns a per-view result summary.
    """
    _require_bridge(request)
    if not payload.protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one rollup group to refresh.")
    try:
        result: dict[str, Any] = refresh_selected_rollups(
            request.app.state.gateway,
            protocol_names=set(payload.protocol_names),
            force_full=payload.force_full,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _log.info(
        f"Rollup refresh triggered via admin UI for {len(payload.protocol_names)} group(s) "
        f"(force_full={payload.force_full})."
    )
    return result


# ---------------------------------------------------------------------------
# Rebuild Compression — back the "Timescale DB -> Rebuild Compression"
# admin screen. Sibling of the Rollup Views endpoints above, using the same
# group keys and no-staging, run-immediately-on-click pattern. Fundamentally
# heavier than a rollup rebuild, though: rather than dropping/recreating a
# view's definition, this decompresses and recompresses every already-
# compressed chunk of a group's raw table and rollup views in place, so a
# compress_segmentby/compress_orderby change (or a Delete Columns edit)
# actually takes effect on historical data instead of only new chunks.
# ---------------------------------------------------------------------------

@router.get("/compression/groups")
def get_compression_groups(request: Request) -> dict[str, Any]:
    """Returns the current per-group compression inventory for the Rebuild Compression screen."""
    _require_bridge(request)
    try:
        groups: list[dict[str, Any]] = list_compression_groups(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"groups": groups}


class RebuildCompressionRequest(BaseModel):
    # protocol_name values as returned by list_compression_groups(), plus
    # the literal "shared_narrow" for the shared narrow stack — one entry
    # per checked group checkbox on the Rebuild Compression screen.
    protocol_names: list[str]


@router.patch("/compression/rebuild")
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
    gateway: Any = request.app.state.gateway

    def event_stream():
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


# ---------------------------------------------------------------------------
# Bridge info panels — back the read-only sections of the TimescaleDB
# bridge's device page (see partials/bridge_panes.html). Unlike everything
# above, these never mutate anything; the panels are observed only, never
# acted on directly — actions live on the Timescale DB menu pads instead.
# ---------------------------------------------------------------------------

@router.get("/health")
def get_health(request: Request) -> dict[str, Any]:
    """Connection/background-worker snapshot for the Bridge Health panel."""
    _require_bridge(request)
    try:
        return get_timescale_health(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/storage")
def get_storage(request: Request) -> dict[str, Any]:
    """Per-source-table storage snapshot for the Storage Overview panel."""
    _require_bridge(request)
    try:
        tables: list[dict[str, Any]] = get_storage_overview(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"tables": tables}


@router.get("/compression-retention")
def get_compression_retention(request: Request) -> dict[str, Any]:
    """Compression/retention configuration summary for that panel."""
    _require_bridge(request)
    try:
        return get_compression_retention_summary(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/jobs")
def get_jobs(request: Request) -> dict[str, Any]:
    """TimescaleDB background scheduler job snapshot for that panel."""
    _require_bridge(request)
    try:
        jobs: list[dict[str, Any]] = get_background_jobs(request.app.state.gateway)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"jobs": jobs}
