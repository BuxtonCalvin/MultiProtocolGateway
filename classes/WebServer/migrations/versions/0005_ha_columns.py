"""add_protocol_register_home_assistant_columns

Adds the optional Home Assistant attributes carried by the protocol CSVs'
"ha device class", "ha state class" and "ha entity category" columns.

The columns are added NULLable with no default on purpose. NULL means "never
loaded from the CSV" — which is true of every row that existed before this
migration — whereas "" means "loaded, and blank". The scanner fills NULLs from
the CSV on its next pass (even for rows with uncommitted edits), so a commit
can never rewrite a CSV with empty cells over values that are really there.

Revision ID: 0005_ha_columns
Revises: 0004_pending_delete
Create Date: 2026-10-06 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_ha_columns'
down_revision: Union[str, None] = '0004_pending_delete'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ha_device_class', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('ha_state_class', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('ha_entity_category', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('protocol_registers', schema=None) as batch_op:
        batch_op.drop_column('ha_entity_category')
        batch_op.drop_column('ha_state_class')
        batch_op.drop_column('ha_device_class')
