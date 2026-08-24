# Description: SQLAlchemy ORM models for the MPG Web Management UI staging database.
# File: models.py
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
SQLAlchemy ORM models for the MPG Web Management UI staging database.

This database is a LOCAL SQLite file.
Its sole purpose is to stage user edits before they are committed back to
config.cfg and the protocol override/mask/screen files.

Tables
------
Setting          — mirrors every key-value pair from config.cfg
ProtocolRegister — one row per CSV register row per protocol file
ConfigBackup     — timestamped archives of committed config.cfg versions
AppState         — single-row global flags (dirty, orphan counts, last commit)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    object_session,
)

# Define explicit naming tokens to enforce consistent SQLite alter rules
naming_convention: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=naming_convention)
    pass


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------

class Setting(Base):
    """
    One row per key-value pair per section from config.cfg.

    value_disk    — what is currently on disk (refreshed on scan / file-watch)
    value_staged  — what the user has edited in the UI (persists across restarts)
    is_dirty      — True when value_staged != value_disk
    is_active     — False means the row is excluded from the next commit output
    is_orphan     — True when the key no longer appears in any transport class
    transport_type— "scraper" | "bridge" | "general" | "logging"
    """
    __tablename__: str = "settings"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("section", "key", name="uq_settings_section_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    section: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_disk: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_staged: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    transport_type: Mapped[str] = mapped_column(String(32), default="general")
    # "scraper" | "bridge" | "general" | "logging"

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    is_orphan: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Setting [{self.section}] {self.key}={self.value_staged!r} dirty={self.is_dirty}>"

    def mark_dirty(self) -> None:
        self.is_dirty = self.value_staged != self.value_disk

    @property
    def effective_value(self) -> str | None:
        """Returns staged value if dirty, otherwise disk value."""
        return self.value_staged if self.is_dirty else self.value_disk


# ---------------------------------------------------------------------------
# ProtocolRegister
# ---------------------------------------------------------------------------

class ProtocolRegister(Base):
    """
    One row per register entry per protocol CSV file. This is the shared
    protocol *definition* — one row per protocol, not per device.

    write_mode_protocol — the R/RW/RD/WO value from the CSV (never changed by UI)
    is_dirty             — a metadata field below (variable_name, documented_name,
                            unit, etc.) was edited in the protocol editor and the
                            underlying CSV needs to be rewritten on commit
                            (see config_writer._write_protocol_csvs).

    Per-device write/mask/screen selection state lives on DeviceProtocolSelection,
    not here — two devices sharing this protocol can make different choices.
    """
    __tablename__: str = "protocol_registers"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "protocol_name", "registry_type", "register_address",
            name="uq_register_protocol_type_addr"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Protocol identity
    protocol_group: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    protocol_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    registry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # "input" | "holding" | "coil" | "discrete" | "custom_bus" | "json"

    # Register fields (sourced from CSV)
    register_address: Mapped[str] = mapped_column(String(32), nullable=False)
    variable_name: Mapped[str] = mapped_column(String(128), nullable=False)
    documented_name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    values_range: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjustments: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_interval: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Write mode from protocol (immutable from CSV — never changed by user)
    write_mode_protocol: Mapped[str] = mapped_column(String(8), default="R")
    # "R" | "RW" | "RD" | "WO"

    # 32 bit register joins
    paired_high_address: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Set directly (not via mark_dirty()) by update_protocol_register_field()
    # whenever a metadata field above is edited in the protocol editor —
    # triggers a CSV rewrite for this protocol on the next commit.
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)

    # True for rows materialized on-demand for a synthetic metric (see
    # services.protocol_service.build_synthetic_rows) or a JSON
    # code-description "<name>_desc" metric (see build_json_desc_rows) the
    # first time a device selects it — NOT sourced from a protocol CSV row,
    # despite living in this table. config_writer._write_protocol_csvs
    # MUST exclude any row with either flag set when regenerating a
    # protocol's CSV file on commit, or these get written back as fake
    # register rows that don't correspond to real hardware registers and
    # will re-parse incorrectly (a literal "<name>_desc" CSV row would
    # collide with the one _add_code_description_entries generates fresh
    # from its source row at every load). register_address for these rows
    # is never a real CSV address — see _virtual_register_address().
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    is_json_desc: Mapped[bool] = mapped_column(Boolean, default=False)
    # Staged for deletion by the protocol editor's DELETE checkbox column.
    # Deliberately not folded into is_dirty (used for in-place field edits
    # that get *rewritten* into the CSV) — a deletion isn't a value to
    # write back, it's the absence of the row, and needs its own explicit
    # commit step: _write_protocol_csvs excludes pending_delete rows from
    # the regenerated CSV, and config_writer._delete_pending_protocol_registers
    # (run earlier in commit_all, before the CSV write) actually removes
    # the row — and any DeviceProtocolSelection rows across every device
    # that reference it, since that table has no FK/cascade of its own,
    # only a loose (protocol_name, registry_type, register_address) match
    # (see DeviceProtocolSelection) — from the DB.
    pending_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    # Only meaningful when is_json_desc is True — the variable_name of the
    # real CSV register this "<name>_desc" entry decodes (registry_map_entry
    # .description_source in the live transport, see build_json_desc_rows).
    # Lets the mask/screen auto-link cascade (see protocol_service
    # .toggle_register_field / materialize_and_toggle_virtual_metric) find
    # the paired code row from the desc side without needing the live
    # transport — the reverse direction (code -> desc) still needs it,
    # since a not-yet-materialized desc has no DB row to search for by
    # source_variable_name; that direction is resolved at the API layer
    # (routers.protocols), which has transport access, via build_json_desc_rows.
    source_variable_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ProtocolRegister {self.protocol_name}/{self.registry_type} "
            f"@{self.register_address} {self.variable_name!r}>"
        )

    @property
    def is_writable_by_protocol(self) -> bool:
        return self.write_mode_protocol in ("RW", "W", "WO", "WRITE", "R/W")


