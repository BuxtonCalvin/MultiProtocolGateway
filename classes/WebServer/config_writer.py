# Description: config_writer.py — The commit engine.
# File: config_writer.py
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
config_writer.py — The commit engine.

When the user clicks "COMMIT ALL CHANGES" this module:
  1. Archives the current config.cfg to a timestamped backup
  2. Rebuilds config.cfg from scratch using active settings rows
  3. Writes variable_mask_*.txt and variable_screen_*.txt files
  4. Writes *.writable.csv files (one per device) for user_write_enabled changes
  5. Resets all is_dirty flags and updates AppState.last_commit_at
"""

from __future__ import annotations

import csv
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from sqlalchemy.engine.row import Row
from sqlalchemy.orm import Session

from .database import refresh_app_state
from .models import (
    AppState,
    ConfigBackup,
    DeviceProtocolSelection,
    ProtocolRegister,
    Setting,
)

_log: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backup helper
# ---------------------------------------------------------------------------

def create_backup(config_path: Path, db: Session, trigger: str = "manual") -> ConfigBackup:
    """
    Copy config.cfg to config/backups/config_<timestamp>.cfg.
    Record the backup in the ConfigBackup table.
    """
    backup_dir: Path = config_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts: str = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_path: Path = backup_dir / f"config_{ts}.cfg"

    shutil.copy2(config_path, backup_path)
    size: int = backup_path.stat().st_size

    record = ConfigBackup(
        filepath=str(backup_path),
        file_size_bytes=size,
        trigger=trigger,
    )
    db.add(record)
    db.flush()
    _log.info(f"Config backup created: {backup_path}")
    return record


# ---------------------------------------------------------------------------
# Config writer
# ---------------------------------------------------------------------------

def _build_config_text(settings_rows: list[Setting]) -> str:
    """
    Rebuild config.cfg content from active Setting rows.
    Groups rows by section, preserving transport. prefix ordering:
      [general] → [logging] → [transport.*] alphabetically → [remaining]
    """
    from collections import defaultdict
    removed_keys: set[str] = {"analyze_protocol", "analyze_protocol_save_load"}
    sections: dict[str, list[Setting]] = defaultdict(list)
    for row in settings_rows:
        if row.is_active and row.key not in removed_keys:
            sections[row.section].append(row)

    # Section ordering
    order: List[str] = ["general", "logging"]
    transport_sections: List[str] = sorted(
        s for s in sections if s.startswith("transport.")
    )
    other_sections: List[str] = sorted(
        s for s in sections
        if s not in order and s not in transport_sections
    )
    final_order: List[str] = order + transport_sections + other_sections

    lines: list[str] = []
    for section in final_order:
        if section not in sections:
            continue
        lines.append(f"[{section}]")
        for row in sections[section]:
            value: str = row.value_staged if row.value_staged is not None else (row.value_disk or "")
            lines.append(f"{row.key} = {value}")
        lines.append("")  # blank line between sections

    return "\n".join(lines)


def _write_mask_screen_files(db: Session, project_root: Path) -> dict[str, int]:
    """
    For each transport section, rewrite its variable_mask_*.txt and
    variable_screen_*.txt files based on that device's DeviceProtocolSelection
    toggles (write/mask/screen selection is per-device, not part of the
    shared ProtocolRegister protocol definition).

    Returns counts of files written.
    """
    config_dir: Path = project_root / "config"
    config_dir.mkdir(exist_ok=True)

    written: dict[str, int] = {"mask": 0, "screen": 0}

    # Group ProtocolRegister rows by protocol_name (used as file suffix)
    # The mask/screen file names use the transport name from the Setting rows.
    # We need to find which transports use which protocols.
    transport_protocols: dict[str, str] = {}
    for row in db.query(Setting).filter(Setting.key == "protocol_version").all():
        # section = "transport.Inverter1" → transport_name = "Inverter1"
        transport_name: str = row.section.replace("transport.", "", 1)
        transport_protocols[transport_name] = row.value_staged or row.value_disk or ""

    for transport_name, protocol_version in transport_protocols.items():
        if not protocol_version:
            continue

        # Find all registers for this protocol. Synthetic and JSON
        # code-description rows are excluded here — "Synthetic and JSON
        # metric descriptions should not be written to a mask or screen
        # file." Their selection still reaches these files correctly: the
        # mask/screen cascade in protocol_service.toggle_register_field /
        # materialize_and_toggle_virtual_metric keeps a description's
        # DeviceProtocolSelection in lockstep with its underlying CODE
        # register's (a real, non-excluded CSV row), so selecting a
        # description writes its code's name here, never the description's
        # own name — same for a synthetic field's code (there isn't one, so
        # a synthetic selection has no file representation at all; it can
        # only reach the data stream via its own always-forwarded path).
        registers: List[Row[Tuple[DeviceProtocolSelection, ProtocolRegister]]] = (
            db.query(DeviceProtocolSelection, ProtocolRegister)
            .join(
                ProtocolRegister,
                (DeviceProtocolSelection.protocol_name == ProtocolRegister.protocol_name)
                & (DeviceProtocolSelection.registry_type == ProtocolRegister.registry_type)
                & (DeviceProtocolSelection.register_address == ProtocolRegister.register_address),
            )
            .filter(
                DeviceProtocolSelection.device_name == transport_name,
                DeviceProtocolSelection.protocol_name.like(f"{protocol_version}%"),
                ProtocolRegister.is_synthetic == False,   # noqa: E712
                ProtocolRegister.is_json_desc == False,   # noqa: E712
            )
            .all()
        )

        if not registers:
            continue

        # Mask file: variables where mask_enabled=True
        # Use sets keyed by variable_name — if ANY registry type has a variable
        # explicitly unmasked (mask_enabled=False after user toggle), it must be
        # absent from the file.  We collect names that are masked, then subtract
        # any that are unmasked in another registry type for the same device.
        masked_names: set[str] = set()
        unmasked_names: set[str] = set()
        for selection_row, protocol_row in registers:
            if selection_row.mask_enabled:
                masked_names.add(protocol_row.variable_name)
            else:
                unmasked_names.add(protocol_row.variable_name)
        mask_lines: list[str] = sorted(masked_names - unmasked_names)
        mask_path: Path = config_dir / f"variable_mask_{transport_name}.txt"
        mask_path.write_text("\n".join(mask_lines) + "\n", encoding="utf-8")
        written["mask"] += 1
        _log.info(f"Wrote mask file: {mask_path} ({len(mask_lines)} entries)")

        # Screen file: variables where screen_enabled=True (same dedup logic)
        screened_names: set[str] = set()
        unscreened_names: set[str] = set()
        for selection_row, protocol_row in registers:
            if selection_row.screen_enabled:
                screened_names.add(protocol_row.variable_name)
            else:
                unscreened_names.add(protocol_row.variable_name)
        screen_lines: list[str] = sorted(screened_names - unscreened_names)
        screen_path: Path = config_dir / f"variable_screen_{transport_name}.txt"
        screen_path.write_text("\n".join(screen_lines) + "\n", encoding="utf-8")
        written["screen"] += 1
        _log.info(f"Wrote screen file: {screen_path} ({len(screen_lines)} entries)")

    return written


def _write_writable_csv(db: Session, protocols_dir: Path, config_dir: Path) -> int:
    """
    For each device with user_write_enabled changes, write/update the
    <device_name>.writable.csv in the config_dir.

    This is a per-device write-enable allowlist gating which protocol-writable
    variables get an MQTT write topic, driven entirely by the "W" checkbox
    (DeviceProtocolSelection.user_write_enabled) — reusing "override" for both
    made the two easy to confuse despite sharing no directory, filename
    pattern, writer, or reader.

    One file per DEVICE, <device_name>.writable.csv. Write-enable selections are inherently
    per-device (DeviceProtocolSelection.device_name), so two devices sharing
    the same protocol can (and often should) have different write-enabled
    registers — e.g. only one of a pair of identical inverters is actually
    wired up for remote control. A protocol-scoped file couldn't represent
    that: every device on that protocol would share the same file and
    therefore the same write-enabled set, regardless of what each device's
    own "W" checkboxes actually said.

    Storing these in config_dir (not protocols_dir) means they survive
    software updates and are accessible via a Docker volume mount on config/.

    The writable file contains only rows where user_write_enabled=True and
    the protocol allows writing (write_mode_protocol in RW/W/WO).
    Setting user_write_enabled=False removes a row from the writable file.

    Returns count of writable files written.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    device_names: list[str] = [
        row[0]
        for row in (
            db.query(DeviceProtocolSelection.device_name)
            .filter(DeviceProtocolSelection.registry_type.in_(["holding", "coil"]))
            .distinct()
            .all()
        )
    ]

    written = 0
    for device_name in device_names:
        # Which protocol(s) this device's selections reference — used only to
        # confirm the underlying CSV(s) still exist, so a missing/renamed
        # protocol file surfaces as a warning here instead of silently
        # writing a writable file with no protocol behind it.
        protocol_names_for_device: list[str] = [
            row[0]
            for row in (
                db.query(DeviceProtocolSelection.protocol_name)
                .filter(
                    DeviceProtocolSelection.device_name == device_name,
                    DeviceProtocolSelection.registry_type.in_(["holding", "coil"]),
                )
                .distinct()
                .all()
            )
        ]
        if not any(_find_protocol_csv(protocols_dir, p) for p in protocol_names_for_device):
            _log.warning(
                f"Cannot find a protocol CSV for any of {protocol_names_for_device} "
                f"used by device '{device_name}' — writable file not written"
            )
            continue

        writable_path: Path = config_dir / f"{device_name}.writable.csv"

        # Note: previously this loaded the existing writable file into a dict
        # before immediately clear()-ing and rebuilding it from the DB query
        # below — the load was completely discarded and never fed into the
        # rebuilt result. Removed rather than fixed, since the DB is already
        # authoritative here and there was nothing from the on-disk file this
        # function needed to preserve.
        selected_rows: List[ProtocolRegister] = (
            db.query(ProtocolRegister)
            .join(
                DeviceProtocolSelection,
                (DeviceProtocolSelection.protocol_name == ProtocolRegister.protocol_name) &
                (DeviceProtocolSelection.registry_type == ProtocolRegister.registry_type) &
                (DeviceProtocolSelection.register_address == ProtocolRegister.register_address),
            )
            .filter(
                DeviceProtocolSelection.device_name == device_name,
                ProtocolRegister.registry_type.in_(["holding", "coil"]),
                DeviceProtocolSelection.user_write_enabled == True,  # noqa: E712
            )
            .all()
        )

        writable_entries: dict[str, dict[str, str]] = {}
        for row in selected_rows:
            if row.is_writable_by_protocol:
                key: str = row.documented_name or row.variable_name
                writable_entries[key] = {
                    "documented name": row.documented_name,
                    "write": "W",
                }

        # Write writable file
        if writable_entries:
            with open(writable_path, "w", newline="", encoding="utf-8") as f:
                writer: csv.DictWriter[str] = csv.DictWriter(
                    f, fieldnames=["documented name", "write"]
                )
                writer.writeheader()
                for entry in writable_entries.values():
                    writer.writerow(entry)
        elif writable_path.exists():
            writable_path.unlink()  # Remove empty writable file

        written += 1
        _log.info(f"Writable CSV updated: {writable_path}")

    return written


