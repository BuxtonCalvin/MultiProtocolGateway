# Description: services/protocol_service.py — Protocol register queries and toggle mutations.
# File: protocol_service.py
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
services/protocol_service.py — Protocol register queries and toggle mutations.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.engine.row import Row
from sqlalchemy.orm import Query, Session

from ..database import refresh_app_state
from ..models import DeviceProtocolSelection, ProtocolRegister, RegisterToggleTarget

_log: logging.Logger = logging.getLogger(__name__)


@dataclass
class DeviceRegisterView:
    id: int
    protocol_name: str
    registry_type: str
    register_address: str
    variable_name: str
    documented_name: str
    unit: str | None
    data_type: str | None
    values_range: str | None
    adjustments: str | None
    note: str | None
    read_interval: str | None
    write_mode_protocol: str
    user_write_enabled: bool
    mask_enabled: bool
    screen_enabled: bool
    is_dirty: bool
    # Paired-register fields — populated when this row is the merged stem of
    # a _l/_h pair.  paired_high_address holds the _h register address so the
    # UI can render the range "40-41" and show the expand/collapse detail rows.
    paired_high_address: str | None = None

    @property
    def is_paired(self) -> bool:
        """True when this row represents a merged _l/_h register pair."""
        return bool(self.paired_high_address)

    @property
    def is_writable_by_protocol(self) -> bool:
        return self.write_mode_protocol in ("RW", "W", "WO", "WRITE", "R/W")


def _safe_paired_address(row: Any) -> str | None:
    """
    Safely read paired_high_address from a ProtocolRegister ORM row.

    SQLAlchemy raises InvalidRequestError (not AttributeError) when accessing
    a mapped attribute that doesn't exist as a column in the current DB schema.
    Python's getattr(obj, name, default) only catches AttributeError, so it
    would re-raise here.  We first try the instance __dict__ directly to bypass
    any descriptor magic, then fall back to attribute access, swallowing all
    exceptions until the migration adds the column.
    """
    # Fast path: check instance dict directly, bypassing SQLAlchemy descriptors
    instance_state = getattr(row, "__dict__", {})
    if "paired_high_address" in instance_state:
        return instance_state["paired_high_address"]
    # Slow path: attempt instrumented access, catch anything SQLAlchemy raises
    try:
        val = row.paired_high_address  # type: ignore[union-attr]
        return val
    except Exception:
        return None


def get_protocol_registers(
    db: Session,
    protocol_name: str,
    registry_type: str,
    page: int = 1,
    page_size: int = 50,
    device_name: str | None = None,
) -> dict[str, Any]:
    """
    Returns a paginated list of ProtocolRegister rows for a given
    protocol_name and registry_type (input | holding | coil | discrete | json).
    """
    query: Query[ProtocolRegister] = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
        )
        .order_by(ProtocolRegister.register_address)
    )

    total: int = query.count()
    _log.debug("get_protocol_registers: %s/%s page=%d total=%d", protocol_name, registry_type, page, total)
    protocol_rows: List[ProtocolRegister] = query.offset((page - 1) * page_size).limit(page_size).all()

    rows: list[DeviceRegisterView]

    if device_name:
        selections: dict[tuple[str, str, str], DeviceProtocolSelection] = {
            (row.protocol_name, row.registry_type, row.register_address): row
            for row in (
                db.query(DeviceProtocolSelection)
                .filter(
                    DeviceProtocolSelection.device_name == device_name,
                    DeviceProtocolSelection.protocol_name == protocol_name,
                    DeviceProtocolSelection.registry_type == registry_type,
                )
                .all()
            )
        }
    else:
        selections = {}

    view_rows: list[DeviceRegisterView] = []
    for row in protocol_rows:
        try:
            s: DeviceProtocolSelection | None = selections.get(
                (row.protocol_name, row.registry_type, row.register_address)
            )
            view_rows.append(
                DeviceRegisterView(
                    id=row.id,
                    protocol_name=row.protocol_name,
                    registry_type=row.registry_type,
                    register_address=row.register_address,
                    variable_name=row.variable_name,
                    documented_name=row.documented_name,
                    unit=row.unit,
                    data_type=row.data_type,
                    values_range=row.values_range,
                    adjustments=row.adjustments,
                    note=row.note,
                    read_interval=row.read_interval,
                    write_mode_protocol=row.write_mode_protocol,
                    user_write_enabled=s.user_write_enabled if s else False,
                    mask_enabled=s.mask_enabled if s else False,
                    screen_enabled=s.screen_enabled if s else False,
                    is_dirty=s.is_dirty if s else False,
                    paired_high_address=_safe_paired_address(row),
                )
            )
        except Exception as exc:
            _log.warning(
                "Skipping register row id=%s variable=%s in get_protocol_registers: %s",
                getattr(row, "id", "?"),
                getattr(row, "variable_name", "?"),
                exc,
            )
    rows = view_rows

    return {
        "protocol_name": protocol_name,
        "registry_type": registry_type,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": rows,
    }


