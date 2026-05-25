# Description: routers/transport_settings.py — Transport Settings registry page endpoints.
# File: transport_settings.py
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

"""routers/transport_settings.py — Transport Settings registry page endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session, refresh_app_state
from ..models import SettingDescription
from ..scanner import scan_transport_library
from ..services.setting_description_service import (
    get_all_setting_descriptions,
    seed_setting_descriptions,
    update_description,
)

_log: logging.Logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transport-settings", tags=["transport-settings"])


class DescriptionUpdate(BaseModel):
    description: str


@router.get("")
def list_settings(db: Session = Depends(get_session)):
    rows = get_all_setting_descriptions(db)
    return [
        {
            "id": r.id,
            "key": r.key,
            "transports": r.transports,
            "description": r.description or "",
            "is_dirty": r.is_dirty,
        }
        for r in rows
    ]


@router.post("/scan")
def scan_for_new_settings(request: Request, db: Session = Depends(get_session)):
    """
    Re-scan the transport library and report any newly discovered setting keys
    that were not previously in the database. Returns a summary message.
    """
    transports_dir = request.app.state.transports_dir

    library = scan_transport_library(transports_dir)

    # Build current key → transports mapping from library
    key_to_transports: dict[str, set[str]] = {}
    for transport_name, info in library.items():
        for key in info.get("keys", {}).keys():
            key_to_transports.setdefault(key, set()).add(transport_name)

    # Find genuinely new keys (not yet in DB)
    existing_keys = {r.key for r in db.query(SettingDescription.key).all()}  # type: ignore[attr-defined]
    existing_keys = {r[0] for r in db.query(SettingDescription.key).all()}

    new_findings: list[dict] = []
    for key, transport_set in sorted(key_to_transports.items()):
        if key not in existing_keys:
            new_findings.append({
                "key": key,
                "transports": sorted(transport_set),
            })

    # Run the full seed: insert new rows, update transport lists, purge removed keys
    touched, purged_keys = seed_setting_descriptions(db, transports_dir, purge_removed=True)

    _log.info(
        "Transport settings scan complete: %d new keys, %d rows touched, %d keys purged",
        len(new_findings), touched, len(purged_keys)
    )

    return {
        "new_count": len(new_findings),
        "touched": touched,
        "new_findings": new_findings,
        "purged_count": len(purged_keys),
        "purged_keys": purged_keys,
    }


@router.patch("/{setting_id}")
def patch_description(
    setting_id: int,
    payload: DescriptionUpdate,
    db: Session = Depends(get_session),
):
    row = update_description(db, setting_id, payload.description)
    if not row:
        raise HTTPException(status_code=404, detail="Setting not found")
    refresh_app_state(db)
    db.commit()
    return {"id": row.id, "key": row.key, "description": row.description, "is_dirty": row.is_dirty}
