# Description: routers/commit.py — Commit, diff, and backup endpoints.
# File: commit.py
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

"""routers/commit.py — Commit, diff, and backup endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config_writer import commit_all
from ..database import get_session, refresh_app_state
from ..diff_engine import build_diff
from ..models import DeviceProtocolSelection, ProtocolRegister, Setting
from ..services.backup_service import list_backups, rollback_to
from ..services.setting_description_service import (
    commit_descriptions,
    discard_descriptions,
)

_log: logging.Logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/commit", tags=["commit"])


@router.post("")
def do_commit(request: Request, db: Session = Depends(get_session)):
    """
    Full commit: backup → write config.cfg → write masks/screens/overrides →
    reset dirty flags.
    """
    state = request.app.state
    try:
        result = commit_all(
            db=db,
            config_path=state.config_path,
            project_root=state.project_root,
            protocols_dir=state.protocols_dir,
            config_dir=getattr(state, "config_dir", None),
        )
        desc_count = commit_descriptions(db)
        db.commit()
        result["descriptions_committed"] = desc_count
    except Exception as exc:
        _log.debug("descriptions not committed")
        raise HTTPException(status_code=500, detail=str(exc))
    else:
        return {"status": "ok", **result}


@router.get("/diff")
def diff(db: Session = Depends(get_session)):
    """Return structured diff of staged vs disk state."""
    result = build_diff(db)
    return {
        "summary": result.summary,
        "settings": [
            {
                "section": d.section,
                "key": d.key,
                "old_value": d.old_value,
                "new_value": d.new_value,
                "change_type": d.change_type,
            }
            for d in result.settings
        ],
        "protocols": [
            {
                "protocol_name": d.protocol_name,
                "registry_type": d.registry_type,
                "register_address": d.register_address,
                "variable_name": d.variable_name,
                "field": d.field,
                "old_value": d.old_value,
                "new_value": d.new_value,
            }
            for d in result.protocols
        ],
    }


@router.get("/backups")
def get_backups(db: Session = Depends(get_session)):
    backups = list_backups(db)
    return [
        {
            "id": b.id,
            "created_at": b.created_at.isoformat(),
            "filepath": b.filepath,
            "file_size_bytes": b.file_size_bytes,
            "trigger": b.trigger,
            "notes": b.notes,
        }
        for b in backups
    ]


@router.post("/discard")
def discard_changes(db: Session = Depends(get_session)):
    """
    Discard all staged changes: reset value_staged = value_disk and
    clear all is_dirty flags. Does NOT touch the config file on disk.
    """

    # Reset Setting rows
    dirty_settings = db.query(Setting).filter(Setting.is_dirty == True).all()  # noqa: E712
    for row in dirty_settings:
        row.value_staged = row.value_disk
        row.is_dirty = False

    # Reset ProtocolRegister dirty flags
    dirty_protocols = db.query(ProtocolRegister).filter(ProtocolRegister.is_dirty == True).all()  # noqa: E712
    for row in dirty_protocols:
        row.is_dirty = False

    # Reset DeviceProtocolSelection dirty flags
    dirty_selections = db.query(DeviceProtocolSelection).filter(DeviceProtocolSelection.is_dirty == True).all()  # noqa: E712
    for row in dirty_selections:
        row.is_dirty = False

    discard_descriptions(db)
    db.flush()
    refresh_app_state(db)
    db.commit()

    return {"status": "discarded"}


class RollbackRequest(BaseModel):
    backup_id: int


@router.post("/rollback")
def do_rollback(payload: RollbackRequest, request: Request, db: Session = Depends(get_session),) :
    """
    Restore config.cfg from a backup, then re-scan so the DB matches the
    restored file. The config file is treated as ground truth after rollback:
    value_staged is reset to value_disk for every setting so there are no
    stale staged edits left over from before the rollback.
    """

    success: bool = rollback_to(db, payload.backup_id, request.app.state.config_path)
    if not success:
        raise HTTPException(status_code=404, detail="Backup not found or file missing")

    # Re-scan so value_disk reflects the restored config.
    # set_cfg_is_truth ensures value_staged = value_disk (cfg is ground truth after rollback).
    try:
        request.app.state.scanner.set_cfg_is_truth(True)
        request.app.state.scanner.run()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback succeeded but re-scan failed: {exc}")

    refresh_app_state(db)
    db.commit()

    return {"status": "rolled_back", "backup_id": payload.backup_id}
