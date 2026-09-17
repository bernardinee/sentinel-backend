"""Pydantic v2 request/response models — no bare dicts on the wire (§5)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WINDOW_SAMPLES = 500
TARGET_FS = 100


# ── Ingest (§5.1) ─────────────────────────────────────────────────────────────

class TriggerBlock(BaseModel):
    peak_g: float
    jerk_gs: float


class GpsBlock(BaseModel):
    lat: float | None = None
    lon: float | None = None
    valid: bool = False
    satellites: int | None = None
    speed_kmh: float | None = None
    hdop: float | None = None


class DeviceBlock(BaseModel):
    uptime_s: int | None = None
    free_heap: int | None = None
    rssi: int | None = None
    firmware_version: str | None = None


class WindowBlock(BaseModel):
    fs_hz: int
    units_accel: str
    units_gyro: str
    ax: list[float]
    ay: list[float]
    az: list[float]
    gx: list[float]
    gy: list[float]
    gz: list[float]

    @field_validator("ax", "ay", "az", "gx", "gy", "gz")
    @classmethod
    def exactly_500(cls, v: list[float], info):
        if len(v) != WINDOW_SAMPLES:
            raise ValueError(
                f"{info.field_name} must contain exactly {WINDOW_SAMPLES} samples, got {len(v)}")
        return v

    @field_validator("fs_hz")
    @classmethod
    def fs_100(cls, v: int):
        if v != TARGET_FS:
            raise ValueError(f"fs_hz must be {TARGET_FS}, got {v}")
        return v

    @field_validator("units_accel")
    @classmethod
    def accel_in_g(cls, v: str):
        if v != "g":
            raise ValueError(f'units_accel must be "g", got "{v}" — the model is trained on g')
        return v


class EventIn(BaseModel):
    device_id: str
    event_id: str
    detected_at: datetime | None = None
    trigger: TriggerBlock
    gps: GpsBlock = GpsBlock()
    device: DeviceBlock = DeviceBlock()
    window: WindowBlock


class EventAck(BaseModel):
    """Response to the ESP32 — small and FLAT (§5.1), one level deep only.
    The device parses this on a constrained heap."""
    event_id: str
    severity_class: int | None
    severity_name: str | None
    confidence: float | None
    p_crash: float | None
    accident_confirmed: bool | None
    label_source: str | None
    peak_g: float | None
    classification_pending: bool = False


class HeartbeatIn(BaseModel):
    device_id: str
    gps: GpsBlock = GpsBlock()
    device: DeviceBlock = DeviceBlock()
    battery_v: float | None = None


# -- Driver authentication ---------------------------------------------------

class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=7, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    device_id: str = Field(min_length=1, max_length=64)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        email = value.strip().lower()
        if email.count("@") != 1 or "." not in email.rsplit("@", 1)[1]:
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("name", "phone", "device_id")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value


class ChangePasswordIn(BaseModel):
    """Change your own password.

    The current password is required even though the caller is already
    authenticated: an access token sitting in an unattended browser should not
    be enough to take permanent ownership of the account.
    """
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class ResponderCreateIn(BaseModel):
    """Create a colleague's dispatch-console account.

    Restricted to signed-in responders — a dispatch team adds its own
    operators. Still not reachable from /auth/register, which mints drivers
    only, so an anonymous caller can never obtain responder rights.
    """
    name: str = Field(min_length=2, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(default="", max_length=32)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        email = value.strip().lower()
        if email.count("@") != 1 or "." not in email.rsplit("@", 1)[1]:
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value


class ResponderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str
    phone: str
    role: str
    active: bool
    created_at: datetime


class ResponderPatch(BaseModel):
    active: bool | None = None
    name: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=12, max_length=128)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    phone: str
    role: str
    #: Drivers are bound to one device; responders are not, so this is null
    #: for them.
    device_id: str | None = None


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


# ── Query (§5.2) ──────────────────────────────────────────────────────────────

class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    device_id: str
    label: str | None
    firmware_version: str | None
    registered_at: datetime
    last_seen_at: datetime | None
    status: str
    last_lat: float | None
    last_lon: float | None
    last_satellites: int | None
    last_uptime_s: int | None
    last_rssi: int | None


class DispatchEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    at: datetime
    actor: str
    action: str
    note: str | None


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
    id: str
    event_id: str
    device_id: str
    detected_at: datetime | None
    received_at: datetime
    severity_class: int | None
    severity_name: str | None
    confidence: float | None
    p_crash: float | None
    model_severity: str | None
    accident_confirmed: bool | None
    probabilities: dict | None
    peak_g: float | None
    excursion_ms: float | None
    impulse_gs: float | None
    signature_match: bool | None
    label_source: str | None
    unit_scale_applied: float | None
    inference_time_ms: float | None
    classification_pending: bool
    trigger_peak_g: float | None
    trigger_jerk_gs: float | None
    lat: float | None
    lon: float | None
    gps_valid: bool | None
    satellites: int | None
    speed_kmh: float | None
    status: str
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    resolved_at: datetime | None
    notes: str | None


class IncidentDetailOut(IncidentOut):
    dispatch_events: list[DispatchEventOut] = []


class DriverIncidentOut(IncidentOut):
    """The response milestones a driver may see without dispatcher-only data."""
    assigned_at: datetime | None = None
    en_route_at: datetime | None = None
    arrived_at: datetime | None = None
    responding_units: list[str] = []


class IncidentPage(BaseModel):
    items: list[IncidentOut]
    total: int
    page: int
    page_size: int


class WindowOut(BaseModel):
    incident_id: str
    fs_hz: int
    ax: list[float]
    ay: list[float]
    az: list[float]
    gx: list[float]
    gy: list[float]
    gz: list[float]


class HeartbeatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    at: datetime
    lat: float | None
    lon: float | None
    satellites: int | None
    uptime_s: int | None
    free_heap: int | None
    rssi: int | None
    battery_v: float | None


class StatsSummary(BaseModel):
    total_incidents: int
    by_severity: dict[str, int]
    by_label_source: dict[str, int]
    by_status: dict[str, int]
    mean_inference_ms: float | None
    mean_end_to_end_s: float | None
    last_24h: int
    last_7d: int
    last_30d: int


# ── Actions (§5.3) ────────────────────────────────────────────────────────────

class AcknowledgeIn(BaseModel):
    actor: str
    note: str | None = None


class DispatchIn(BaseModel):
    actor: str
    action: Literal["assign", "en_route", "on_scene"]
    note: str | None = None


class ResolveIn(BaseModel):
    actor: str
    outcome: Literal["resolved", "false_alarm"]
    note: str | None = None


# ── Sentinel hooks (§5.4) ─────────────────────────────────────────────────────

class PanicIn(BaseModel):
    device_id: str
    lat: float | None = None
    lon: float | None = None
    note: str | None = None


class ProtectionStatus(BaseModel):
    device_id: str
    registered: bool
    online: bool
    monitoring_active: bool
    gps_locked: bool
    satellites: int | None
    last_heartbeat_at: datetime | None
    last_heartbeat_age_s: float | None
    open_incidents: int


class ContactIn(BaseModel):
    name: str
    phone: str
    relationship: str | None = None
    priority: int = 1
    active: bool = True


class ContactPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    relationship: str | None = None
    priority: int | None = None
    active: bool | None = None


# ── Response units / dispatch routing ────────────────────────────────────────

UnitType = Literal["AMBULANCE", "FIRE", "POLICE", "RESCUE"]
UnitStatus = Literal["available", "dispatched", "en_route", "on_scene", "out_of_service"]


class UnitIn(BaseModel):
    call_sign: str
    unit_type: UnitType
    station_name: str
    home_lat: float
    home_lon: float
    crew_size: int | None = None
    contact_phone: str | None = None


class UnitPatch(BaseModel):
    station_name: str | None = None
    status: UnitStatus | None = None
    current_lat: float | None = None
    current_lon: float | None = None
    crew_size: int | None = None
    contact_phone: str | None = None
    active: bool | None = None


class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    call_sign: str
    unit_type: str
    station_name: str
    home_lat: float
    home_lon: float
    current_lat: float | None
    current_lon: float | None
    status: str
    crew_size: int | None
    contact_phone: str | None
    assigned_incident_id: str | None
    active: bool
    last_update: datetime
    #: Dispatch-run state, present while the unit is responding. The dashboard
    #: animates the unit along `route_geometry` from `dispatched_at`, using
    #: `route_eta_s` as the trip duration; all three are null when idle.
    dispatched_at: datetime | None = None
    route_geometry: list[list[float]] | None = None
    route_eta_s: float | None = None


class RouteOut(BaseModel):
    distance_km: float
    duration_min: float
    geometry: list[list[float]] = []
    source: str  # "osrm" (road route) | "straight_line" (estimate)


class DispatchOption(BaseModel):
    """A unit ranked by how fast it can actually reach the scene by road."""
    unit: UnitOut
    route: RouteOut
    eta_min: float
    recommended: bool  # top pick for its service type


class DispatchOptions(BaseModel):
    incident_id: str
    incident_lat: float
    incident_lon: float
    required_types: list[str]
    #: Units free to be sent, fastest first.
    options: list[DispatchOption]
    #: Units already committed to THIS incident, with their live routes. Kept
    #: separate from `options` (which means "available to dispatch") because a
    #: responding unit's route is what the dispatcher most needs on the map.
    responding: list[DispatchOption] = []
    routing_source: str
    note: str | None = None


class AssignUnitIn(BaseModel):
    call_sign: str
    actor: str
    note: str | None = None


class UnitStatusIn(BaseModel):
    status: UnitStatus
    actor: str
    note: str | None = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: str
    device_id: str
    name: str
    phone: str
    relationship: str | None = Field(default=None, validation_alias="relationship_")
    priority: int
    active: bool
