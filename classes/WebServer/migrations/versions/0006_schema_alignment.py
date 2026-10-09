"""align_schema_with_models

Resolves drift between the SQLAlchemy models and the schema the migrations
actually produced. Migration 0001 declared columns with ``default=...`` — a
Python-side ORM default that has no effect on DDL — and omitted ``nullable=False``,
so those columns were created NULLable with no database default. The models
(``Mapped[bool]`` etc.) have always treated them as required, and migrations
0002-0004 correctly used ``nullable=False`` + ``server_default``.

This migration brings the database in line with the models:

* Backfills any NULL in the affected columns with the column's documented
  default, then makes the column NOT NULL with a matching database default
  (so an insert that omits the column — raw SQL, a future bulk path — can no
  longer produce a NULL).
* ``setting_descriptions.key``: replaces the redundant pair "UNIQUE constraint +
  non-unique index" with the single unique index the model declares.
  Uniqueness is enforced throughout.

The matching ``server_default`` declarations were added to the models in the
same change, and ``env.py`` now compares types and server defaults so this kind
of drift is reported by ``alembic check`` / autogenerate.

Revision ID: 0006_schema_alignment
Revises: 0005_ha_columns
Create Date: 2026-10-07 00:00:00.000000

"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.interfaces import ReflectedIndex

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import ReflectedIndex, ReflectedUniqueConstraint
# revision identifiers, used by Alembic.
revision: str = '0006_schema_alignment'
down_revision: Union[str, None] = '0005_ha_columns'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BOOL = sa.Boolean()
_INT = sa.Integer()

# table -> [(column, existing type, SQL literal used for both the NULL backfill and the DEFAULT)]
# Literals must match the models' ``default=`` / ``server_default=``.
_TIGHTEN: dict[str, list[tuple[str, sa.types.TypeEngine[Any], str]]] = {
    'settings': [
        ('transport_type', sa.String(32), "'general'"),
        ('is_active', _BOOL, '1'),
        ('is_dirty', _BOOL, '0'),
        ('is_orphan', _BOOL, '0'),
    ],
    'protocol_registers': [
        ('write_mode_protocol', sa.String(8), "'R'"),
        ('is_dirty', _BOOL, '0'),
    ],
    'device_protocol_selections': [
        ('user_write_enabled', _BOOL, '0'),
        ('mask_enabled', _BOOL, '0'),
        ('screen_enabled', _BOOL, '0'),
        ('user_write_enabled_disk', _BOOL, '0'),
        ('mask_enabled_disk', _BOOL, '0'),
        ('screen_enabled_disk', _BOOL, '0'),
        ('is_dirty', _BOOL, '0'),
    ],
    'config_backups': [
        ('trigger', sa.String(32), "'manual'"),
    ],
    'app_state': [
        ('has_dirty_settings', _BOOL, '0'),
        ('has_dirty_protocols', _BOOL, '0'),
        ('has_orphans', _BOOL, '0'),
        ('dirty_settings_count', _INT, '0'),
        ('dirty_protocols_count', _INT, '0'),
        ('orphan_count', _INT, '0'),
        ('scanner_status', sa.String(32), "'idle'"),
    ],
    'setting_descriptions': [
        ('is_dirty', _BOOL, '0'),
    ],
}


def upgrade() -> None:
    bind: sa.Connection = op.get_bind()

    # 1. Backfill NULLs first — NOT NULL cannot be applied while any remain.
    for table, columns in _TIGHTEN.items():
        for name, _type, default in columns:
            bind.execute(
                sa.text(f"UPDATE {table} SET {name} = {default} WHERE {name} IS NULL")  # noqa: S608
            )  # noqa: S608

    # 2. Tighten. One batch (= one table rebuild on SQLite) per table.
    inspector: sa.Inspector = sa.inspect(bind)
    assert inspector is not None

    # Use the concrete SQLAlchemy structural types instead of raw dict[str, Any]
    raw_constraints: list[ReflectedUniqueConstraint] = (
        inspector.get_unique_constraints("setting_descriptions")
    )
    unique_constraints: set[str] = {
        str(u["name"]) for u in raw_constraints if u.get("name")
    }

    raw_indexes: list[ReflectedIndex] = inspector.get_indexes(
        "setting_descriptions"
    )
    indexes: dict[str, ReflectedIndex] = {
        str(i["name"]): i for i in raw_indexes if i.get("name")
    }

    for table, columns in _TIGHTEN.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, type_, default in columns:
                batch_op.alter_column(
                    name,
                    existing_type=type_,
                    nullable=False,
                    server_default=sa.text(default),
                )
            if table == "setting_descriptions":
                # Written defensively: a database may not have every object.
                if "uq_setting_descriptions_key" in unique_constraints:
                    batch_op.drop_constraint(
                        "uq_setting_descriptions_key", type_="unique"
                    )

                existing: ReflectedIndex | None = indexes.get(
                    "ix_setting_descriptions_key"
                )
                if existing is not None and not existing.get("unique", False):
                    batch_op.drop_index("ix_setting_descriptions_key")
                if existing is None or not existing.get("unique", False):
                    batch_op.create_index(
                        "ix_setting_descriptions_key", ["key"], unique=True
                    )

def downgrade() -> None:
    # Restores the previous (drifted) shape. Data is untouched; only the NOT NULL
    # and DEFAULT clauses and the key index/constraint arrangement are reverted.
    for table, columns in _TIGHTEN.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, type_, _default in columns:
                batch_op.alter_column(name, existing_type=type_, nullable=True, server_default=None)
            if table == 'setting_descriptions':
                batch_op.drop_index('ix_setting_descriptions_key')
                batch_op.create_unique_constraint('uq_setting_descriptions_key', ['key'])
                batch_op.create_index('ix_setting_descriptions_key', ['key'], unique=False)
