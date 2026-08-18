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
from typing import TYPE_CHECKING, Any, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row
from sqlalchemy.orm import Session

from classes.WebServer.models import DeviceProtocolSelection, ProtocolRegister

from ...transports.transport_base import transport_base
from ..database import get_session, session_scope
from ..services.protocol_service import (
    DeviceRegisterView,
    build_json_desc_rows,
    build_synthetic_rows,
    get_device_metric_summary,
    get_protocol_json,
    get_protocol_registers,
    get_protocols_for_device,
    materialize_and_toggle_virtual_metric,
    register_row_sort_key,
    toggle_register_field,
    update_protocol_register_field,
)

if TYPE_CHECKING:
    # Deferred at runtime — importing protocol_gateway at module load time
    # risks a circular import, since it's what wires up the WebServer app
    # in the first place (see the same pattern in commit.py/devices.py).
    # Only needed here, under TYPE_CHECKING, for the annotations below.
    from protocol_gateway import Protocol_Gateway

_log: logging.Logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/protocols", tags=["protocols"])

# A generic JSON value — used for the two spots in this file that read or
# write an arbitrary protocol .json config file's contents, where the
# actual shape is whatever's in that file (see save_protocol_json() /
# protocol_table_partial()'s "json" registry_type branch), not something
# this router defines or controls.
JSONValue = dict[str, "JSONValue"] | list["JSONValue"] | str | int | float | bool | None


@router.get("/{protocol_name}/{registry_type}")
def list_registers(
    protocol_name: str,
    registry_type: str,
    page: int = 1,
    page_size: int = 50,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    # dict[str, Any] kept: this passes straight through
    # get_protocol_registers()'s own return shape, which is
    # protocol_service.py's to define (a service module, not this router —
    # out of this pass's scope, same reasoning as commit_staged_deletions()
    # in commit.py).
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
    # dict[str, Any] kept: same reasoning as list_registers() above —
    # get_protocols_for_device()'s return shape belongs to protocol_service.py.
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
    # Return type kept dict[str, Any]: get_device_metric_summary()'s shape
    # belongs to protocol_service.py, same reasoning as above. gateway/
    # transport below are locally-owned and now precisely typed.
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    transport: transport_base | None = gateway.get_transport(f"transport.{device_name}") if gateway is not None else None
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
    request: Request,
    register_id: int,
    payload: ToggleRequest,
    device_name: str | None = None,
    db: Session = Depends(get_session),
)-> dict[str, int | bool]:

    result: DeviceProtocolSelection | None = toggle_register_field(db, register_id, payload.field, payload.value, device_name)
    if result is None:
        raise HTTPException(
            status_code=403,
            detail="Toggle not allowed — no device_name given, protocol "
                   "write_mode is read-only, or register not found."
        )

    # toggle_register_field's own cascade only covers the code -> desc
    # direction when the desc has *already* been materialized (it's
    # DB-only, so it can't discover a desc row that doesn't exist yet).
    # The reverse case — a plain CSV register being mask/screen-toggled for
    # the first time, whose JSON code-description companion has never been
    # selected before — needs the live transport (build_json_desc_rows) to
    # even know a desc companion exists, so it's resolved here rather than
    # in the DB-only service layer. Mirrors materialize_and_toggle_virtual_metric.
    if payload.field in ("mask_enabled", "screen_enabled") and device_name:
        source_row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
        if source_row is not None and not source_row.is_synthetic and not source_row.is_json_desc:
            gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
            transport: transport_base | None = gateway.get_transport(f"transport.{device_name}") if gateway is not None else None
            if transport is not None:
                for desc_row in build_json_desc_rows(transport, registry_type=source_row.registry_type):
                    if desc_row.source_variable_name == source_row.variable_name:
                        materialize_and_toggle_virtual_metric(
                            db, source_row.protocol_name, source_row.registry_type, device_name,
                            "json_desc", desc_row.variable_name, desc_row.documented_name,
                            desc_row.unit, desc_row.data_type, desc_row.note, desc_row.read_interval,
                            payload.field, payload.value,
                            source_variable_name=source_row.variable_name,
                        )
                        break  # a code register decodes to exactly one desc companion

    db.commit()
    return {
        "id": result.id,
        "user_write_enabled": result.user_write_enabled,
        "mask_enabled": result.mask_enabled,
        "screen_enabled": result.screen_enabled,
        "is_dirty": result.is_dirty,
        "is_writable_by_protocol": getattr(result, "is_writable_by_protocol", False),
    }

class VirtualToggleRequest(BaseModel):
    field: str
    value: bool
    source_variable_name: str | None = None
    kind: str                       # "synthetic" | "json_desc"
    variable_name: str
    documented_name: str = ""
    unit: str = ""
    data_type: str = ""
    note: str | None = None
    read_interval: str | None = None


