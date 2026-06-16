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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from sqlalchemy import select
from sqlalchemy.orm import Query, Session

from ..database import refresh_app_state
from ..models import DeviceProtocolSelection, ProtocolRegister

_log = __import__("logging").getLogger(__name__)


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

    @property
    def is_writable_by_protocol(self) -> bool:
        return self.write_mode_protocol in ("RW", "W", "WO", "WRITE", "R/W")


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
        rows = [
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
                user_write_enabled=s.user_write_enabled if (s := selections.get((row.protocol_name, row.registry_type, row.register_address))) else False,
                mask_enabled=s.mask_enabled if s else False,
                screen_enabled=s.screen_enabled if s else False,
                is_dirty=s.is_dirty if s else False,
            )
            for row in protocol_rows
        ]
    else:
        rows = protocol_rows

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
    rows = (
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

    target = row
    if device_name:
        target = (
            db.query(DeviceProtocolSelection)
            .filter(
                DeviceProtocolSelection.device_name == device_name,
                DeviceProtocolSelection.protocol_name == row.protocol_name,
                DeviceProtocolSelection.registry_type == row.registry_type,
                DeviceProtocolSelection.register_address == row.register_address,
            )
            .first()
        )
        if target is None:
            target = DeviceProtocolSelection(
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
            db.add(target)
            db.flush()

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
    return target


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
