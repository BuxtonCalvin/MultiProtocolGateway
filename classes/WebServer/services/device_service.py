# Description: services/device_service.py — Queries the staging DB for device/transport data used to build the navigation menus and device panes.
# File: device_service.py
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
services/device_service.py — Queries the staging DB for device/transport data
used to build the navigation menus and device panes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppState, Setting
from ..scanner import scan_transport_library


@dataclass
class DeviceSummary:
    name: str
    section: str
    transport_type: str      # "scraper" | "bridge" | "general"
    transport_class: str     # e.g. "modbus_tcp", "mqtt"
    protocol_version: str    # "" for bridges
    host: str
    port: str
    is_connected: bool = False   # runtime status — set by gateway if available


@dataclass
class NavData:
    scrapers: list[DeviceSummary]
    bridges: list[DeviceSummary]
    protocol_groups: list[str]


def get_nav_data(db: Session) -> NavData:
    """
    Returns all the data needed to render the three nav dropdowns.
    """
    scrapers: list[DeviceSummary] = []
    bridges: list[DeviceSummary] = []

    # Find all transport sections
    sections: Sequence[str] = (
        db.execute(
            select(Setting.section)
            .where(Setting.section.like("transport.%"))
            .distinct()
        )
        .scalars()
        .all()
    )

    for section in sorted(sections):
        device_name: str = section.removeprefix("transport.")
        keys: dict[str, str] = _get_section_keys(db, section)

        transport_class: str = keys.get("transport", "")
        protocol_version: str = keys.get("protocol_version", "")
        transport_type: str = keys.get("transport_type_cached", "general")

        summary = DeviceSummary(
            name=device_name,
            section=section,
            transport_type=transport_type,
            transport_class=transport_class,
            protocol_version=protocol_version,
            host=keys.get("host", ""),
            port=keys.get("port", ""),
        )

        if transport_type == "scraper":
            scrapers.append(summary)
        elif transport_type == "bridge":
            bridges.append(summary)

    # Protocol groups from ProtocolRegister table
    from ..models import ProtocolRegister
    groups: Sequence[str] = (
        db.execute(
            select(ProtocolRegister.protocol_group).distinct()
        )
        .scalars()
        .all()
    )

    return NavData(
        scrapers=scrapers,
        bridges=bridges,
        protocol_groups=sorted(groups),
    )


def get_device_settings(db: Session, section: str) -> list[Setting]:
    """Return all Setting rows for a device section, ordered by key."""
    return (
        db.query(Setting)
        .filter(Setting.section == section)
        .order_by(Setting.key)
        .all()
    )


def get_device_summary(db: Session, device_name: str) -> DeviceSummary | None:
    section: str = f"transport.{device_name}"
    keys: dict[str, str] = _get_section_keys(db, section)
    if not keys:
        return None
    return DeviceSummary(
        name=device_name,
        section=section,
        transport_type=keys.get("transport_type_cached", "general"),
        transport_class=keys.get("transport", ""),
        protocol_version=keys.get("protocol_version", ""),
        host=keys.get("host", ""),
        port=keys.get("port", ""),
    )


def _get_section_keys(db: Session, section: str) -> dict[str, str]:
    rows: List[Setting] = db.query(Setting).filter(Setting.section == section).all()
    result: dict[str, str] = {}
    for row in rows:
        result[row.key] = row.value_staged or row.value_disk or ""
        result["transport_type_cached"] = row.transport_type
    return result


def get_transport_library(transports_dir: Path) -> list[dict[str, Any]]:
    """
    Returns the transport library list for the TRANSPORT LIBRARY page.
    Each entry has: name, classification, keys.
    """
    library: dict[str, dict[str, Any]] = scan_transport_library(transports_dir)
    result = []
    for name, info in sorted(library.items()):
        all_keys = list(info["keys"].keys())
        result.append({
            "name": name,
            "classification": info["classification"],
            "key_count": len(all_keys),
            "sample_keys": all_keys[:5],
            "all_keys": all_keys,
        })
    return result


def get_app_state(db: Session) -> AppState:
    state: AppState | None = db.get(AppState, 1)
    if state is None:
        from ..database import ensure_app_state
        state = ensure_app_state(db)
    return state


def get_orphaned_settings(db: Session) -> list[Setting]:
    return (
        db.query(Setting)
        .filter(Setting.is_orphan == True)  # noqa: E712
        .order_by(Setting.section, Setting.key)
        .all()
    )


def delete_orphan(db: Session, setting_id: int) -> bool:
    row: Setting | None = db.get(Setting, setting_id)
    if row and row.is_orphan:
        db.delete(row)
        db.commit()
        return True
    return False


def delete_orphans_bulk(db: Session, setting_ids: list[int]) -> int:
    count = 0
    for sid in setting_ids:
        if delete_orphan(db, sid):
            count += 1
    return count
