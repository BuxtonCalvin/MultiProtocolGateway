"""add_protocol_register_pending_delete

Revision ID: 0004_pending_delete
Revises: 0003_orphaned_filter_names
Create Date: 2026-08-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004_pending_delete'
down_revision: Union[str, None] = '0003_orphaned_filter_names'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('pending_delete', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )


def downgrade() -> None:
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        batch_op.drop_column('pending_delete')
