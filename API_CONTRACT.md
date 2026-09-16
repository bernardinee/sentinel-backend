# Sentinel API Contract

Version 1.0 · all endpoints are under `/api/v1` · written to Chapter 3 appendix standard.

Device and responder integrations authenticate with a scoped shared secret.
Driver endpoints authenticate with short-lived Bearer access tokens issued by
the account endpoints.

```
X-API-Key: <shared secret>
# or, for a driver:
Authorization: Bearer <access token>
Content-Type: application/json
```

Responses are Pydantic-validated. Errors use FastAPI's shape:
`{"detail": "<message>"}` — except validation errors, which return the standard
422 body listing the offending field.

| Code | Meaning |
|---|---|
| 200 | OK (also returned for an idempotent replay of an existing `event_id`) |
| 201 | Created (`/panic`, contact creation, unit registration) |
| 204 | Deleted, no body |
| 401 | Missing, invalid, expired, or revoked authentication |
| 403 | Valid key, insufficient role |
| 404 | Unknown incident / device / contact / unit |
| 409 | Illegal dispatch-state transition, duplicate call sign, unit already committed, or routing requested for an incident with no GPS fix |
| 422 | Payload failed validation (sample count, `fs_hz`, units, unit-scale assertion) |
| 502 | — *not used*: ML failures never fail ingest, see §Graceful degradation |

---

## 1. Ingest (device → backend)

### `POST /api/v1/events`

The main path. One call per detected crash.

**Request**

```json
{
  "device_id": "ESP32_ACC_001",
  "event_id": "ESP32_ACC_001-0007-00412337",
  "detected_at": "2026-09-14T10:22:41Z",
  "trigger": { "peak_g": 3.41, "jerk_gs": 159.27 },
  "gps": {
    "lat": 5.6581, "lon": -0.1812, "valid": true,
    "satellites": 7, "speed_kmh": 42.3, "hdop": 1.2
  },
  "device": {
    "uptime_s": 41233, "free_heap": 142000,
    "rssi": -61, "firmware_version": "2.0.0"
  },
  "window": {
    "fs_hz": 100, "units_accel": "g", "units_gyro": "deg_s",
    "ax": [500 floats], "ay": [500 floats], "az": [500 floats],
    "gx": [500 floats], "gy": [500 floats], "gz": [500 floats]
  }
}
```

`detected_at`, `gps`, and `device` are optional; everything else is required.

**Validation — all failures are loud, none are silently repaired**

| Rule | Failure |
|---|---|
| each of `ax`…`gz` is exactly 500 samples | 422, names the field and the count received |
| `fs_hz == 100` | 422 |
| `units_accel == "g"` | 422 |
| median resultant magnitude ≤ 5 | 422, names the likely m/s² mistake |

The last rule is the unit assertion. Gravity is ≈1.0 g but ≈9.81 m/s², so a
median resultant above 5 means the sender is in m/s². The backend rejects rather
than rescaling, because a silent rescale is how a whole training class was
invalidated once before.

**Handler sequence** (order is load-bearing)

1. Validate.
2. Idempotency check on `event_id` — if it exists, return the stored incident.
   Inference does **not** re-run.
3. Upsert the device, touch `last_seen_at`.
4. Persist the incident **and the raw 500×6 window** — before any ML call.
5. Call the ML API (10 s timeout, 2 retries with backoff) or run inference
   in-process when `INFERENCE_MODE=local`.
6. Write the classification back onto the incident.
7. Broadcast `incident.created` over the WebSocket.
8. Return the flat ACK.

**Response — deliberately small and flat (one level, no nesting)**

```json
{
  "event_id": "ESP32_ACC_001-0007-00412337",
  "severity_class": 1,
  "severity_name": "Moderate",
  "confidence": 0.772,
  "p_crash": 0.8105,
  "accident_confirmed": true,
  "label_source": "model+signature",
  "peak_g": 6.501,
  "classification_pending": false
}
```

Measured at 246 bytes for a real crash — the ESP32 parses it into a 512-byte
document on a constrained heap, so the shape must not grow or nest.

When the ML API is unreachable, the incident is still stored and broadcast, and
the ACK carries `classification_pending: true` with `severity_class: null`. The
device then falls back to its local threshold decision.

### `POST /api/v1/heartbeat`

Sent every 30 s. Updates the device row and appends to `device_heartbeats`;
broadcasts a `device_status` frame.

```json
{
  "device_id": "ESP32_ACC_001",
  "gps": { "lat": 5.6581, "lon": -0.1812, "valid": true, "satellites": 7 },
  "device": { "uptime_s": 41233, "free_heap": 142000, "rssi": -61 },
  "battery_v": 3.9
}
```

Response: `{"ok": true}`. A device is considered **offline** after 90 s without a
heartbeat (three missed beats).

