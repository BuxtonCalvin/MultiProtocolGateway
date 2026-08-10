# Description: routers/protocols.py — Protocol register endpoints.
# File: protocols.py
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

"""routers/protocols.py — Protocol register endpoints."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row
from sqlalchemy.orm import Session

from classes.WebServer.models import DeviceProtocolSelection, ProtocolRegister

from ..database import get_session, session_scope
from ..services.protocol_service import (
    DeviceRegisterView,
    build_synthetic_rows,
    get_device_metric_summary,
    get_protocol_json,
    get_protocol_registers,
    get_protocols_for_device,
    register_row_sort_key,
    toggle_register_field,
    update_protocol_register_field,
)

_log: logging.Logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/protocols", tags=["protocols"])

@router.get("/{protocol_name}/{registry_type}")
def list_registers(
    protocol_name: str,
    registry_type: str,
    page: int = 1,
    page_size: int = 50,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    return get_protocol_registers(db, protocol_name, registry_type, page, page_size, device_name)


@router.get("/device/{protocol_version}/tabs")
def device_protocol_tabs(
    protocol_version: str,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """
    Returns every protocol tab for a device, each with its W/M/S counts
    already computed by get_protocols_for_device(). This is the single
    source of truth for those counts: pages.py calls the same function
    directly for the initial page render, and the client re-fetches this
    endpoint after a toggle to refresh the tab strip — so the two can
    never disagree.
    """
    return get_protocols_for_device(db, protocol_version, device_name=device_name)


@router.get("/device/{protocol_version}/metric-summary")
def device_metric_summary(
    protocol_version: str,
    device_name: str,
    request: Request,
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Returns the Available / Selected / Register-map-total counts shown next
    to the protocol tab strip (see get_device_metric_summary()). The client
    re-fetches this after every mask/screen toggle to keep the badges in
    sync — same source of truth used for the initial page render.
    """
    gateway: Any = getattr(request.app.state, "gateway", None)
    transport: Any = gateway.get_transport(f"transport.{device_name}") if gateway is not None else None
    return get_device_metric_summary(db, protocol_version, device_name, transport=transport)


class ToggleRequest(BaseModel):
    field: str
    value: bool


class FieldUpdateRequest(BaseModel):
    field: str
    value: str


class ProtocolRegisterResponse(BaseModel):
    id: int
    user_write_enabled: bool
    mask_enabled: bool
    screen_enabled: bool
    is_dirty: bool
    is_writable_by_protocol: bool

    # Crucial for SQLAlchemy properties
    model_config = ConfigDict(from_attributes=True)


@router.patch("/{register_id}/toggle", response_model=ProtocolRegisterResponse)
def toggle_register(
    register_id: int,
    payload: ToggleRequest,
    device_name: str | None = None,
    db: Session = Depends(get_session),
)-> dict[str, Any]:

    result: DeviceProtocolSelection | None = toggle_register_field(db, register_id, payload.field, payload.value, device_name)
    if result is None:
        raise HTTPException(
            status_code=403,
            detail="Toggle not allowed — no device_name given, protocol "
                   "write_mode is read-only, or register not found."
        )
    db.commit()
    return {
        "id": result.id,
        "user_write_enabled": result.user_write_enabled,
        "mask_enabled": result.mask_enabled,
        "screen_enabled": result.screen_enabled,
        "is_dirty": result.is_dirty,
        "is_writable_by_protocol": getattr(result, "is_writable_by_protocol", False),
    }

@router.patch("/{register_id}/field")
def update_register_field(register_id: int, payload: FieldUpdateRequest, db: Session = Depends(get_session))-> dict[str, Any]:
    result: ProtocolRegister | None = update_protocol_register_field(db, register_id, payload.field, payload.value)
    if result is None:
        raise HTTPException(status_code=404, detail="Protocol register or field not found")
    db.commit()
    return {
        "id": result.id,
        "field": payload.field,
        "value": getattr(result, payload.field),
        "is_dirty": result.is_dirty,
    }


# ---------------------------------------------------------------------------
# HTML partial routes
#
# Same /api/protocols prefix as the JSON endpoints above. `/table` and
# `/json` distinguish these from GET /{protocol_name}/{registry_type|
# (the JSON register list) — same resource, different representation.
# ---------------------------------------------------------------------------


@router.get("/{protocol_name}/{registry_type}/table", response_class=HTMLResponse, response_model=None)
async def protocol_table_partial(
    request: Request,
    protocol_name: str,
    registry_type: str,
    page: int = 1,
    device_name: str | None = None,
):
    """HTMX partial — register table rows, or JSON editor for json registry_type."""
    if registry_type == "json":
        # Look up protocol_group so we can find the .json file
        with session_scope() as db:
            row: Row[Tuple[str]] | None = (
                db.query(ProtocolRegister.protocol_group)
                .filter(ProtocolRegister.protocol_name == protocol_name)
                .first()
            )
        protocol_group: Any = row[0] if row else ""
        config_dir: Path = getattr(request.app.state, "config_dir")
        json_data, is_override = get_protocol_json(
            request.app.state.protocols_dir, protocol_group, protocol_name,
            config_dir=config_dir,
        )
        json_data: Any = json_data or {}
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="partials/json_editor.html",
            context={
                "protocol_name": protocol_name,
                "protocol_group": protocol_group,
                "json_data": json_data,
                "is_override": is_override,
            },
        )

    with session_scope() as db:
        data: dict[str, Any] = get_protocol_registers(
            db, protocol_name, registry_type, page, page_size=5000, device_name=device_name
        )

    # Append synthetic metric rows when rendering a device (scraper) view.
    # Synthetic rows are display-only — they have no DB row, no toggle
    # endpoints, and are never written to mask/screen files.  The transport
    # is looked up by name via the gateway so the metadata stays live.
    if device_name:
        gateway: Any = getattr(request.app.state, "gateway", None)
        if gateway is not None:
            transport: Any = gateway.get_transport(f"transport.{device_name}")
            if transport is not None:
                synthetic: List[DeviceRegisterView] = build_synthetic_rows(transport, registry_type=registry_type)
                if synthetic:
                    data["rows"] = list(data.get("rows", [])) + synthetic

    # Initial table order: synthetic metrics first, then anything with a
    # W/M/S checkbox selected, then everything else — alphabetical by
    # variable_name within each group. get_protocol_registers() itself still
    # orders by register_address (that's what pagination/offset math above
    # relies on); this re-sorts only the page actually being displayed.
    data["rows"] = sorted(data.get("rows", []), key=register_row_sort_key)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/protocol_table.html",
        context={
            "protocol_name": protocol_name,
            "registry_type": registry_type,
            "device_name": device_name,
            **data,
        },
    )


@router.post("/{protocol_group}/{protocol_name}/json", response_class=HTMLResponse, response_model=None)
async def save_protocol_json(request: Request, protocol_group: str, protocol_name: str) -> JSONResponse:
    """Save updated JSON config for a protocol directly to disk."""

    try:
        body: Any = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "detail": "Invalid JSON body"}, status_code=400)
    config_dir: Path = getattr(request.app.state, "config_dir", request.app.state.protocols_dir / protocol_group)
    config_dir.mkdir(parents=True, exist_ok=True)
    json_path: Path = config_dir / f"{protocol_name}.json"
    try:
        json_path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
    return JSONResponse({"status": "ok", "path": str(json_path)})
