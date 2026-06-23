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
from typing import Protocol, runtime_checkable

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
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


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# RegisterToggleTarget
# ---------------------------------------------------------------------------

@runtime_checkable
class RegisterToggleTarget(Protocol):
    """
    Structural protocol satisfied by both ProtocolRegister and
    DeviceProtocolSelection.  Allows toggle_register_field in
    protocol_service.py to type ``target`` as a single non-union type,
    giving checkers full attribute visibility without a cast.

    Attributes are declared as Mapped[bool] — not plain bool — because
    pyright resolves protocol conformance against class-level annotations,
    where SQLAlchemy's descriptor appears as Mapped[bool].  At runtime the
    descriptor transparently returns a plain bool on instances, but the
    structural check happens at the annotation level, so the protocol must
    mirror that exact type to satisfy invariant mutable-attribute matching.
    """

    user_write_enabled: Mapped[bool]
    mask_enabled: Mapped[bool]
    screen_enabled: Mapped[bool]
    is_dirty: Mapped[bool]

    def mark_dirty(self) -> None: ...


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
    __tablename__ = "settings"
    __table_args__ = (
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
    One row per register entry per protocol CSV file.

    write_mode_protocol — the R/RW/RD/WO value from the CSV (never changed by UI)
    user_write_enabled  — toggled by user in the UI (fed into .override.csv on commit)
    mask_enabled        — drives the variable_mask_*.txt file on commit
    screen_enabled      — drives the variable_screen_*.txt file on commit
    is_dirty            — any of the three toggles differs from what is on disk
    """
    __tablename__ = "protocol_registers"
    __table_args__ = (
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
    # "input" | "holding" | "coil" | "discrete" | "json"

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

    # User-controlled toggles (these drive the override/mask/screen files)
    user_write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mask_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    screen_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Disk-state snapshots (set on scan, compared on dirty check)
    user_write_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=False)
    mask_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=True)
    screen_enabled_disk: Mapped[bool] = mapped_column(Boolean, default=False)

    # 32 bit register joins
    paired_high_address: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ProtocolRegister {self.protocol_name}/{self.registry_type} "
            f"@{self.register_address} {self.variable_name!r}>"
        )

    def mark_dirty(self) -> None:
        self.is_dirty = (
            self.user_write_enabled != self.user_write_enabled_disk
            or self.mask_enabled != self.mask_enabled_disk
            or self.screen_enabled != self.screen_enabled_disk
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

    __tablename__ = "device_protocol_selections"
    __table_args__ = (
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

    __tablename__ = "config_backups"

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
# AppState
# ---------------------------------------------------------------------------

class AppState(Base):
    """
    Single-row table.  id is always 1.
    Tracks global dirty/orphan flags used to drive the UI banner and
    the COMMIT ALL CHANGES button enabled state.
    """

    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    has_dirty_settings: Mapped[bool] = mapped_column(Boolean, default=False)
    has_dirty_protocols: Mapped[bool] = mapped_column(Boolean, default=False)
    has_orphans: Mapped[bool] = mapped_column(Boolean, default=False)

    dirty_settings_count: Mapped[int] = mapped_column(Integer, default=0)
    dirty_protocols_count: Mapped[int] = mapped_column(Integer, default=0)
    orphan_count: Mapped[int] = mapped_column(Integer, default=0)

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
    __tablename__ = "setting_descriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    transports: Mapped[str] = mapped_column(Text, nullable=True)   # comma-separated list
    description: Mapped[str] = mapped_column(Text, nullable=True)
    description_disk: Mapped[str] = mapped_column(Text, nullable=True)  # for dirty tracking
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False)

    def mark_dirty(self) -> None:
        self.is_dirty = (self.description or "") != (self.description_disk or "")
