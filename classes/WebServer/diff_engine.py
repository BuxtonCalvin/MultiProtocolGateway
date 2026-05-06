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

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from .models import ProtocolRegister, Setting

ChangeType = Literal["modified", "added", "removed", "orphan", "unchanged"]


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
    field: str                  # "user_write_enabled" | "mask_enabled" | "screen_enabled"
    old_value: bool
    new_value: bool
    change_type: ChangeType


@dataclass
class DiffResult:
    settings: list[SettingDiff] = field(default_factory=list)
    protocols: list[ProtocolDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.settings or self.protocols)

    @property
    def summary(self) -> dict[str, int]:
        from collections import Counter
        sc: Counter[str] = Counter(d.change_type for d in self.settings)
        pc: Counter[str] = Counter(d.change_type for d in self.protocols)
        return {
            "settings_modified": sc["modified"],
            "settings_added": sc["added"],
            "settings_removed": sc["removed"],
            "settings_orphaned": sc["orphan"],
            "protocols_modified": pc["modified"],
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
                    change_type="orphan",
                    is_orphan=True,
                ))
            continue

        if not row.is_dirty:
            continue

        disk = row.value_disk or ""
        staged = row.value_staged or ""

        if not disk and staged:
            change_type: ChangeType = "added"
        elif disk and not row.is_active:
            change_type = "removed"
        elif disk != staged:
            change_type = "modified"
        else:
            continue

        result.settings.append(SettingDiff(
            section=row.section,
            key=row.key,
            old_value=disk,
            new_value=staged,
            change_type=change_type,
        ))

    # ---- Protocol diff ----
    for row in db.query(ProtocolRegister).filter(ProtocolRegister.is_dirty == True).all():  # noqa: E712
        for field_name, disk_field, staged_field in (
            ("user_write_enabled", row.user_write_enabled_disk, row.user_write_enabled),
            ("mask_enabled", row.mask_enabled_disk, row.mask_enabled),
            ("screen_enabled", row.screen_enabled_disk, row.screen_enabled),
        ):
            if disk_field != staged_field:
                result.protocols.append(ProtocolDiff(
                    protocol_name=row.protocol_name,
                    registry_type=row.registry_type,
                    register_address=row.register_address,
                    variable_name=row.variable_name,
                    field=field_name,
                    old_value=disk_field,
                    new_value=staged_field,
                    change_type="modified",
                ))

    return result
