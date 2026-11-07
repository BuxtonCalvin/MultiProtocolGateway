"""add_protocol_file_states

Revision ID: 0007_protocol_file_states
Revises: 0006_schema_alignment
Create Date: 2026-11-05 00:00:00.000000

Remembers the hash of each protocol CSV when its ProtocolRegister rows were last
reconciled with it, so the scanner can remove stale rows only for files that changed
(for example after a docker pull brings in modified protocol maps).  Nothing is
backfilled: a protocol with no row here is reconciled on the next scan.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007_protocol_file_states'
down_revision: Union[str, None] = '0006_schema_alignment'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'protocol_file_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('protocol_name', sa.String(length=128), nullable=False),
        sa.Column('file_sha256', sa.String(length=64), nullable=False),
        sa.Column('reconciled_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_protocol_file_states')),
        sa.UniqueConstraint('protocol_name', name='uq_protocol_file_state_protocol'),
    )


def downgrade() -> None:
    op.drop_table('protocol_file_states')
