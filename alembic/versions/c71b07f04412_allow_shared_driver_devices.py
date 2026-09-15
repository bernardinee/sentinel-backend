"""allow shared driver devices

Revision ID: c71b07f04412
Revises: bf2d51828375
Create Date: 2026-09-16 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c71b07f04412"
down_revision: Union[str, None] = "bf2d51828375"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_users_device_id", table_name="users")
    op.create_index("ix_users_device_id", "users", ["device_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_device_id", table_name="users")
    op.create_index("ix_users_device_id", "users", ["device_id"], unique=True)