def get_protocols_for_device(
    db: Session, protocol_version: str, device_name: str | None = None
) -> list[dict]:
    """
    Given a protocol_version string (e.g. "eg4_18kpv"),
    returns the available registry_types (tabs) for that device,
    including W/M/S selection counts when device_name is provided.
    """
    rows: Sequence[Row[Tuple[str, str]]] = (
        db.execute(
            select(
                ProtocolRegister.protocol_name,
                ProtocolRegister.registry_type,
            )
            .where(ProtocolRegister.protocol_name.like(f"{protocol_version}%"))
            .distinct()
            .order_by(ProtocolRegister.registry_type)
        )
        .all()
    )

    tabs = []
    for r in rows:
        protocol_name, registry_type = r[0], r[1]

        write_count = mask_count = screen_count = 0

        if device_name:
            sels: List[DeviceProtocolSelection] = (
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

        tabs.append({
            "protocol_name": protocol_name,
            "registry_type": registry_type,
            "write_count":   write_count,
            "mask_count":    mask_count,
            "screen_count":  screen_count,
        })
    return tabs


def toggle_register_field(
    db: Session,
    register_id: int,
    field: str,   # "user_write_enabled" | "mask_enabled" | "screen_enabled"
    value: bool,
    device_name: str | None = None,
) -> ProtocolRegister | DeviceProtocolSelection | None:
    """
    Toggle a single field on a ProtocolRegister row.
    Enforces the two-gate rule: user_write_enabled can only be True
    if the protocol permits writing.
    Returns the updated row, or None if not found / not allowed.
    """
    allowed_fields: set[str] = {"user_write_enabled", "mask_enabled", "screen_enabled"}
    if field not in allowed_fields:
        return None

    row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
    if row is None:
        return None

    target: RegisterToggleTarget = row

    if device_name:
        existing: DeviceProtocolSelection | None = (
            db.query(DeviceProtocolSelection)
            .filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == row.protocol_name,
                DeviceProtocolSelection.registry_type == row.registry_type,
                DeviceProtocolSelection.register_address == row.register_address,
            )
            .first()
        )
        if existing is None:
            existing = DeviceProtocolSelection(
                device_name=device_name,
                protocol_name=row.protocol_name,
                registry_type=row.registry_type,
                register_address=row.register_address,
                user_write_enabled=False,
                mask_enabled=False,
                screen_enabled=False,
                user_write_enabled_disk=False,
                mask_enabled_disk=False,
                screen_enabled_disk=False,
                is_dirty=False,
            )
            db.add(existing)
            db.flush()
        target = existing

    if field == "user_write_enabled" and value and not row.is_writable_by_protocol:
        return None

    setattr(target, field, value)
    # Mask and screen are mutually exclusive for a register.
    if field == "mask_enabled" and value:
        target.screen_enabled = False
    elif field == "screen_enabled" and value:
        target.mask_enabled = False
    target.mark_dirty()
    db.flush()
    refresh_app_state(db)
    _log.debug("toggle_register_field: register=%d field=%s value=%s device=%s", register_id, field, value, device_name)
    return target  # type: ignore[return-value]  # concrete type is ProtocolRegister | DeviceProtocolSelection


def update_protocol_register_field(
    db: Session,
    register_id: int,
    field: str,
    value: str,
) -> ProtocolRegister | None:
    allowed_fields: set[str] = {
        "variable_name",
        "documented_name",
        "unit",
        "data_type",
        "values_range",
        "note",
        "read_interval",
        "write_mode_protocol",
    }
    if field not in allowed_fields:
        return None

    row: ProtocolRegister | None = db.get(ProtocolRegister, register_id)
    if row is None:
        return None

    setattr(row, field, value)
    row.is_dirty = True
    db.flush()
    refresh_app_state(db)
    return row


