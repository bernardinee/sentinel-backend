"""responder accounts have no device

Drivers own exactly one Sentinel device; responders dispatch across the whole
fleet and own none. `users.device_id` therefore becomes nullable. It stays
UNIQUE, which still enforces one driver per device — SQL treats multiple NULLs
as distinct, so any number of responders coexist.

batch_alter_table is used because SQLite (which the test suite runs on) cannot
ALTER COLUMN; it rebuilds the table instead. On PostgreSQL it emits a plain
ALTER.

Revision ID: 03e0c9003e83
Revises: a912ec7d41b0
Create Date: 2026-09-15 18:18:15.420437

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '03e0c9003e83'
down_revision: Union[str, None] = 'a912ec7d41b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "device_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )


def downgrade() -> None:
    # Responder rows have a NULL device_id and cannot satisfy a NOT NULL
    # constraint, so they are removed before it is restored.
    op.execute("DELETE FROM users WHERE device_id IS NULL")
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "device_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
