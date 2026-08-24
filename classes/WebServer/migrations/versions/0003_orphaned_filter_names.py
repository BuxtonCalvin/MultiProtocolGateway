"""add_orphaned_filter_names

Revision ID: 0003_orphaned_filter_names
Revises: 0002_initial
Create Date: 2026-08-22 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_orphaned_filter_names'
down_revision: Union[str, None] = '0002_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'orphaned_filter_names',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_name', sa.String(length=128), nullable=False),
        sa.Column('file_type', sa.String(length=16), nullable=False),
        sa.Column('filename', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('detected_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_orphaned_filter_names')),
        sa.UniqueConstraint(
            'device_name', 'file_type', 'name',
            name='uq_orphaned_filter_device_type_name',
        ),
    )
    op.create_index(
        op.f('ix_orphaned_filter_names_device_name'),
        'orphaned_filter_names',
        ['device_name'],
        unique=False,
    )

    with op.batch_alter_table('app_state', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('has_orphaned_filters', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )
        batch_op.add_column(
            sa.Column('orphaned_filter_count', sa.Integer(), nullable=False, server_default=sa.text('0'))
        )


def downgrade() -> None:
    with op.batch_alter_table('app_state', schema=None) as batch_op:
        batch_op.drop_column('orphaned_filter_count')
        batch_op.drop_column('has_orphaned_filters')

    op.drop_index(op.f('ix_orphaned_filter_names_device_name'), table_name='orphaned_filter_names')
    op.drop_table('orphaned_filter_names')
