# Description: Initial schema — create all four staging tables.
# File: 0001_initial.py
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

"""Initial schema — create all four staging tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── settings ──
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("section", sa.String(128), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_disk", sa.Text, nullable=True),
        sa.Column("value_staged", sa.Text, nullable=True),
        sa.Column("default_value", sa.Text, nullable=True),
        sa.Column("transport_type", sa.String(32), default="general"),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("is_dirty", sa.Boolean, default=False),
        sa.Column("is_orphan", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("section", "key", name="uq_settings_section_key"),
    )
    op.create_index("ix_settings_section", "settings", ["section"])

    # ── protocol_registers ──
    op.create_table(
        "protocol_registers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("protocol_group", sa.String(128), nullable=False),
        sa.Column("protocol_name", sa.String(128), nullable=False),
        sa.Column("registry_type", sa.String(16), nullable=False),
        sa.Column("register_address", sa.String(32), nullable=False),
        sa.Column("variable_name", sa.String(128), nullable=False),
        sa.Column("documented_name", sa.String(128), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("data_type", sa.String(32), nullable=True),
        sa.Column("values_range", sa.Text, nullable=True),
        sa.Column("adjustments", sa.Text, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("read_interval", sa.String(32), nullable=True),
        sa.Column("write_mode_protocol", sa.String(8), default="R"),
        sa.Column("user_write_enabled", sa.Boolean, default=False),
        sa.Column("mask_enabled", sa.Boolean, default=True),
        sa.Column("screen_enabled", sa.Boolean, default=False),
        sa.Column("user_write_enabled_disk", sa.Boolean, default=False),
        sa.Column("mask_enabled_disk", sa.Boolean, default=True),
        sa.Column("screen_enabled_disk", sa.Boolean, default=False),
        sa.Column("is_dirty", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "protocol_name", "registry_type", "register_address",
            name="uq_register_protocol_type_addr"
        ),
    )
    op.create_index("ix_pr_protocol_name", "protocol_registers", ["protocol_name"])
    op.create_index("ix_pr_protocol_group", "protocol_registers", ["protocol_group"])

    # ── config_backups ──
    op.create_table(
        "config_backups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("filepath", sa.Text, nullable=False),
        sa.Column("file_size_bytes", sa.Integer, nullable=True),
        sa.Column("trigger", sa.String(32), default="manual"),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # ── app_state ──
    op.create_table(
        "app_state",
        sa.Column("id", sa.Integer, primary_key=True, default=1),
        sa.Column("last_scan_at", sa.DateTime, nullable=True),
        sa.Column("last_commit_at", sa.DateTime, nullable=True),
        sa.Column("has_dirty_settings", sa.Boolean, default=False),
        sa.Column("has_dirty_protocols", sa.Boolean, default=False),
        sa.Column("has_orphans", sa.Boolean, default=False),
        sa.Column("dirty_settings_count", sa.Integer, default=0),
        sa.Column("dirty_protocols_count", sa.Integer, default=0),
        sa.Column("orphan_count", sa.Integer, default=0),
        sa.Column("scanner_status", sa.String(32), default="idle"),
        sa.Column("scanner_last_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_table("config_backups")
    op.drop_table("protocol_registers")
    op.drop_table("settings")
