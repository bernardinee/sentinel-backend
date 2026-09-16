"""SMS relay: device -> POST /api/v1/sms/send -> provider."""
import httpx
import pytest

from app.modules import sms
from tests.conftest import HEADERS

DEVICE = {"X-API-Key": "device-key"}


@pytest.fixture(autouse=True)
def reset_rate_limit():
    sms._sent.clear()
    yield
    sms._sent.clear()


@pytest.fixture
def device_with_contact(client):
    hb = {"device_id": "ESP32_TEST", "gps": {"lat": 5.65, "lon": -0.18, "valid": True, "satellites": 6},
          "device": {"uptime_s": 1, "free_heap": 1, "rssi": -50}}
    assert client.post("/api/v1/heartbeat", json=hb, headers=HEADERS).status_code == 200
    r = client.post("/api/v1/devices/ESP32_TEST/contacts", headers=HEADERS,
                    json={"name": "Mum", "phone": "+233 24 123 4567", "priority": 1, "active": True})
    assert r.status_code == 201, r.text
    return client


def body(**kw):
    return {"phone": "+233241234567", "message": "Accident detected at 5.65,-0.18",
            "device_id": "ESP32_TEST", **kw}


@pytest.mark.parametrize("raw,expected", [
    ("+233241234567", "+233241234567"), ("233241234567", "+233241234567"),
    ("0241234567", "+233241234567"), ("00233 24 123 4567", "+233241234567"),
])
def test_normalize_phone(raw, expected):
    assert sms.normalize_phone(raw) == expected


def test_not_configured_returns_503(device_with_contact, settings):
    settings.SMS_PROVIDER = ""
    r = device_with_contact.post("/api/v1/sms/send", json=body(), headers=DEVICE)
    assert r.status_code == 503


def test_unknown_recipient_rejected(device_with_contact, settings):
    settings.SMS_PROVIDER = "console"
    r = device_with_contact.post("/api/v1/sms/send", json=body(phone="+233500000000"), headers=DEVICE)
    assert r.status_code == 403


def test_allowlist_env_permits_recipient(device_with_contact, settings):
    settings.SMS_PROVIDER = "console"
    settings.SMS_ALLOWED_RECIPIENTS = "0500000000"
    r = device_with_contact.post("/api/v1/sms/send", json=body(phone="+233500000000"), headers=DEVICE)
    assert r.status_code == 200 and r.json()["status"] == "sent"


def test_requires_auth(device_with_contact):
    assert device_with_contact.post("/api/v1/sms/send", json=body()).status_code == 401


def test_arkesel_request_shape_and_success(device_with_contact, settings, monkeypatch):
    settings.SMS_PROVIDER = "arkesel"
    settings.ARKESEL_API_KEY = "k"
    settings.SMS_SENDER_ID = "Sentinel"
    seen = {}

    async def fake_post(self, url, json=None, headers=None, **kw):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"status": "success",
                                         "data": [{"recipient": "233241234567", "id": "abc-1"}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = device_with_contact.post("/api/v1/sms/send", json=body(phone="0241234567"), headers=DEVICE)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "sent", "provider": "arkesel", "phone": "+233241234567",
                        "provider_message_id": "abc-1", "segments": 1}
    assert seen["url"] == sms.ARKESEL_URL and seen["headers"]["api-key"] == "k"
    assert seen["json"] == {"sender": "Sentinel", "message": "Accident detected at 5.65,-0.18",
                            "recipients": ["233241234567"]}


def test_provider_failure_is_502(device_with_contact, settings, monkeypatch):
    settings.SMS_PROVIDER = "arkesel"
    settings.ARKESEL_API_KEY = "bad"

    async def fake_post(self, url, **kw):
        return httpx.Response(401, json={"message": "Invalid key", "status": "error"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = device_with_contact.post("/api/v1/sms/send", json=body(), headers=DEVICE)
    assert r.status_code == 502 and "Invalid key" in r.json()["detail"]


def test_twilio_request_shape(device_with_contact, settings, monkeypatch):
    settings.SMS_PROVIDER = "twilio"
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_FROM_NUMBER = "AC1", "t", "+15550001"
    seen = {}

    async def fake_post(self, url, data=None, auth=None, **kw):
        seen.update(url=url, data=data, auth=auth)
        return httpx.Response(201, json={"sid": "SM1"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = device_with_contact.post("/api/v1/sms/send", json=body(), headers=DEVICE)
    assert r.status_code == 200 and r.json()["provider_message_id"] == "SM1"
    assert seen["data"] == {"From": "+15550001", "To": "+233241234567", "Body": "Accident detected at 5.65,-0.18"}


def test_rate_limit(device_with_contact, settings):
    settings.SMS_PROVIDER = "console"
    settings.SMS_RATE_LIMIT_PER_10MIN = 2
    codes = [device_with_contact.post("/api/v1/sms/send", json=body(), headers=DEVICE).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
