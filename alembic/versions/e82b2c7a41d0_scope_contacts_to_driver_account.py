"""Scope emergency contacts to a driver account.

Legacy device contacts can be attributed only when one driver owns the
device. Contacts on a shared device remain unowned and invisible to drivers;
dispatchers can review them without leaking them to the wrong account.

Revision ID: e82b2c7a41d0
Revises: c71b07f04412
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e82b2c7a41d0"
down_revision: Union[str, None] = "c71b07f04412"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("emergency_contacts", sa.Column("owner_user_id", sa.String(length=36), nullable=True))
    op.create_index("ix_emergency_contacts_owner_user_id", "emergency_contacts", ["owner_user_id"])
    with op.batch_alter_table("emergency_contacts") as batch:
        batch.create_foreign_key(
            "fk_emergency_contacts_owner_user_id_users", "users", ["owner_user_id"], ["id"])

    # An existing contact can be assigned safely only when its device has a
    # single driver. Do not guess ownership for a shared ESP32.
    op.execute("""
        UPDATE emergency_contacts
        SET owner_user_id = (
            SELECT MIN(users.id) FROM users
            WHERE users.device_id = emergency_contacts.device_id
              AND users.role = 'driver'
        )
        WHERE (
            SELECT COUNT(*) FROM users
            WHERE users.device_id = emergency_contacts.device_id
              AND users.role = 'driver'
        ) = 1
    """)


def downgrade() -> None:
    op.drop_index("ix_emergency_contacts_owner_user_id", table_name="emergency_contacts")
    with op.batch_alter_table("emergency_contacts") as batch:
        batch.drop_constraint("fk_emergency_contacts_owner_user_id_users", type_="foreignkey")
        batch.drop_column("owner_user_id")
