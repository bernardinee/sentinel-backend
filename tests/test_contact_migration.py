"""Legacy contacts must never be guessed onto a shared device account."""
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_contact_migration_backfills_only_unambiguous_owner(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'contacts.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "c71b07f04412")

    engine = create_engine(url)
    with engine.begin() as connection:
        for device_id in ("device-one", "device-shared"):
            connection.execute(text(
                "INSERT INTO devices (id, device_id, registered_at, status) "
                "VALUES (:id, :id, CURRENT_TIMESTAMP, 'unknown')"
            ), {"id": device_id})
        for user_id, device_id in (
            ("vanessa", "device-one"),
            ("friend", "device-shared"),
            ("roommate", "device-shared"),
        ):
            connection.execute(text(
                "INSERT INTO users (id, name, email, phone, password_hash, role, "
                "device_id, active, sessions_valid_from, created_at, updated_at) "
                "VALUES (:id, :id, :email, '123', 'hash', 'driver', :device, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": user_id, "email": f"{user_id}@example.com", "device": device_id})
        for contact_id, device_id in (
            ("owned-contact", "device-one"),
            ("ambiguous-contact", "device-shared"),
        ):
            connection.execute(text(
                "INSERT INTO emergency_contacts (id, device_id, name, phone, priority, active) "
                "VALUES (:id, :device, :id, '123', 1, 1)"
            ), {"id": contact_id, "device": device_id})

    command.upgrade(config, "head")
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT id, owner_user_id FROM emergency_contacts ORDER BY id"
        )).all()
    assert rows == [("ambiguous-contact", None), ("owned-contact", "vanessa")]
