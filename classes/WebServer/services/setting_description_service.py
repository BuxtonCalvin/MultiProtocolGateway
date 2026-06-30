# Description: services/setting_description_service.py
# File: setting_description_service.py
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

"""services/setting_description_service.py

Manages the setting_descriptions table — a consolidated, alphabetical registry
of every transport setting key, which transports use it, and a user-editable
description.

Descriptions are seeded from setting_descriptions.json (via transport_registry)
rather than from a hardcoded Python dict.  The JSON file is updated automatically
on every scan via sync_from_library(), so new keys added to transport modules
appear in the UI without any manual maintenance.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

from sqlalchemy.orm import Session

from ..models import SettingDescription
from ..scanner import scan_transport_library
from ..transport_registry import (
    get_setting_descriptions,
    sync_from_library,
    write_descriptions_to_json,
)

_log: logging.Logger = logging.getLogger(__name__)


def seed_setting_descriptions(db: Session, transports_dir: Path, purge_removed: bool = False) -> tuple[int, list[str]]:
    """
    Scan the transport library and populate setting_descriptions with all
    discovered keys.  Runs on every startup — updates the transports list for
    existing keys, inserts new ones.

    The JSON registry files are synced first so any newly discovered transport
    keys receive a description stub immediately, even before the user fills in
    the description via the UI.

    If purge_removed=True, rows for keys no longer found in any transport are
    deleted from the DB.

    Returns (count of rows inserted/updated, list of purged keys).
    """
    library: dict[str, dict[str, Any]] = scan_transport_library(transports_dir)

    # Keep transport_defaults.json and setting_descriptions.json current.
    # New keys get an empty description stub; existing entries are untouched.
    sync_from_library(library, write_defaults=True, write_descriptions=True, purge_removed=purge_removed)

    # Reload descriptions after the sync so newly added stubs are visible
    # in the same startup pass.
    descriptions: dict[str, str] = get_setting_descriptions()

    # Build: key → set of transport names that use it
    key_to_transports: dict[str, set[str]] = {}
    for transport_name, info in library.items():
        for key in info.get("keys", {}).keys():
            key_to_transports.setdefault(key, set()).add(transport_name)

    _log.info("seed_setting_descriptions: scanning %d transports", len(library))
    touched = 0

    for key, transport_set in sorted(key_to_transports.items()):
        transports_str: str = ", ".join(sorted(transport_set))
        existing: SettingDescription | None = (
            db.query(SettingDescription)
            .filter(SettingDescription.key == key)
            .first()
        )

        if existing:
            # Update transports list (may have changed as new transports are added)
            if existing.transports != transports_str:
                existing.transports = transports_str
                touched += 1
            # Backfill description from JSON if the DB row is still empty
            if not existing.description:
                seed_desc: str = descriptions.get(key, "")
                if seed_desc:
                    existing.description = seed_desc
                    existing.description_disk = seed_desc
                    touched += 1
        else:
            desc: str = descriptions.get(key, "")
            row = SettingDescription(
                key=key,
                transports=transports_str,
                description=desc,
                description_disk=desc,
                is_dirty=False,
            )
            db.add(row)
            touched += 1

    # Purge rows for keys no longer in any transport
    purged_keys: list[str] = []
    if purge_removed:
        current_keys: set[str] = set(key_to_transports.keys())
        stale_rows: List[SettingDescription] = (
            db.query(SettingDescription)
            .filter(~SettingDescription.key.in_(current_keys))
            .all()
        )
        for row in stale_rows:
            purged_keys.append(row.key)
            db.delete(row)
            _log.info("seed_setting_descriptions: purged removed key '%s'", row.key)

    db.commit()
    _log.info(
        "seed_setting_descriptions: %d rows inserted/updated, %d purged",
        touched, len(purged_keys),
    )
    return touched, purged_keys


def get_all_setting_descriptions(db: Session) -> list[SettingDescription]:
    return db.query(SettingDescription).order_by(SettingDescription.key).all()


def update_description(db: Session, setting_id: int, description: str) -> SettingDescription | None:
    row: SettingDescription | None = db.get(SettingDescription, setting_id)
    if not row:
        return None
    row.description = description
    row.mark_dirty()
    db.flush()
    return row


def discard_descriptions(db: Session) -> None:
    dirty: List[SettingDescription] = (
        db.query(SettingDescription)
        .filter(SettingDescription.is_dirty == True)  # noqa: E712
        .all()
    )
    for row in dirty:
        row.description = row.description_disk
        row.is_dirty = False
    db.flush()


def commit_descriptions(db: Session) -> int:
    """
    Commit all staged description edits:
      1. Flush description → description_disk in the DB and clear is_dirty.
      2. Write the full current descriptions map to setting_descriptions.json
         so the JSON file stays in sync with the DB without any manual steps.

    Writing the complete map (not just dirty rows) ensures the JSON always
    reflects the full authoritative state — it is safe to call on every
    global commit even if nothing changed.
    """
    dirty: List[SettingDescription] = (
        db.query(SettingDescription)
        .filter(SettingDescription.is_dirty == True)  # noqa: E712
        .all()
    )
    count: int = len(dirty)
    for row in dirty:
        row.description_disk = row.description
        row.is_dirty = False
    db.flush()

    # Write ALL current descriptions to JSON — not just the dirty rows.
    # This keeps setting_descriptions.json as a reliable distribution master
    # that developers can rely on without remembering to manually sync it.
    all_rows: List[SettingDescription] = db.query(SettingDescription).all()
    descriptions_map: dict[str, str] = {
        row.key: (row.description or "") for row in all_rows
    }
    try:
        write_descriptions_to_json(descriptions_map)
    except Exception as exc:
        _log.error("commit_descriptions: failed to write JSON file: %s", exc)
        # Do not re-raise — the DB commit succeeded; JSON is a convenience file.

    return count
