"""Data model (§4). JSON columns keep the schema portable (Postgres + SQLite
for tests); raw windows are stored as JSON float arrays — the reproducible
evidence trail Phase 1 failed to keep."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC so arithmetic against
    utcnow() works on both engines."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128))
    firmware_version: Mapped[str | None] = mapped_column(String(32))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online|offline|unknown
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lon: Mapped[float | None] = mapped_column(Float)
    last_satellites: Mapped[int | None] = mapped_column(Integer)
    last_uptime_s: Mapped[int | None] = mapped_column(Integer)
    last_rssi: Mapped[int | None] = mapped_column(Integer)

    incidents: Mapped[list["Incident"]] = relationship(back_populates="device")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16), default="driver")
    # Drivers own exactly one device; responders own none, so this is nullable.
    # Still unique, which keeps "one driver per device" enforced — SQL treats
    # multiple NULLs as distinct, so any number of responders coexist.
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id"), unique=True, index=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_hash: Mapped[str | None] = mapped_column(String(64))


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # classification (nullable until inference completes — §8.7)
    severity_class: Mapped[int | None] = mapped_column(Integer, index=True)
    severity_name: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    p_crash: Mapped[float | None] = mapped_column(Float)
    model_severity: Mapped[str | None] = mapped_column(String(16))
    accident_confirmed: Mapped[bool | None] = mapped_column(Boolean, index=True)
    probabilities: Mapped[dict | None] = mapped_column(JSON)
    peak_g: Mapped[float | None] = mapped_column(Float)
    excursion_ms: Mapped[float | None] = mapped_column(Float)
    impulse_gs: Mapped[float | None] = mapped_column(Float)
    signature_match: Mapped[bool | None] = mapped_column(Boolean)
    label_source: Mapped[str | None] = mapped_column(String(32), index=True)
    unit_scale_applied: Mapped[float | None] = mapped_column(Float)
    inference_time_ms: Mapped[float | None] = mapped_column(Float)
    classification_pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # trigger + location
    trigger_peak_g: Mapped[float | None] = mapped_column(Float)
    trigger_jerk_gs: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    gps_valid: Mapped[bool | None] = mapped_column(Boolean)
    satellites: Mapped[int | None] = mapped_column(Integer)
    speed_kmh: Mapped[float | None] = mapped_column(Float)

    # lifecycle
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    device: Mapped[Device] = relationship(back_populates="incidents")
    window: Mapped["IncidentWindow | None"] = relationship(back_populates="incident", uselist=False)
    dispatch_events: Mapped[list["DispatchEvent"]] = relationship(
        back_populates="incident", order_by="DispatchEvent.at")


Index("ix_incidents_received_severity", Incident.received_at, Incident.severity_class)


class IncidentWindow(Base):
    __tablename__ = "incident_windows"

    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), primary_key=True)
    fs_hz: Mapped[int] = mapped_column(Integer, default=100)
    ax: Mapped[list] = mapped_column(JSON)
    ay: Mapped[list] = mapped_column(JSON)
    az: Mapped[list] = mapped_column(JSON)
    gx: Mapped[list] = mapped_column(JSON)
    gy: Mapped[list] = mapped_column(JSON)
    gz: Mapped[list] = mapped_column(JSON)

    incident: Mapped[Incident] = relationship(back_populates="window")


class DeviceHeartbeat(Base):
    __tablename__ = "device_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    satellites: Mapped[int | None] = mapped_column(Integer)
    uptime_s: Mapped[int | None] = mapped_column(Integer)
    free_heap: Mapped[int | None] = mapped_column(Integer)
    rssi: Mapped[int | None] = mapped_column(Integer)
    battery_v: Mapped[float | None] = mapped_column(Float)


class EmergencyContact(Base):
    __tablename__ = "emergency_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32))
    relationship_: Mapped[str | None] = mapped_column("relationship", String(64))
    priority: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResponseUnit(Base):
    """An emergency service unit the dispatcher can send to an incident.

    This is operator-maintained roster data (like emergency contacts), not
    sensor data — it is entered by a human, never fabricated by the system.
    """
    __tablename__ = "response_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_sign: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    unit_type: Mapped[str] = mapped_column(String(16), index=True)  # AMBULANCE|FIRE|POLICE|RESCUE
    station_name: Mapped[str] = mapped_column(String(128))
    home_lat: Mapped[float] = mapped_column(Float)
    home_lon: Mapped[float] = mapped_column(Float)
    # Live position when the unit reports one; falls back to the station.
    current_lat: Mapped[float | None] = mapped_column(Float)
    current_lon: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="available", index=True)
    crew_size: Mapped[int | None] = mapped_column(Integer)
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    assigned_incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_update: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def position(self) -> tuple[float, float]:
        """Live position if reported, else the home station."""
        if self.current_lat is not None and self.current_lon is not None:
            return self.current_lat, self.current_lon
        return self.home_lat, self.home_lon


class DispatchEvent(Base):
    __tablename__ = "dispatch_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))  # acknowledge|assign|en_route|on_scene|resolve|false_alarm
    note: Mapped[str | None] = mapped_column(Text)

    incident: Mapped[Incident] = relationship(back_populates="dispatch_events")
