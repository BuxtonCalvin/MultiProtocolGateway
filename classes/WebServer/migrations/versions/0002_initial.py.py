"""add_synthetic_and_json_desc_fields

Revision ID: 0002_initial
Revises: 0001_initial
Create Date: 2026-08-16 18:44:24.499420

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_initial'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Safely alter the structure using explicit naming properties
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        # 1. Add new columns safely with functional server-side fallbacks
        batch_op.add_column(sa.Column('is_synthetic', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('is_json_desc', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('source_variable_name', sa.String(length=128), nullable=True))

        # 2. Safely align and upgrade database indices to fix mismatch warnings
        # We wrap these in a try/except or drop them directly to match your latest models
        try:
            batch_op.drop_index('ix_pr_protocol_group')
            batch_op.drop_index('ix_pr_protocol_name')
        except Exception:  # noqa: S110
            pass # Handles cases where local developer database name variance exists

        batch_op.create_index('ix_protocol_registers_protocol_group', ['protocol_group'], unique=False)
        batch_op.create_index('ix_protocol_registers_protocol_name', ['protocol_name'], unique=False)

        # 3. Completely eliminate the ghost column allocations
        batch_op.drop_column('mask_enabled')
        batch_op.drop_column('mask_enabled_disk')
        batch_op.drop_column('screen_enabled')
        batch_op.drop_column('screen_enabled_disk')
        batch_op.drop_column('user_write_enabled')
        batch_op.drop_column('user_write_enabled_disk')


def downgrade() -> None:
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        batch_op.drop_index('ix_protocol_registers_protocol_name')
        batch_op.drop_index('ix_protocol_registers_protocol_group')
        batch_op.create_index('ix_pr_protocol_name', ['protocol_name'], unique=False)
        batch_op.create_index('ix_pr_protocol_group', ['protocol_group'], unique=False)

        batch_op.drop_column('source_variable_name')
        batch_op.drop_column('is_json_desc')
        batch_op.drop_column('is_synthetic')

        batch_op.add_column(sa.Column('user_write_enabled_disk', sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column('user_write_enabled', sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column('screen_enabled_disk', sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column('screen_enabled', sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column('mask_enabled_disk', sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column('mask_enabled', sa.BOOLEAN(), nullable=True))
