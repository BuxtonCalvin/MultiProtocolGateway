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

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from ..services.protocol_service import (
    get_protocol_registers,
    get_protocols_for_device,
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
):
    return get_protocol_registers(db, protocol_name, registry_type, page, page_size, device_name)


@router.get("/device/{protocol_version}/tabs")
def device_protocol_tabs(
    protocol_version: str,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> list[dict]:
    return get_protocols_for_device(db, protocol_version, device_name=device_name)

@router.get("/device/{slug}/counts")
def tab_counts(
    slug: str,
    protocol_name: str,
    registry_type: str,
    device_name: str | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """Return W/M/S counts for one tab — called after each toggle to refresh the display."""
    from ..models import DeviceProtocolSelection
    write_count = mask_count = screen_count = 0
    if device_name:
        sels = (
            db.query(DeviceProtocolSelection)
            .filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == protocol_name,
                DeviceProtocolSelection.registry_type == registry_type,
            )
            .all()
        )
        write_count: int  = sum(1 for s in sels if s.user_write_enabled)
        mask_count: int   = sum(1 for s in sels if s.mask_enabled)
        screen_count: int = sum(1 for s in sels if s.screen_enabled)
        _log.debug(f"write_count: {write_count}, mask_count: {mask_count},  screen_count: ", {screen_count})
    return {"write_count": write_count, "mask_count": mask_count, "screen_count": screen_count}


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
):
    result = toggle_register_field(db, register_id, payload.field, payload.value, device_name)
    if result is None:
        raise HTTPException(
            status_code=403,
            detail="Toggle not allowed — protocol write_mode is read-only, "
                   "or register not found."
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
def update_register_field(
    register_id: int,
    payload: FieldUpdateRequest,
    db: Session = Depends(get_session),
):
    result = update_protocol_register_field(db, register_id, payload.field, payload.value)
    if result is None:
        raise HTTPException(status_code=404, detail="Protocol register or field not found")
    db.commit()
    return {
        "id": result.id,
        "field": payload.field,
        "value": getattr(result, payload.field),
        "is_dirty": result.is_dirty,
    }