---

## 2. Query

### `GET /api/v1/incidents`

Filters: `status`, `severity_class`, `device_id`, `accident_confirmed`, `from`,
`to`, `page`, `page_size` (default 50, max 500). Sorted `received_at` desc.

```json
{ "items": [ IncidentOut, … ], "total": 42, "page": 1, "page_size": 50 }
```

`from` is also how the dashboard backfills after a WebSocket reconnect.

**`IncidentOut`**

| Field | Type | Notes |
|---|---|---|
| `id`, `event_id`, `device_id` | string | `device_id` is the internal UUID |
| `detected_at`, `received_at` | datetime | device clock / server clock |
| `severity_class` | 0\|1\|2\|null | null while pending |
| `severity_name` | string\|null | Normal / Moderate / Severe |
| `confidence`, `p_crash` | float\|null | |
| `model_severity` | string\|null | what the model alone said |
| `accident_confirmed` | bool\|null | |
| `probabilities` | object\|null | `{Normal, Moderate, Severe}` |
| `peak_g`, `excursion_ms`, `impulse_gs` | float\|null | measured on the 20 Hz low-passed window |
| `signature_match` | bool\|null | physics gate verdict |
| `label_source` | string\|null | see below |
| `unit_scale_applied` | float\|null | 1.0 = already in g |
| `inference_time_ms` | float\|null | |
| `classification_pending` | bool | true = ML retry queued |
| `trigger_peak_g`, `trigger_jerk_gs` | float\|null | what the device measured |
| `lat`, `lon`, `gps_valid`, `satellites`, `speed_kmh` | | |
| `status` | enum | new / acknowledged / dispatched / resolved / false_alarm |
| `acknowledged_at`, `acknowledged_by`, `resolved_at`, `notes` | | |

**`label_source` values** — this field must reach the dashboard:

| Value | Meaning |
|---|---|
| `model+signature` | P(crash) cleared the alert threshold **and** the physics signature agreed. A real detection. |
| `signature_override` | P(crash) cleared the threshold but the physics gate rejected it (peak outside 2–7 g, or transient outside 40–250 ms) and forced Normal. |
| `model` | P(crash) stayed below the threshold. No crash. |
| `manual_panic` | Human pressed the button. Model bypassed entirely; excluded from all statistics. |

Note `signature_override` does **not** imply the model's argmax was a crash
class — the gate fires on P(crash) crossing the threshold, which can happen
while the top class is still Normal.

### Other query endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/v1/incidents/active` | Everything not resolved/false_alarm, severity desc then recency. The responder triage queue. |
| `GET /api/v1/incidents/{id}` | `IncidentOut` + `dispatch_events[]` |
| `GET /api/v1/incidents/{id}/window` | `{incident_id, fs_hz, ax…gz}` — the stored 500×6 evidence. Separate endpoint so lists stay light. |
| `GET /api/v1/devices` | `DeviceOut[]`, status computed live from heartbeat age |
| `GET /api/v1/devices/{device_id}` | one `DeviceOut` |
| `GET /api/v1/devices/{device_id}/heartbeats?hours=24` | `HeartbeatOut[]` ascending |
| `GET /api/v1/stats/summary` | see below |
| `GET /api/v1/ml/health` | proxy of the ML API `/health` (or local model metadata) |

**`GET /api/v1/stats/summary`**

```json
{
  "total_incidents": 6,
  "by_severity": {"Normal": 2, "Moderate": 4, "Severe": 0, "pending": 0},
  "by_label_source": {"model+signature": 5, "signature_override": 1},
  "by_status": {"new": 5, "acknowledged": 1},
  "mean_inference_ms": 17.11,
  "mean_end_to_end_s": 2.45,
  "last_24h": 6, "last_7d": 6, "last_30d": 6
}
```

`manual_panic` incidents are excluded from every figure here. `mean_end_to_end_s`
is `received_at − detected_at`, discarding clock-skew outliers outside 0–3600 s.

---

## 3. Actions

All three write a `dispatch_events` row and broadcast `incident.updated`. The
mission timeline is derived from that table, never from incident columns alone.

| Endpoint | Body | Legal from |
|---|---|---|
| `POST /api/v1/incidents/{id}/acknowledge` | `{actor, note?}` | `new` |
| `POST /api/v1/incidents/{id}/dispatch` | `{actor, action: assign\|en_route\|on_scene, note?}` | `acknowledged`, `dispatched` (`on_scene` needs `dispatched`) |
| `POST /api/v1/incidents/{id}/resolve` | `{actor, outcome: resolved\|false_alarm, note?}` | `new`, `acknowledged`, `dispatched` |

An illegal transition returns **409** with the current status named. Acting on a
closed incident is always 409.

---

## 3b. Response units and dispatch routing