def _find_protocol_csv(protocols_dir: Path, protocol_name: str) -> Path | None:
    """Search protocols_dir recursively for a CSV matching protocol_name."""
    for csv_file in protocols_dir.rglob(f"{protocol_name}.csv"):
        return csv_file
    # Try partial match (protocol_name might be a stem)
    for csv_file in protocols_dir.rglob("*.csv"):
        if csv_file.stem == protocol_name:
            return csv_file
    return None


def _write_protocol_csvs(db: Session, protocols_dir: Path) -> int:
    dirty_protocols: List[str] = [
        row[0]
        for row in (
            db.query(ProtocolRegister.protocol_name)
            .filter(ProtocolRegister.is_dirty == True)  # noqa: E712
            .distinct()
            .all()
        )
    ]

    written = 0
    for protocol_name in dirty_protocols:
        csv_path: Path | None = _find_protocol_csv(protocols_dir, protocol_name)
        if not csv_path:
            continue

        rows: List[ProtocolRegister] = (
            db.query(ProtocolRegister)
            .filter(
                ProtocolRegister.protocol_name == protocol_name,
                # is_synthetic / is_json_desc rows are materialized on-demand
                # for the webUI's selectable synthetic/JSON metrics (see
                # services.protocol_service.materialize_and_toggle_virtual_metric)
                # — never real CSV rows, so they must never be written back
                # into one. Their register_address is a "~"-prefixed virtual
                # token, not a real register address, so writing them out
                # would corrupt the CSV even if is_dirty tripped for an
                # unrelated real row in the same protocol.
                ProtocolRegister.is_synthetic == False,   # noqa: E712
                ProtocolRegister.is_json_desc == False,   # noqa: E712
            )
            .order_by(ProtocolRegister.register_address)
            .all()
        )

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer: csv.DictWriter[str] = csv.DictWriter(
                f,
                fieldnames=[
                    "register",
                    "variable_name",
                    "documented_name",
                    "unit",
                    "data_type",
                    "values",
                    "adjustments",
                    "note",
                    "writable",
                    "read_interval",
                ],
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "register": row.register_address,
                    "variable_name": row.variable_name,
                    "documented_name": row.documented_name,
                    "unit": row.unit or "",
                    "data_type": row.data_type or "",
                    "values": row.values_range or "",
                    "adjustments": row.adjustments or "",
                    "note": row.note or "",
                    "writable": row.write_mode_protocol or "",
                    "read_interval": row.read_interval or "",
                })

        written += 1
        _log.info(f"Protocol CSV updated: {csv_path}")

    return written


