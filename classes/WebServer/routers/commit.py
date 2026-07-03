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
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from classes.WebServer.diff_engine import DiffResult
from classes.WebServer.models import ConfigBackup

from ..config_writer import commit_all
from ..database import get_session, refresh_app_state
from ..diff_engine import build_diff
from ..models import DeviceProtocolSelection, ProtocolRegister, Setting
from ..services.backup_service import list_backups, rollback_to
from ..services.setting_description_service import (
    commit_descriptions,
    discard_descriptions,
)
from ..services.timescale_service import clear_staged_deletions, commit_staged_deletions

_log: logging.Logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/commit", tags=["commit"])


@router.post("")
def do_commit(request: Request, db: Session = Depends(get_session))-> dict[str, Any]:
    """
    Full commit: backup → write config.cfg → write masks/screens/overrides →
    reset dirty flags.
    """
    state = request.app.state
    try:
        result: dict[str, int | str] = commit_all(
            db=db,
            config_path=state.config_path,
            project_root=state.project_root,
            protocols_dir=state.protocols_dir,
            config_dir=getattr(state, "config_dir", None),
        )
        desc_count: int = commit_descriptions(db)
        db.commit()
        result["descriptions_committed"] = desc_count

        # Apply any staged TimescaleDB wide-table column deletions. This is
        # live Postgres schema work (drop rollups -> ALTER TABLE -> rebuild
        # rollups), not a config.cfg write, so it runs against the live
        # gateway rather than through config_writer.commit_all(). A failure
        # here is raised same as any other commit-step failure below; any
        # protocol that finished before the failure is already cleared from
        # staging (see commit_staged_deletions), so retrying the commit only
        # retries what's left.
        #
        # Kept in its own dict rather than folded into `result` — result is
        # typed dict[str, int | str] to match commit_all()'s return type,
        # and timescale_protocols_updated is a list[str], which doesn't fit
        # that value type.
        timescale_results: list[dict[str, Any]] = commit_staged_deletions(
            getattr(state, "gateway", None), state
        )
        timescale_summary: dict[str, int | list[str]] = {}
        if timescale_results:
            timescale_summary["timescale_columns_deleted"] = sum(len(r["deleted"]) for r in timescale_results)
            timescale_summary["timescale_protocols_updated"] = [r["protocol_name"] for r in timescale_results]

        # Recompute AppState dirty/orphan counts from the now-cleared flags so
        # the very next /api/devices/state poll (fired by base.html after the
        # commit response) sees zero dirty items and disables the commit button
        # without requiring a second press.
        refresh_app_state(db)
    except Exception as exc:
        # Was previously a hardcoded, debug-level "descriptions not
        # committed" message regardless of which step actually failed
        # (config write, descriptions, or a staged TimescaleDB column
        # deletion) -- misleading and, at debug level, invisible in most
        # deployments' default logging config. Log what actually failed,
        # at error level, so a commit failure is never silent.
        _log.error(f"do_commit: commit failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    else:
        return {"status": "ok", **result, **timescale_summary}


@router.get("/diff")
def diff(db: Session = Depends(get_session))-> dict[str, Any]:
    """Return structured diff of staged vs disk state."""
    result: DiffResult = build_diff(db)
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
def get_backups(db: Session = Depends(get_session))-> list[dict[str, Any]]:
    backups: List[ConfigBackup] = list_backups(db)
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
def discard_changes(request: Request, db: Session = Depends(get_session)) -> dict[str, str]:
    """
    Discard all staged changes: reset value_staged = value_disk and
    clear all is_dirty flags. Does NOT touch the config file on disk.
    Also clears any staged TimescaleDB column deletions — those are
    in-memory only, so nothing on disk or in Postgres needs reverting.
    """

    # Reset Setting rows
    dirty_settings: List[Setting] = db.query(Setting).filter(Setting.is_dirty == True).all()  # noqa: E712
    for row in dirty_settings:
        row.value_staged = row.value_disk
        row.is_dirty = False

    # Reset ProtocolRegister dirty flags
    dirty_protocols: List[ProtocolRegister] = db.query(ProtocolRegister).filter(ProtocolRegister.is_dirty == True).all()  # noqa: E712
    for row in dirty_protocols:
        row.is_dirty = False

    # Reset DeviceProtocolSelection dirty flags
    dirty_selections: List[DeviceProtocolSelection] = db.query(DeviceProtocolSelection).filter(DeviceProtocolSelection.is_dirty == True).all()  # noqa: E712
    for row in dirty_selections:
        row.is_dirty = False

    discard_descriptions(db)
    clear_staged_deletions(request.app.state)
    db.flush()
    refresh_app_state(db)
    db.commit()

    return {"status": "discarded"}


class RollbackRequest(BaseModel):
    backup_id: int


@router.post("/rollback")
def do_rollback(payload: RollbackRequest, request: Request, db: Session = Depends(get_session)) -> dict[str, Any]:
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
