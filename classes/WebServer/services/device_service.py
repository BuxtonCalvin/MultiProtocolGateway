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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppState, Setting
from ..scanner import TransportLibraryEntry, scan_transport_library
from ..transport_registry import get_known_transport_keys

_log: logging.Logger = logging.getLogger(__name__)


class TransportLibraryRow(TypedDict):
    """Shape of each row returned by get_transport_library()."""
    name: str
    classification: str
    key_count: int
    sample_keys: list[str]
    all_keys: list[str]


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


def ensure_bridge_sections_exist(db: Session, bridge_value: str | None) -> list[str]:
    """
    Given a staged "bridge" setting value — a comma-separated list of
    "transport.<name>" entries, as written by the bridge multi-select (see
    scraper_panes.html) — create the DB rows for any referenced bridge
    section that doesn't exist yet, seeded from that transport class's
    resolved defaults in transport_defaults.json.

    Without this, picking a bridge that has never been configured before
    (no existing [transport.<name>] section) stages a "bridge = ...,
    transport.<name>, ..." reference on the scraper side that points at
    nothing: config_writer.commit_all() only ever writes sections it finds
    rows for (see _group_settings there), so the referenced bridge section
    would silently never appear in config.cfg even though the scraper's
    bridge= line names it. Called from update_setting() in devices.py
    whenever the patched row is the "bridge" key.

    Only creates rows for sections that don't already exist at all — an
    existing bridge (however it got there: prior manual creation, a scan
    of a hand-edited config.cfg, etc.) is left untouched.

    Returns the list of newly-created bridge section names (device_name,
    not the full "transport.<name>" section string), empty if nothing
    needed creating, so the caller can log/report what happened.
    """
    if not bridge_value:
        return []

    known: dict[str, dict[str, str]] = get_known_transport_keys()
    created: list[str] = []

    for part in bridge_value.split(","):
        part = part.strip()
        if not part.startswith("transport."):
            continue
        bridge_name: str = part.removeprefix("transport.")
        if not bridge_name:
            continue

        section: str = f"transport.{bridge_name}"
        exists: bool = (
            db.query(Setting.id).filter(Setting.section == section).first()
            is not None
        )
        if exists:
            continue

        defaults: dict[str, str] | None = known.get(bridge_name)
        if defaults is None:
            # Not a recognized transport class (stale value, or a bridge
            # module that's since been removed from the transports dir) —
            # nothing to seed. Leave the dangling reference as-is; the diff
            # panel / orphan detection is the right place to surface that,
            # not a guess made here.
            _log.warning(
                "ensure_bridge_sections_exist: '%s' is not a known transport "
                "class — leaving section unseeded", bridge_name
            )
            continue

        # Most bridge entries in transport_defaults.json don't $extends
        # "_base" (only scraper-style transports generally do), so "transport"
        # usually isn't among their resolved default keys at all. Set it
        # explicitly to the class name being instantiated regardless — same
        # as new-scraper creation does — so get_nav_data()'s transport_class
        # lookup and the config.cfg output both have it.
        seed: dict[str, str] = {**defaults, "transport": bridge_name}

        for key, default_value in seed.items():
            row = Setting(
                section=section,
                key=key,
                value_disk=None,
                value_staged=default_value,
                default_value=default_value,
                transport_type="bridge",
                is_active=True,
            )
            row.mark_dirty()
            db.add(row)

        created.append(bridge_name)
        _log.info(
            "ensure_bridge_sections_exist: created new bridge section '%s' "
            "(%d keys) from transport defaults", section, len(seed)
        )

    return created


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


def get_transport_library(transports_dir: Path) -> list[TransportLibraryRow]:
    """
    Returns the transport library list for the TRANSPORT LIBRARY page.
    Each entry has: name, classification, keys.
    """
    library: dict[str, TransportLibraryEntry] = scan_transport_library(transports_dir)
    result: list[TransportLibraryRow] = []
    for name, info in sorted(library.items()):
        all_keys: list[str] = list(info["keys"].keys())
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
