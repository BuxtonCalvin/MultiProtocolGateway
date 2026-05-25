# Description: Add setting_descriptions table.
# File: 0002_setting_descriptions.py
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

"""Add setting_descriptions table.

Revision ID: 0002_setting_descriptions
Revises: 0001_initial
Create Date: 2026-04-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_setting_descriptions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "setting_descriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("transports", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("description_disk", sa.Text, nullable=True),
        sa.Column("is_dirty", sa.Boolean, default=False),
    )


def downgrade() -> None:
    op.drop_table("setting_descriptions")
