"""Ingest path (§5.1): ESP32 → backend. The handler sequence is load-bearing:
validate → idempotency → device upsert → persist BEFORE inference → classify →
update → broadcast → flat ACK to the device.
"""
import logging
import statistics
from math import sqrt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Device, DeviceHeartbeat, Incident, IncidentWindow, utcnow
from app.modules.inference.service import InferenceUnavailable, classify
from app.modules.ws import manager
from app.schemas import EventAck, EventIn, HeartbeatIn, IncidentOut

log = logging.getLogger(__name__)
router = APIRouter(tags=["ingest"])


def _upsert_device(db: Session, device_id: str, gps, dev) -> Device:
    device = db.execute(select(Device).where(Device.device_id == device_id)).scalar_one_or_none()
    if device is None:
        device = Device(device_id=device_id)
        db.add(device)
    device.last_seen_at = utcnow()
    device.status = "online"
    if dev is not None:
        if dev.firmware_version:
            device.firmware_version = dev.firmware_version
        if dev.uptime_s is not None:
            device.last_uptime_s = dev.uptime_s
        if dev.rssi is not None:
            device.last_rssi = dev.rssi
    if gps is not None and gps.valid and gps.lat is not None and gps.lon is not None:
        device.last_lat, device.last_lon = gps.lat, gps.lon
        device.last_satellites = gps.satellites
    db.flush()
    return device


def _assert_units_are_g(window) -> None:
    """§8.2 — explicit unit assertion. The scaler expects az mean ≈ 0.92 g; a
    median resultant magnitude > 5 almost certainly means m/s² was sent."""
    mags = [sqrt(x * x + y * y + z * z)
            for x, y, z in zip(window.ax, window.ay, window.az)]
    med = statistics.median(mags)
    if med > 5.0:
        raise HTTPException(
            status_code=422,
            detail=(f"Rejected: median resultant magnitude {med:.2f} looks like m/s², "
                    f"not g (gravity ≈ 9.81 m/s² vs ≈ 1.0 g). A unit error already "
                    f"invalidated a training class once — fix the sender; the backend "
                    f"will not silently rescale."))


def _apply_classification(incident: Incident, res: dict) -> None:
    sig = res.get("crash_signature", {})
    incident.severity_class = res.get("severity_class")
    incident.severity_name = res.get("severity_name")
    incident.confidence = res.get("confidence")
    incident.p_crash = res.get("p_crash")
    incident.model_severity = res.get("model_severity")
    incident.accident_confirmed = res.get("accident_confirmed")
    incident.probabilities = res.get("probabilities")
    incident.peak_g = sig.get("peak_g")
    incident.excursion_ms = sig.get("excursion_ms")
    incident.impulse_gs = sig.get("impulse_gs")
    incident.signature_match = sig.get("signature_match")
    incident.label_source = res.get("label_source")
    incident.unit_scale_applied = res.get("unit_scale_applied")
    incident.inference_time_ms = res.get("inference_time_ms")
    incident.classification_pending = False


def _ack(incident: Incident) -> EventAck:
    return EventAck(
        event_id=incident.event_id,
        severity_class=incident.severity_class,
        severity_name=incident.severity_name,
        confidence=incident.confidence,
        p_crash=incident.p_crash,
        accident_confirmed=incident.accident_confirmed,
        label_source=incident.label_source,
        peak_g=incident.peak_g,
        classification_pending=incident.classification_pending,
    )


@router.post("/events", response_model=EventAck)
async def ingest_event(
    body: EventIn,
    db: Session = Depends(get_db),
    _=Depends(require_role("device", "responder")),
):
    # 1. Validate — Pydantic enforced 500 samples / fs 100 / units "g" already;
    #    this is the semantic unit assertion on the actual data.
    _assert_units_are_g(body.window)

    # 2. Idempotency: the ESP32 retries on timeout; one shake = one incident.
    existing = db.execute(
        select(Incident).where(Incident.event_id == body.event_id)).scalar_one_or_none()
    if existing is not None:
        return _ack(existing)

    # 3. Device upsert.
    device = _upsert_device(db, body.device_id, body.gps, body.device)

    # 4. Persist provisional incident + raw window BEFORE calling the ML API.
    incident = Incident(
        event_id=body.event_id,
        device_id=device.id,
        detected_at=body.detected_at,
        received_at=utcnow(),
        trigger_peak_g=body.trigger.peak_g,
        trigger_jerk_gs=body.trigger.jerk_gs,
        lat=body.gps.lat, lon=body.gps.lon, gps_valid=body.gps.valid,
        satellites=body.gps.satellites, speed_kmh=body.gps.speed_kmh,
        status="new",
        classification_pending=True,
    )
    db.add(incident)
    db.flush()
    db.add(IncidentWindow(
        incident_id=incident.id, fs_hz=body.window.fs_hz,
        ax=body.window.ax, ay=body.window.ay, az=body.window.az,
        gx=body.window.gx, gy=body.window.gy, gz=body.window.gz,
    ))
    db.commit()  # evidence persisted — a network failure can no longer lose it

    # 5-6. Classify (remote w/ retries, or local) and write back.
    try:
        result = await classify(body.window.model_dump())
        _apply_classification(incident, result)
        db.commit()
    except InferenceUnavailable as exc:
        log.warning("ML API unavailable for %s: %s — left classification_pending",
                    body.event_id, exc)

    # 7. Broadcast.
    await manager.broadcast("incident.created",
                            IncidentOut.model_validate(incident).model_dump())

    # 8. Flat ACK for the device.
    return _ack(incident)


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatIn,
    db: Session = Depends(get_db),
    _=Depends(require_role("device", "responder")),
):
    device = _upsert_device(db, body.device_id, body.gps, body.device)
    db.add(DeviceHeartbeat(
        device_id=device.id,
        lat=body.gps.lat if body.gps.valid else None,
        lon=body.gps.lon if body.gps.valid else None,
        satellites=body.gps.satellites,
        uptime_s=body.device.uptime_s,
        free_heap=body.device.free_heap,
        rssi=body.device.rssi,
        battery_v=body.battery_v,
    ))
    db.commit()
    await manager.broadcast("device_status", {
        "device_id": body.device_id,
        "status": "online",
        "last_seen_at": device.last_seen_at,
        "lat": device.last_lat, "lon": device.last_lon,
        "satellites": device.last_satellites,
        "uptime_s": body.device.uptime_s,
        "free_heap": body.device.free_heap,
        "rssi": body.device.rssi,
        "battery_v": body.battery_v,
    })
    return {"ok": True}
