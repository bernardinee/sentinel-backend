# sentinel-backend

The middle tier of an ML-based road accident detection and severity
classification system. It turns a crash detected on an ESP32 into something a
responder can see: it receives the raw IMU window, classifies it, stores it, and
pushes it live to the dashboard.

It is the only component that talks to the ML API, and it is the system of
record.

- **API reference** → [API_CONTRACT.md](API_CONTRACT.md)
- **Design and rationale** → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Deploying** → [DEPLOYMENT.md](DEPLOYMENT.md)

## Quick start

```bash
cp .env.example .env        # edit API_KEY and JWT_SECRET
docker compose up --build   # Postgres on 5433, backend on 8080
```

Without Docker, SQLite works and needs no server:

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:///./dev.db"
export INFERENCE_MODE=local
alembic upgrade head
uvicorn app.main:app --port 8080
```

Then, using a stored real capture:

```bash
python scripts/replay.py scripts/samples/4_Real_crash_4_g___90_ms.json \
  --lat 5.6581 --lon -0.1812
```

```
HTTP 200 in 2478 ms  (event_id=replay-…)
{ "severity_name": "Moderate", "accident_confirmed": true,
  "label_source": "model+signature", "peak_g": 4.404 }
```

## Responder accounts (dispatch console)

Responders sign in to the dashboard with an email and password; the console no
longer ships an API key to the browser. Accounts are provisioned out of band:

```bash
python scripts/create_responder.py --email ops@sentinel.gh --name "Control Room"
```

`/auth/register` mints **drivers only**, deliberately. If it could create
responders, anyone who reached the API could grant themselves dispatch control
of the whole fleet, so responder accounts are created against the database by an
operator instead. Use `--reset-password` to rotate one and `--deactivate` to
disable it — deactivation takes effect immediately, including for tokens already
issued.

## Driver authentication and live updates

Driver accounts register and sign in through `/api/v1/auth/*`. Passwords are
stored as Argon2 hashes. Access tokens expire after 15 minutes; opaque refresh
tokens last 30 days, rotate on every use, and are stored hashed in PostgreSQL.
Reusing a rotated token revokes the user's remaining sessions.

Each driver account is linked to one Sentinel device. Driver REST calls and
WebSocket events are restricted to that device, while the responder dashboard
continues to use a responder API key. Set a long, random `JWT_SECRET` in every
non-local deployment.

## Inference modes

| `INFERENCE_MODE` | Behaviour |
|---|---|
| `remote` | POSTs to `ML_API_URL/predict`, 10 s timeout, 2 retries with backoff |
| `local` | Loads `artifacts/phase2_xgboost_calibrated.joblib` and runs the **same vendored code** in-process — no network, identical decisions |

Local mode exists so a demonstration never depends on a hosted deployment. It is
a vendored copy of the ML API's inference path rather than a reimplementation,
because hand-duplicated feature extraction is how train/serve skew gets in.

## What the classifier does

```
p_crash = P(Moderate) + P(Severe)
signature = 2 g ≤ peak < 7 g  AND  40 ms ≤ longest excursion above 2 g ≤ 250 ms
```

Both must agree for a crash. If `p_crash` clears the threshold but the physics
signature does not hold, the label is forced to Normal and `label_source` is
`signature_override`. Severity above Moderate is graded by impulse (≥ 0.959 g·s),
**never by peak height** — a brief 12 g spike from a dropped device is Normal.

Do not add a "but 12 g must surely be severe" rule anywhere. Removing exactly
that rule is one of the project's findings.

## Bundled sample windows

`scripts/samples/` holds the Phase 2 validation vectors extracted from the ML
API's own Postman suite — real recorded windows, including the negative case
`2_Brief_12_g_spike__dropped_device_.json`, which must classify as
`signature_override`. They are development fixtures; the demonstration path uses
the live device.

## Dispatching units

Register the fleet once (operator configuration — real Greater Accra stations):

```bash
python scripts/register_fleet.py
```

Then `GET /api/v1/incidents/{id}/dispatch-options` ranks available units by
**road travel time** to the scene, flags the fastest of each required service,
and returns route geometry for the map. `POST …/assign-unit` sends one.

Routing uses the keyless public OSRM demo. If it is unreachable, ETAs fall back
to a haversine estimate that is explicitly marked `straight_line` — the UI dashes
those routes so a guess never reads as a road route.

## Tests

```bash
python -m pytest -q     # 39 tests
```

Covering ingest validation (sample count, `fs_hz`, units, and the m/s² unit
assertion), `event_id` idempotency, ML client retry and unavailability, the
dispatch state machine, panic exclusion from statistics, and the unit roster —
including that ranking follows road time rather than straight-line distance, and
that routing degrades safely when OSRM is down. Two run the real model against
real windows and assert the crash/override outcomes.

Tests use SQLite and need no Docker.

## Layout

```
app/
  main.py                 FastAPI app, WS endpoint, background retry + prune
  config.py  db.py  models.py  schemas.py  auth.py
  modules/
    ingest.py             validate → idempotency → persist → classify → broadcast
    accounts.py         driver registration, token rotation + logout
    incidents.py devices.py dispatch.py stats.py sentinel.py ws.py
    units.py              fleet roster, ETA ranking, assignment
    routing.py            OSRM road routes, cache, marked fallback
    inference/
      service.py          remote/local dispatcher, retries
      phase2_vendored.py  verbatim port of the ML API inference path
alembic/                  migrations
artifacts/                model, feature names, calibrated thresholds
scripts/replay.py         re-inject a stored capture through the full path
scripts/register_fleet.py load the Accra responder roster
scripts/create_responder.py provision a dispatch-console account
```
