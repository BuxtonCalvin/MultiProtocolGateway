# Description: diff_engine.py — Compares the SQLite staging DB against the on-disk config.cfg and returns structured diff records for the visual diff panel.
# File: diff_engine.py
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
diff_engine.py — Compares the SQLite staging DB against the on-disk config.cfg
and returns structured diff records for the visual diff panel.

Change types
------------
  modified  — key exists in both, values differ
  added     — key is in DB (is_active=True) but not currently in config.cfg
  removed   — key is in config.cfg but marked is_active=False in DB
  orphan    — key in DB but no longer found in any transport class
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum

from sqlalchemy.orm import Session

from .models import ProtocolRegister, Setting


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    ORPHAN = "orphan"


@dataclass
class SettingDiff:
    section: str
    key: str
    old_value: str | None       # value_disk (what's on disk now)
    new_value: str | None       # value_staged (what will be committed)
    change_type: ChangeType
    is_orphan: bool = False


@dataclass
class ProtocolDiff:
    protocol_name: str
    registry_type: str
    register_address: str
    variable_name: str
    field: str                  # "user_write_enabled" | "mask_enabled" | "screen_enabled" | "pending_delete"
    old_value: bool
    new_value: bool
    change_type: ChangeType


@dataclass
class DiffResult:
    # Force the factory to explicitly return a list of the specific dataclass
    settings: list[SettingDiff] = dc_field(default_factory=lambda: list[SettingDiff]())
    protocols: list[ProtocolDiff] = dc_field(default_factory=lambda: list[ProtocolDiff]())


    @property
    def has_changes(self) -> bool:
        return bool(self.settings or self.protocols)

    @property
    def summary(self) -> dict[str, int]:
        from collections import Counter
        sc: Counter[str] = Counter(d.change_type for d in self.settings)
        pc: Counter[str] = Counter(d.change_type for d in self.protocols)
        return {
            "settings_modified": sc[ChangeType.MODIFIED],
            "settings_added": sc[ChangeType.ADDED],
            "settings_removed": sc[ChangeType.REMOVED],
            "settings_orphaned": sc[ChangeType.ORPHAN],
            "protocols_modified": pc[ChangeType.MODIFIED],
            "protocols_removed": pc[ChangeType.REMOVED],
            "total_changes": len(self.settings) + len(self.protocols),
        }


def build_diff(db: Session) -> DiffResult:
    """
    Build a full diff between staged DB state and on-disk state.
    Only returns rows that have actual changes.
    """
    result = DiffResult()

    # ---- Settings diff ----
    for row in db.query(Setting).all():
        if row.is_orphan:
            # Only show active orphans — inactive orphans are excluded from
            # config output anyway, so there is nothing to commit or action.
            if row.is_active:
                result.settings.append(SettingDiff(
                    section=row.section,
                    key=row.key,
                    old_value=row.value_disk,
                    new_value=row.value_staged,
                    change_type=ChangeType.ORPHAN,
                    is_orphan=True,
                ))
            continue

        if not row.is_dirty:
            continue

        disk: str = row.value_disk or ""
        staged: str = row.value_staged or ""

        if not disk and staged:
            change_type: ChangeType = ChangeType.ADDED
        elif disk and not row.is_active:
            change_type: ChangeType = ChangeType.REMOVED
        elif disk != staged:
            change_type: ChangeType = ChangeType.MODIFIED
        else:
            continue

        result.settings.append(SettingDiff(
            section=row.section,
            key=row.key,
            old_value=disk,
            new_value=staged,
            change_type=change_type,
        ))

    # ---- Protocol register diff: pending deletions ----
    # Deliberately scoped to pending_delete only, not every ProtocolRegister
    # .is_dirty field edit (variable_name, documented_name, etc.) — those
    # are already visible inline in the protocol table itself (dirty-row
    # highlight + "*" indicator, see protocol_table.html) as they're being
    # made, so duplicating each one here wouldn't add information. A staged
    # deletion is different: it's destructive and irreversible once
    # committed, unlike every other change in that table, so it gets its
    # own explicit, un-missable line in the pre-commit diff review.
    for row in db.query(ProtocolRegister).filter(ProtocolRegister.pending_delete == True).all():  # noqa: E712
        result.protocols.append(ProtocolDiff(
            protocol_name=row.protocol_name,
            registry_type=row.registry_type,
            register_address=row.register_address,
            variable_name=row.variable_name,
            field="pending_delete",
            old_value=False,
            new_value=True,
            change_type=ChangeType.REMOVED,
        ))

    return result