# ---------------------------------------------------------------------------
# Reset dirty flags after commit
# ---------------------------------------------------------------------------

def _reset_dirty_flags(db: Session) -> None:
    """
    After a successful commit, sync disk state to match staged state
    and clear all dirty flags.

    For inactive rows (is_active=False), the key was intentionally removed
    from the config file. Both value_disk and value_staged are cleared to ""
    so that the next scanner pass (which will find the key absent from
    config.cfg) doesn't create a false "added" dirty state.
    """
    for row in db.query(Setting).filter(Setting.is_dirty == True).all():  # noqa: E712
        if row.is_active:
            row.value_disk = row.value_staged
        else:
            # Key was removed from config — reflect that in both disk and staged
            row.value_disk = ""
            row.value_staged = ""
        row.is_dirty = False

    for row in db.query(ProtocolRegister).filter(ProtocolRegister.is_dirty == True).all():  # noqa: E712
        row.is_dirty = False

    for row in db.query(DeviceProtocolSelection).filter(DeviceProtocolSelection.is_dirty == True).all():  # noqa: E712
        row.user_write_enabled_disk = row.user_write_enabled
        row.mask_enabled_disk = row.mask_enabled
        row.screen_enabled_disk = row.screen_enabled
        row.is_dirty = False

    db.flush()


