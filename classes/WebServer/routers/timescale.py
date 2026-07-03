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
services/timescale_service.py), the same way analysis.py reaches modbus
transports for the Analyze feature.

Checking/unchecking a column checkbox only stages the change in memory
(stage_field()); nothing is deleted until the admin presses the existing
"Commit All Changes" button, which calls commit_staged_deletions() from
routers/commit.py.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services.timescale_service import (
    get_all_staged,
    get_staged_columns,
    get_timescale_bridge,
    has_staged_deletions,
    list_wide_table_fields,
    list_wide_tables,
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
    services/timescale_service.commit_staged_deletions).
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
    return {
        "protocol_name": protocol_name,
        "column_name": column_name,
        "checked": payload.checked,
        "staged_count": staged_deletion_count(request.app.state),
    }


@router.get("/staged")
def get_staged(request: Request) -> dict[str, Any]:
    """Summary of everything currently staged for deletion, for a review/diff panel."""
    return {
        "has_staged": has_staged_deletions(request.app.state),
        "count": staged_deletion_count(request.app.state),
        "staged": get_all_staged(request.app.state),
    }