class DeviceProtocolSelection(Base):
    """
    Device-specific register selection state.

    This keeps mask/screen/write choices scoped to a transport device while the
    ProtocolRegister table remains the shared protocol definition source.
    """

    __tablename__: str = "device_protocol_selections"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "device_name", "protocol_name", "registry_type", "register_address",
            name="uq_device_protocol_selection"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    protocol_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    registry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    register_address: Mapped[str] = mapped_column(String(32), nullable=False)

    user_write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mask_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    screen_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    user_write_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=False)
    mask_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=False)
    screen_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=False)

    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    # 32 bit register joins
    paired_high_address: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def mark_dirty(self) -> None:
        self.is_dirty = (
            self.user_write_enabled != self.user_write_enabled_disk
            or self.mask_enabled != self.mask_enabled_disk
            or self.screen_enabled != self.screen_enabled_disk
        )

    @property
    def is_writable_by_protocol(self) -> bool:
        """
        Determines writability by looking up the base protocol's rules.
        """
        db: Session | None = object_session(self)
        if db:
            parent: ProtocolRegister | None = db.query(ProtocolRegister).filter(
                ProtocolRegister.protocol_name == self.protocol_name,
                ProtocolRegister.registry_type == self.registry_type,
                ProtocolRegister.register_address == self.register_address
            ).first()
            return parent.is_writable_by_protocol if parent else False
        return False


# ---------------------------------------------------------------------------
# ConfigBackup
# ---------------------------------------------------------------------------

class ConfigBackup(Base):
    """Timestamped archive of each committed config.cfg."""

    __tablename__: str = "config_backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    # "manual" | "auto" | "file_watch"
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ConfigBackup {self.id} {self.created_at} {self.trigger}>"


# ---------------------------------------------------------------------------
# OrphanedFilterName
# ---------------------------------------------------------------------------

class OrphanedFilterName(Base):
    """
    One row per mask/screen filter-file line that matches no known register
    for its device's current protocol, as of the last scan.

    Why this exists: variable_mask_<device>.txt / variable_screen_<device>.txt
    are plain text files a user (or an "Analyze"-driven export) can write
    register names into. If a name in one of those files stops matching any
    register — most commonly because the protocol's CSV was updated and the
    register was renamed or removed — that line becomes permanently inert.
    For screen (denylist) files specifically this is worse than inert: if
    NONE of a registry type's registers appear in the screen file, the whole
    type is treated as fully screened and excluded from scraping entirely
    (see protocol_settings.py's load__registry) — a single stale line can
    silently zero out all data for a device with no error anywhere. This
    table makes that condition visible in the web UI instead of only in
    logs.

    Purely derived, not user-edited: every scan replaces a device's rows
    wholesale (delete-then-insert), same pattern the scanner already uses
    for ProtocolRegister. There's no is_dirty/value_staged here because
    there's nothing to stage — a name simply does or doesn't currently
    resolve.
    """
    __tablename__: str = "orphaned_filter_names"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "device_name", "file_type", "name",
            name="uq_orphaned_filter_device_type_name",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # "mask" | "screen"
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<OrphanedFilterName [{self.device_name}] {self.file_type}:{self.name}>"


# ---------------------------------------------------------------------------
# AppState
# ---------------------------------------------------------------------------

class AppState(Base):
    """
    Single-row table.  id is always 1.
    Tracks global dirty/orphan flags used to drive the UI banner and
    the COMMIT ALL CHANGES button enabled state.
    """

    __tablename__: str = "app_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    has_dirty_settings: Mapped[bool] = mapped_column(Boolean, default=False)
    has_dirty_protocols: Mapped[bool] = mapped_column(Boolean, default=False)
    has_orphans: Mapped[bool] = mapped_column(Boolean, default=False)
    has_orphaned_filters: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when any mask/screen filter-file line matches no current
    # register — see OrphanedFilterName. Deliberately separate from
    # has_orphans: a Setting orphan is a stale config.cfg key (safe to
    # ignore indefinitely), an orphaned filter name can be silently
    # suppressing live data (see OrphanedFilterName docstring) — these
    # need different urgency in the UI, so they shouldn't share one flag.

    dirty_settings_count: Mapped[int] = mapped_column(Integer, default=0)
    dirty_protocols_count: Mapped[int] = mapped_column(Integer, default=0)
    orphan_count: Mapped[int] = mapped_column(Integer, default=0)
    orphaned_filter_count: Mapped[int] = mapped_column(Integer, default=0)

    scanner_status: Mapped[str] = mapped_column(String(32), default="idle")
    # "idle" | "running" | "error"
    scanner_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AppState dirty_settings={self.has_dirty_settings} "
            f"dirty_protocols={self.has_dirty_protocols} "
            f"orphans={self.has_orphans}>"
        )

class SettingDescription(Base):
    """
    Consolidated registry of every transport setting key across all transports.
    Populated on first startup by scanning the transport library.
    Descriptions are user-editable; key + transports are derived from scans.
    """
    __tablename__: str = "setting_descriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    transports: Mapped[str] = mapped_column(Text, nullable=True)   # comma-separated list
    description: Mapped[str] = mapped_column(Text, nullable=True)
    description_disk: Mapped[str] = mapped_column(Text, nullable=True)  # for dirty tracking
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)

    def mark_dirty(self) -> None:
        self.is_dirty = (self.description or "") != (self.description_disk or "")
