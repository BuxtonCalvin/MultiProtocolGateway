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
