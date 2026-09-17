"""response unit dispatch-run fields (movement + auto status)

Adds the per-dispatch run state a unit carries while responding: when it was
dispatched, the road route it is travelling, and that route's ETA. The map
animates the unit along `route_geometry`, and the background mover advances its
status (dispatched -> en_route -> on_scene) from `dispatched_at` + `route_eta_s`.

Revision ID: d4a1f2b6c8e0
Revises: e82b2c7a41d0
Create Date: 2026-09-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d4a1f2b6c8e0"
# Chained after the contacts-scoping migration so history stays linear — the two
# were authored on parallel branches off c71b07f04412 and touch unrelated tables.
down_revision: Union[str, None] = "e82b2c7a41d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table keeps this portable to SQLite (used in tests/local dev).
    with op.batch_alter_table("response_units") as batch:
        batch.add_column(sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("route_geometry", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("route_eta_s", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("response_units") as batch:
        batch.drop_column("route_eta_s")
        batch.drop_column("route_geometry")
        batch.drop_column("dispatched_at")
