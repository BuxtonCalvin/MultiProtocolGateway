"""Add paired_high_address to protocol_registers and device_protocol_selections

Revision ID: 0003_paired_high_address
Revises: 0002_setting_descriptions
Create Date: 2026-06-22

This column stores the register address of the _h half of a merged 32-bit
register pair.  NULL for all non-paired (16-bit) registers.  The scanner
merges consecutive _l/_h CSV rows into a single stem-named ProtocolRegister
row and records the high address here so the UI can display the address range
(e.g. "40-41") and render an expand/collapse detail view.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_paired_high_address"
down_revision: str = "0002_setting_descriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # protocol_registers — stores the merged stem row with the _h address
    with op.batch_alter_table("protocol_registers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "paired_high_address",
                sa.String(),
                nullable=True,
                server_default=None,
            )
        )

    # device_protocol_selections — mirrors the column so device-scoped views
    # can also expose the address range without joining back to protocol_registers
    with op.batch_alter_table("device_protocol_selections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "paired_high_address",
                sa.String(),
                nullable=True,
                server_default=None,
            )
        )


def downgrade() -> None:
    # SQLite does not support DROP COLUMN natively before v3.35.
    # batch_alter_table rebuilds the table, which works on all SQLite versions.
    with op.batch_alter_table("protocol_registers") as batch_op:
        batch_op.drop_column("paired_high_address")

    with op.batch_alter_table("device_protocol_selections") as batch_op:
        batch_op.drop_column("paired_high_address")