@router.post("/{protocol_name}/{registry_type}/virtual-register/create-and-toggle", response_model=ProtocolRegisterResponse)
def create_and_toggle_virtual_register(
    protocol_name: str,
    registry_type: str,
    payload: VirtualToggleRequest,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> dict[str, int | bool]:
    """
    First-selection endpoint for a synthetic or JSON code-description
    metric — the register-row equivalent of POST .../settings/create-and-
    activate. Only used the first time a device selects a given virtual
    metric (DeviceRegisterView.id == -1 in the table); every toggle after
    that goes through PATCH /{register_id}/toggle like any other register,
    once that register has a real id. Safe to call repeatedly for the same
    metric/device (materialize_and_toggle_virtual_metric finds-or-creates
    by register_address), so a still-virtual-rendered row's other W/M/S
    checkboxes can keep posting here without needing to know a sibling
    checkbox already materialized the row.
    """
    result: DeviceProtocolSelection | None = materialize_and_toggle_virtual_metric(
        db, protocol_name, registry_type, device_name or "",
        payload.kind, payload.variable_name, payload.documented_name,
        payload.unit, payload.data_type, payload.note, payload.read_interval,
        payload.field, payload.value,
        source_variable_name=payload.source_variable_name,
    )
    if result is None:
        raise HTTPException(
            status_code=403,
            detail="Toggle not allowed — no device_name given, invalid kind/field, "
                   "or protocol write_mode is read-only."
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
    # dict[str, Any] kept: "value" below is getattr(result, payload.field) —
    # payload.field names an arbitrary ProtocolRegister column at runtime,
    # so its value type genuinely can't be known statically (could be a
    # register's bool/int/str/float column depending on what the caller asked
    # to read back). id/field/is_dirty are known (int/str/bool) but a dict's
    # value type is one union across all keys, so they inherit Any too.
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
        protocol_group: str = row[0] if row else ""
        config_dir: Path = getattr(request.app.state, "config_dir")
        json_data_raw, is_override = get_protocol_json(
            request.app.state.protocols_dir, protocol_group, protocol_name,
            config_dir=config_dir,
        )
        # get_protocol_json() (protocol_service.py) can return None as its
        # first tuple element (no json file found / failed to load), so its
        # declared type is dict[str, JSONValue] | None — using a separate
        # name here (json_data_raw) rather than annotating "json_data"
        # directly on the unpack line avoids redeclaring the same name with
        # a narrower (non-Optional) type, which Pyright rejects outright
        # regardless of how the None case is actually handled below.
        json_data: dict[str, JSONValue] = json_data_raw if json_data_raw is not None else {}
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
        # dict[str, Any] kept: same out-of-scope reasoning as list_registers().
        data: dict[str, Any] = get_protocol_registers(
            db, protocol_name, registry_type, page, page_size=5000, device_name=device_name
        )

    # Append synthetic metric rows and JSON code-description ("<name>_desc")
    # rows when rendering a device (scraper) view, for whichever of them
    # this device hasn't already selected — a materialized one comes back
    # from get_protocol_registers() above (real ProtocolRegister row, real
    # id, real toggle state) and would otherwise be duplicated by the live
    # builders below, which always return the *full* live set regardless of
    # DB state. The transport is looked up by name via the gateway so
    # anything not yet materialized stays live.
    if device_name:
        gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
        if gateway is not None:
            transport: transport_base | None = gateway.get_transport(f"transport.{device_name}")
            if transport is not None:
                already_materialized: set[str] = {
                    row.variable_name for row in data.get("rows", [])
                }

                synthetic: List[DeviceRegisterView] = build_synthetic_rows(
                    transport, registry_type=registry_type, exclude_names=already_materialized
                )
                if synthetic:
                    data["rows"] = list(data.get("rows", [])) + synthetic

                # Look up each _desc row's source register's address so it
                # displays next to the register it decodes, e.g. "27.b14",
                # rather than falling back to the source's variable_name.
                address_by_variable: dict[str, str] = {
                    row.variable_name: row.register_address
                    for row in data.get("rows", [])
                }
                json_desc: List[DeviceRegisterView] = build_json_desc_rows(
                    transport, registry_type=registry_type,
                    address_by_variable=address_by_variable, exclude_names=already_materialized,
                )
                if json_desc:
                    data["rows"] = list(data.get("rows", [])) + json_desc

    # Initial table order: synthetic metrics first, then JSON
    # code-description metrics, then anything with a W/M/S checkbox
    # selected, then everything else — alphabetical by variable_name within
    # each group. get_protocol_registers() itself still orders by
    # register_address (that's what pagination/offset math above relies
    # on); this re-sorts only the page actually being displayed.
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
        body: dict[str, JSONValue] = await request.json()
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