The dispatcher's real question is not "which unit is nearest" but "which unit
gets there soonest". Those differ often enough in Accra — ring roads, one-ways,
the Korle lagoon — that recommendations are ranked by **road travel time**, never
by straight-line distance.

Routing uses the public OSRM demo server: free and keyless, the same constraint
that drives the OSM basemap. Candidates are coarse-filtered by haversine (three
nearest per service type) and only the shortlist is routed, so a large roster
does not fan out into dozens of calls.

### Roster

| Endpoint | Body / notes |
|---|---|
| `GET /api/v1/units` | `UnitOut[]` |
| `POST /api/v1/units` | `{call_sign, unit_type, station_name, home_lat, home_lon, crew_size?, contact_phone?}` → 201. 409 if the call sign exists. |
| `PATCH /api/v1/units/{call_sign}` | Any of `station_name, status, current_lat, current_lon, crew_size, contact_phone, active` |
| `DELETE /api/v1/units/{call_sign}` | 204. **409** while the unit is dispatched/en route/on scene. |

`unit_type` ∈ `AMBULANCE | FIRE | POLICE | RESCUE`.
`status` ∈ `available | dispatched | en_route | on_scene | out_of_service`.

A unit reports `current_lat`/`current_lon` when it has live positions;
recommendations use those and fall back to the home station.

The roster is **operator configuration**, like emergency contacts — entered by a
human, never fabricated. `scripts/register_fleet.py` loads a starting roster of
real Greater Accra facilities (Korle Bu, 37 Military, Ridge, GNFS and Police
stations, NADMO) at their real coordinates.

### `GET /api/v1/incidents/{id}/dispatch-options`

```json
{
  "incident_id": "…", "incident_lat": 5.6581, "incident_lon": -0.1812,
  "required_types": ["AMBULANCE", "POLICE"],
  "routing_source": "osrm",
  "note": null,
  "options": [
    {
      "unit": { …UnitOut… },
      "route": { "distance_km": 1.9, "duration_min": 5.0,
                 "geometry": [[lon,lat], …], "source": "osrm" },
      "eta_min": 5.0,
      "recommended": true
    }
  ]
}
```

Sorted by `eta_min` ascending. `recommended` marks the fastest available unit of
each required type. `required_types` is advisory, derived from severity —
Severe → ambulance + fire + police, Moderate → ambulance + police, Normal → none
— and the dispatcher may send anything regardless.

`route.geometry` is GeoJSON `[lon, lat]` for drawing the path on the map.

**409** if the incident has no GPS fix: units cannot be routed to an unknown
location, and saying so is better than routing to a default.

When OSRM is unreachable, `source` is `straight_line` (haversine × 1.35 urban
detour factor at 32 km/h) and `routing_source` becomes `straight_line` or
`mixed`, with `note` explaining that those ETAs are estimates. The UI dashes
estimated routes so a guess never looks like a road route.

### `POST /api/v1/incidents/{id}/assign-unit`

`{call_sign, actor, note?}` → `IncidentOut`.

Sets the unit to `dispatched`, links it to the incident, moves the incident to
`dispatched` (acknowledging it first if it was still `new`), writes an `assign`
dispatch event with the measured road ETA in the note, and broadcasts both
`incident.updated` and `unit.updated`.

**409** if the incident is closed, if the unit is already committed to a
different incident, or if it is inactive.

### `POST /api/v1/units/{call_sign}/status`

`{status, actor, note?}` → `UnitOut`. Progresses a unit through the response.
`en_route` and `on_scene` also write onto the assigned incident's timeline, so
the dispatcher reads one story rather than two. `available` and
`out_of_service` release the incident link.

## 4. Sentinel app hooks

The mobile app consumes these endpoints with a driver Bearer token.

### Driver accounts

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/register` | Create a driver linked to one `device_id` (shared devices are allowed); returns an access/refresh token pair |
| `POST /api/v1/auth/login` | Verify email/password and create a new token pair |
| `POST /api/v1/auth/refresh` | Rotate the opaque refresh token and issue a new access token |
| `POST /api/v1/auth/logout` | Revoke the supplied refresh token; returns 204 |
| `GET /api/v1/auth/me` | Return the authenticated driver profile and linked device |

### Driver role

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/me/protection-status?device_id=` | Drives the app's green/amber/red status ring |
| `POST /api/v1/panic` | `{device_id, lat?, lon?, note?}` → creates a `severity_class: 2`, `label_source: "manual_panic"`, `accident_confirmed: true` incident. **Bypasses the model by design** and is excluded from statistics. Returns 201 + `IncidentOut`. |
| `GET /api/v1/me/incidents?device_id=` | Linked device history, newest first, capped at 200; each incident also includes `assigned_at`, `en_route_at`, `arrived_at`, and current `responding_units` call signs |
| `GET\|POST\|PATCH\|DELETE /api/v1/devices/{device_id}/contacts[/{id}]` | Emergency contact CRUD |

