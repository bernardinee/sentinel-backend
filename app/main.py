"""Sentinel backend — modular monolith (§11): one deployable, clear module
boundaries (ingest / inference / incidents / devices / dispatch / stats /
sentinel / ws) that can be drawn as a service diagram."""
import asyncio
import contextlib
import logging
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, select

from app.auth import (principal_from_access_token, principal_from_api_key,
                      require_role)
from app.config import get_settings
from app.db import SessionLocal
from app.models import DeviceHeartbeat, Incident, IncidentWindow, RefreshToken, utcnow
from app.modules import (accounts, devices, dispatch, incidents, ingest,
                         sentinel, stats, sms, units)
from app.modules.inference.service import (InferenceUnavailable, classify,
                                           ml_health)
from app.modules.ingest import _apply_classification
from app.modules.movement import advance_units_once
from app.modules.ws import manager
from app.schemas import IncidentOut

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sentinel")

PENDING_RETRY_INTERVAL_S = 60
PRUNE_INTERVAL_S = 3600
# How often responders' projected status is advanced. The dashboard animates
# their pins every frame between these ticks, so this only needs to be tight
# enough that a status flip (en route / on scene) feels prompt.
UNIT_ADVANCE_INTERVAL_S = 3


async def _retry_pending_classifications() -> None:
    """§8.7 — background retry for incidents whose ML call failed at ingest."""
    while True:
        await asyncio.sleep(PENDING_RETRY_INTERVAL_S)
        try:
            with SessionLocal() as db:
                pending = db.execute(
                    select(Incident).where(Incident.classification_pending.is_(True))
                    .order_by(Incident.received_at.asc()).limit(10)
                ).scalars().all()
                for incident in pending:
                    window = db.get(IncidentWindow, incident.id)
                    if window is None:
                        continue
                    try:
                        result = await classify({
                            "ax": window.ax, "ay": window.ay, "az": window.az,
                            "gx": window.gx, "gy": window.gy, "gz": window.gz})
                    except InferenceUnavailable:
                        break  # ML still down; try again next cycle
                    _apply_classification(incident, result)
                    db.commit()
                    await manager.broadcast(
                        "incident.updated",
                        IncidentOut.model_validate(incident).model_dump())
                    log.info("Backfilled classification for %s", incident.event_id)
        except Exception:
            log.exception("pending-classification retry loop error")


async def _advance_responders() -> None:
    """Progress dispatched units through en_route -> on_scene on the response
    clock, so status updates without the dispatcher clicking through each step."""
    while True:
        await asyncio.sleep(UNIT_ADVANCE_INTERVAL_S)
        try:
            with SessionLocal() as db:
                await advance_units_once(db)
        except Exception:
            log.exception("responder advance loop error")


async def _prune_heartbeats() -> None:
    """§4 — rolling heartbeat retention."""
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        try:
            cutoff = utcnow() - timedelta(days=get_settings().HEARTBEAT_RETENTION_DAYS)
            with SessionLocal() as db:
                db.execute(delete(DeviceHeartbeat).where(DeviceHeartbeat.at < cutoff))
                db.execute(
                    delete(RefreshToken).where(RefreshToken.expires_at < utcnow()))
                db.commit()
        except Exception:
            log.exception("heartbeat prune loop error")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_retry_pending_classifications()),
             asyncio.create_task(_advance_responders()),
             asyncio.create_task(_prune_heartbeats())]
    log.info("Sentinel backend up — inference mode: %s", get_settings().INFERENCE_MODE)
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(title="Sentinel Backend", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = "/api/v1"
app.include_router(ingest.router, prefix=api)
app.include_router(accounts.router, prefix=api)
app.include_router(incidents.router, prefix=api)
app.include_router(dispatch.router, prefix=api)
app.include_router(devices.router, prefix=api)
app.include_router(stats.router, prefix=api)
app.include_router(sentinel.router, prefix=api)
app.include_router(units.router, prefix=api)
app.include_router(sms.router, prefix=api)


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "sentinel-backend",
            "inference_mode": get_settings().INFERENCE_MODE}


@app.get(f"{api}/ml/health")
async def ml_api_health(_=Depends(require_role())):
    return await ml_health()


@app.websocket("/ws/incidents")
async def ws_incidents(ws: WebSocket):
    # Mobile clients use a short-lived access token in the WebSocket protocol
    # header, keeping credentials out of URLs and access logs. Header/query
    # fallbacks remain for native clients and the existing dashboard.
    authorization = ws.headers.get("authorization", "")
    access_token = ws.query_params.get("access_token")
    offered_protocols = [
        value.strip()
        for value in ws.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    for protocol in offered_protocols:
        if protocol.startswith("bearer."):
            access_token = protocol[len("bearer."):]
            break
    if authorization.lower().startswith("bearer "):
        access_token = authorization[7:].strip()
    try:
        with SessionLocal() as db:
            if access_token:
                principal = principal_from_access_token(access_token, db)
            else:
                key = ws.query_params.get("api_key")
                if not key:
                    raise HTTPException(status_code=401)
                principal = principal_from_api_key(key)
    except HTTPException:
        await ws.close(code=4401)
        return
    subprotocol = "sentinel-v1" if "sentinel-v1" in offered_protocols else None
    await manager.connect(ws, principal, subprotocol=subprotocol)
    ping_task = asyncio.create_task(manager.keepalive(ws))
    try:
        while True:
            await ws.receive_text()  # drain client frames (pongs etc.)
    except WebSocketDisconnect:
        pass
    finally:
        ping_task.cancel()
        await manager.disconnect(ws)