# ---------------------------------------------------------------------------
# Main commit entry point
# ---------------------------------------------------------------------------

def commit_all(db: Session, config_path: Path, project_root: Path, protocols_dir: Path, config_dir: Path | None = None) -> dict[str, int | str]:
    """
    Full commit sequence.  Called by POST /api/commit.

    Returns a summary dict with counts of what was written.
    """
    _log.info("Starting commit sequence...")
    result: dict[str, int | str] = {}

    # 1. Backup
    backup: ConfigBackup = create_backup(config_path, db, trigger="manual")
    result["backup_path"] = backup.filepath

    # 2. Rebuild config.cfg
    all_settings: List[Setting] = db.query(Setting).filter(Setting.is_orphan == False).all()  # noqa: E712
    config_text: str = _build_config_text(all_settings)
    config_path.write_text(config_text, encoding="utf-8")
    result["settings_written"] = len([s for s in all_settings if s.is_active])
    _log.info(f"config.cfg written ({result['settings_written']} active settings)")

    # 3. Mask and screen files
    mask_screen: dict[str, int] = _write_mask_screen_files(db, project_root)
    result["mask_files_written"] = mask_screen["mask"]
    result["screen_files_written"] = mask_screen["screen"]

    # 4. Writable CSVs — saved to config_dir so they survive updates and Docker mounts.
    # Result key kept as "override_files_written" (not renamed to match the new
    # .writable.csv filename) since it's part of the JSON response the commit
    # button's frontend code reads — renaming it here without knowing what
    # reads it there risks a silent breakage this change didn't need to cause.
    _config_dir: Path = config_dir or config_path.parent
    result["override_files_written"] = _write_writable_csv(db, protocols_dir, _config_dir)

    # 5. Protocol CSVs
    result["protocol_csvs_written"] = _write_protocol_csvs(db, protocols_dir)

    # 6. Reset dirty flags
    _reset_dirty_flags(db)

    # 7. Update AppState
    state: AppState | None = db.get(AppState, 1)
    if state:
        state.last_commit_at = datetime.now().astimezone()
    db.commit()

    refresh_app_state(db)

    _log.info(f"Commit complete: {result}")
    return result