**`protection-status`**

```json
{
  "device_id": "ESP32_ACC_001",
  "registered": true, "online": true, "monitoring_active": true,
  "gps_locked": true, "satellites": 7,
  "last_heartbeat_at": "2026-09-14T11:32:18Z",
  "last_heartbeat_age_s": 12.4,
  "open_incidents": 1
}
```

An unregistered `device_id` returns `registered: false` with everything else
false/null rather than 404 — the app shows "not set up", not an error.

The emergency-contact table is for the app. The ESP32 keeps its own hardcoded
SMS list, deliberately, so the fail-safe works with no backend at all.

### Responder role

Reuses §2 and §3, plus `GET /api/v1/incidents/active`.

---

## 5. WebSocket

```
WS /ws/incidents?api_key=<key>
```

The responder dashboard now connects with its short-lived Bearer token in the
`access_token` query parameter; the older responder API-key URL remains supported
for service integrations. Driver clients connect to `/ws/incidents` with
protocols `sentinel-v1` and `bearer.<access-token>`, which keeps the token out of
the URL. Native clients may instead use an Authorization header. An invalid
credential closes with code **4401**. Driver connections only receive incident,
device, and contact-change events belonging to their linked device.

**Envelope**

```json
{ "type": "incident.created" | "incident.updated" | "device_status" | "unit.updated" | "contact.updated" | "ping",
  "at": "2026-09-14T11:32:18.463Z",
  "data": { … } }
```

- `incident.created` / `incident.updated` → `data` is a full `IncidentOut`.
- `device_status` → `{device_id, status, last_seen_at, lat, lon, satellites, uptime_s, free_heap, rssi, battery_v}`.
- `unit.updated` → `data` is a full `UnitOut`, emitted on assignment and on every status change.
- `ping` → server keepalive every 20 s; the client replies `{"type":"pong"}`.

`contact.updated` carries only `{device_id}` (the internal device UUID) after
contact creation, update, or deletion. Clients refetch the device's contact list.
The mobile app refetches its driver incident projection after each incident
WebSocket event to populate responder milestones that are absent from the shared
dispatcher `IncidentOut` frame.

**Client obligations**

1. Reconnect with exponential backoff (1, 2, 4, 8, 15, 30 s).
2. On every successful **re**connect, drivers call `GET /api/v1/me/incidents`
   and responders call `GET /api/v1/incidents?from=<last received_at seen>`, then
   merge the result.

A dashboard that silently misses an incident because a socket dropped is worse
than no dashboard, so the backfill is not optional.

---

## 6. Graceful degradation

If the ML API is unreachable at ingest:

- the incident and its raw window are **already persisted** (step 4 precedes the
  ML call), so nothing is lost;
- `classification_pending` is set and the incident is broadcast anyway, so it
  appears on the dashboard immediately;
- a background task retries every 60 s, in batches of 10 oldest-first, and
  broadcasts `incident.updated` when a backfill lands;
- the device receives a pending ACK and falls back to its local decision.

`INFERENCE_MODE=local` removes the dependency altogether by loading
`artifacts/phase2_xgboost_calibrated.joblib` and running the **same vendored
inference code** in-process — identical 20 Hz Butterworth low-pass, identical 25
features, identical signature gate. It is a vendored copy rather than a
reimplementation precisely to avoid train/serve skew.

## SMS relay — `POST /api/v1/sms/send`

Lets the device send emergency SMS over WiFi when its GSM modem is unavailable.
Roles: `device`, `responder`.

Request:
```json
{"phone": "+233241234567", "message": "Accident detected at 5.6581,-0.1812", "device_id": "ESP32_ACC_001"}
```
`phone` accepts `+233…`, `233…`, `00233…` or Ghana-local `0…`; it is normalised to `+233…`.
`message` is 1–918 characters. `event_id` is optional and only logged.

Response `200`:
```json
{"status": "sent", "provider": "arkesel", "phone": "+233241234567", "provider_message_id": "…", "segments": 1}
```

| Status | Meaning |
|---|---|
| 401 | missing / invalid key |
| 403 | recipient is neither an active emergency contact of `device_id` nor in `SMS_ALLOWED_RECIPIENTS` |
| 422 | invalid phone or message |
| 429 | more than `SMS_RATE_LIMIT_PER_10MIN` messages from this key+device in 10 minutes |
| 502 | provider rejected the message or was unreachable (detail carries the provider's reason) |
| 503 | `SMS_PROVIDER` not configured |

The recipient restriction exists because the device key lives in firmware flash:
without it, anyone who reads the key could send SMS at the project's expense.