def get_protocol_json(
    protocols_dir: Path,
    protocol_group: str,
    protocol_name: str,
    config_dir: Path | None = None,
) -> tuple[dict | None, bool]:
    """
    Load the JSON config file for a protocol.
    Checks config_dir first (user override), then falls back to protocols_dir.
    Returns (data, is_override) where is_override=True means a modified copy exists.
    """
    # Check config override first
    if config_dir is not None:
        override_path: Path = config_dir / f"{protocol_name}.json"
        if override_path.exists():
            try:
                _log.debug("get_protocol_json: loading override from %s", override_path)
                return json.loads(override_path.read_text(encoding="utf-8")), True
            except Exception:
                _log.warning("Failed to load protocol json override file %s", override_path)
                # Fix 5: add the missing return so the checker sees a complete
                # set of return paths and --warn-return-type is satisfied.
                return None, False

    json_path: Path = protocols_dir / protocol_group / f"{protocol_name}.json"
    if json_path.exists():
        try:
            _log.debug("get_protocol_json: loading default from %s", json_path)
            return json.loads(json_path.read_text(encoding="utf-8")), False
        except Exception:
            _log.warning("Failed to load protocol json file %s", json_path)
            return None, False
    _log.debug("get_protocol_json: no json file found for %s/%s", protocol_group, protocol_name)
    return None, False


def export_protocol_registers(
    db: Session,
    protocol_name: str,
    registry_type: str,
    device_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return ALL registers for a protocol/registry_type as a flat list of dicts
    suitable for CSV or JSON export.  Unlike get_protocol_registers this is
    unpaginated and always returns every row.

    When device_name is supplied the W/M/S selections for that device are
    merged in, matching the device view in the table.  Paired-register rows
    include both the logical stem address and the paired high address so the
    exported file documents the full physical address span.
    """
    protocol_rows: list[ProtocolRegister] = (
        db.query(ProtocolRegister)
        .filter(
            ProtocolRegister.protocol_name == protocol_name,
            ProtocolRegister.registry_type == registry_type,
        )
        .order_by(ProtocolRegister.register_address)
        .all()
    )

    selections: dict[tuple[str, str, str], DeviceProtocolSelection] = {}
    if device_name:
        selections = {
            (r.protocol_name, r.registry_type, r.register_address): r
            for r in db.query(DeviceProtocolSelection).filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == protocol_name,
                DeviceProtocolSelection.registry_type == registry_type,
            ).all()
        }

    result: list[dict[str, Any]] = []
    for row in protocol_rows:
        paired_high: str | None = _safe_paired_address(row)
        # Address column: show range "40-41" for paired rows, plain address otherwise
        address_display: str = (
            f"{row.register_address}-{paired_high}" if paired_high
            else str(row.register_address)
        )

        entry: dict[str, Any] = {
            "register_address":   address_display,
            "variable_name":      row.variable_name,
            "documented_name":    row.documented_name,
            "unit":               row.unit or "",
            "data_type":          row.data_type or "",
            "values_range":       row.values_range or "",
            "write_mode_protocol": row.write_mode_protocol,
            "adjustments":        row.adjustments or "",
            "note":               row.note or "",
            "read_interval":      row.read_interval or "",
            "is_paired_register": bool(paired_high),
        }

        if device_name:
            s: DeviceProtocolSelection | None = selections.get(
                (row.protocol_name, row.registry_type, row.register_address)
            )
            entry["write_enabled"] = s.user_write_enabled if s else False
            entry["mask_enabled"]  = s.mask_enabled if s else False
            entry["screen_enabled"] = s.screen_enabled if s else False

        result.append(entry)

    return result


def get_protocol_groups(protocols_dir: Path) -> list[dict[str, Any]]:
    """
    Scan protocols_dir and return the cascading menu structure:
    [ { group: "eg4", protocols: ["eg4_18kpv_holding", "eg4_18kpv_input", ...] } ]
    """
    groups: list[dict[str, Any]] = []
    if not protocols_dir.exists():
        return groups

    _log.debug("get_protocol_groups: scanning %s", protocols_dir)
    for group_dir in sorted(protocols_dir.iterdir()):
        if not group_dir.is_dir():
            continue
        protocols: List[str] = sorted(
            f.stem for f in group_dir.iterdir()
            if f.suffix.lower() in (".csv", ".json")
            and not f.name.endswith(".override.csv")
        )
        if protocols:
            groups.append({"group": group_dir.name, "protocols": protocols})

    return groups
