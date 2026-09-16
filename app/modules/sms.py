"""SMS relay: the device sends {phone, message} over WiFi, the backend sends the SMS.

Replaces the SIM800L path when the GSM module is unavailable:
    ESP32 --WiFi--> POST /api/v1/sms/send --> SMS provider --> phone

Provider is chosen by SMS_PROVIDER:
    arkesel  Ghana SMS gateway (sms.arkesel.com v2). Needs ARKESEL_API_KEY, SMS_SENDER_ID.
    twilio   Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.
    console  Logs the message and reports success. For local testing only.
    (unset)  Endpoint returns 503 "SMS provider not configured".

The device API key is stored in firmware flash and can be read back over USB,
so this endpoint must not become a free SMS gateway: recipients are limited to
the device's active emergency contacts plus SMS_ALLOWED_RECIPIENTS, and each
device (or key) is rate-limited.
"""
import logging
import re
import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, require_role
from app.config import get_settings
from app.db import get_db
from app.models import Device, EmergencyContact

log = logging.getLogger(__name__)
router = APIRouter(tags=["sms"])

ARKESEL_URL = "https://sms.arkesel.com/api/v2/sms/send"
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class SmsIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20, description="International format, e.g. +233241234567")
    message: str = Field(min_length=1, max_length=918, description="Up to 6 SMS segments")
    device_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=96)


class SmsOut(BaseModel):
    status: str
    provider: str
    phone: str
    provider_message_id: str | None = None
    segments: int


class SmsProviderError(Exception):
    pass


def normalize_phone(raw: str) -> str:
    """Return +<country><number>. Accepts +233..., 233..., 00233... and Ghana-local 0XXXXXXXXX."""
    digits = re.sub(r"[\s\-()]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"0\d{9}", digits):          # Ghana local format
        digits = "+233" + digits[1:]
    if not digits.startswith("+"):
        digits = "+" + digits
    if not re.fullmatch(r"\+\d{8,15}", digits):
        raise HTTPException(status_code=422, detail=f"Invalid phone number '{raw}'")
    return digits


def segments(message: str) -> int:
    n = len(message)
    return 1 if n <= 160 else -(-n // 153)


# ── rate limiting (per process; Railway runs one instance) ──────────────────
_sent: dict[str, deque] = defaultdict(deque)


def _check_rate(key: str, limit: int, window_s: int = 600) -> None:
    now = time.monotonic()
    q = _sent[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail=f"SMS rate limit: {limit} per {window_s // 60} min")
    q.append(now)


def _allowed_recipients(db: Session, device_id: str | None) -> set[str]:
    allowed = {normalize_phone(p) for p in get_settings().sms_allowed_list()}
    if device_id:
        device = db.execute(select(Device).where(Device.device_id == device_id)).scalar_one_or_none()
        if device is not None:
            rows = db.execute(select(EmergencyContact.phone).where(
                EmergencyContact.device_id == device.id, EmergencyContact.active.is_(True))).scalars()
            for p in rows:
                try:
                    allowed.add(normalize_phone(p))
                except HTTPException:
                    log.warning("Skipping malformed emergency contact number %r", p)
    return allowed


# ── providers ────────────────────────────────────────────────────────────────
async def _send_arkesel(phone: str, message: str) -> str | None:
    s = get_settings()
    if not s.ARKESEL_API_KEY:
        raise SmsProviderError("ARKESEL_API_KEY is not set")
    body = {"sender": s.SMS_SENDER_ID, "message": message, "recipients": [phone.lstrip("+")]}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(ARKESEL_URL, json=body,
                                 headers={"api-key": s.ARKESEL_API_KEY, "Accept": "application/json"})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or data.get("status") != "success":
        raise SmsProviderError(f"Arkesel {resp.status_code}: {data.get('message') or resp.text[:200]}")
    items = data.get("data") or []
    return str(items[0].get("id")) if items and isinstance(items[0], dict) and items[0].get("id") else None


async def _send_twilio(phone: str, message: str) -> str | None:
    s = get_settings()
    if not (s.TWILIO_ACCOUNT_SID and s.TWILIO_AUTH_TOKEN and s.TWILIO_FROM_NUMBER):
        raise SmsProviderError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER must be set")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(TWILIO_URL.format(sid=s.TWILIO_ACCOUNT_SID),
                                 data={"From": s.TWILIO_FROM_NUMBER, "To": phone, "Body": message},
                                 auth=(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN))
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code not in (200, 201):
        raise SmsProviderError(f"Twilio {resp.status_code}: {data.get('message') or resp.text[:200]}")
    return data.get("sid")


async def _send_console(phone: str, message: str) -> str | None:
    log.warning("SMS_PROVIDER=console: NOT sent to %s:\n%s", phone, message)
    return "console"


PROVIDERS = {"arkesel": _send_arkesel, "twilio": _send_twilio, "console": _send_console}


async def send_sms(phone: str, message: str) -> tuple[str, str | None]:
    """Send through the configured provider. Returns (provider, provider_message_id)."""
    name = get_settings().SMS_PROVIDER.strip().lower()
    if name not in PROVIDERS:
        raise HTTPException(status_code=503, detail="SMS provider not configured (set SMS_PROVIDER)")
    try:
        return name, await PROVIDERS[name](phone, message)
    except SmsProviderError as exc:
        log.error("SMS to %s failed: %s", phone, exc)
        raise HTTPException(status_code=502, detail=f"SMS provider error: {exc}") from exc
    except httpx.HTTPError as exc:
        log.error("SMS to %s failed: %s", phone, exc)
        raise HTTPException(status_code=502, detail=f"SMS provider unreachable: {exc}") from exc


@router.post("/sms/send", response_model=SmsOut)
async def send(body: SmsIn, db: Session = Depends(get_db),
               principal: Principal = Depends(require_role("device", "responder"))):
    phone = normalize_phone(body.phone)
    device_id = body.device_id or principal.device_id
    if phone not in _allowed_recipients(db, device_id):
        raise HTTPException(
            status_code=403,
            detail=(f"{phone} is not an emergency contact of device '{device_id}' and not in "
                    f"SMS_ALLOWED_RECIPIENTS"))
    _check_rate(f"{principal.subject}:{device_id}", get_settings().SMS_RATE_LIMIT_PER_10MIN)
    provider, msg_id = await send_sms(phone, body.message)
    log.info("SMS sent via %s to %s (device=%s event=%s id=%s)", provider, phone, device_id,
             body.event_id, msg_id)
    return SmsOut(status="sent", provider=provider, phone=phone,
                  provider_message_id=msg_id, segments=segments(body.message))
