# Deployment

Three deployables: the ML API (already on Railway), the backend + PostgreSQL
(Railway), and the dashboard (Vercel). Everything also runs locally with
`docker compose up`, so the defence never depends on the internet.

---

## 0. The Railway ML API build failure — status and diagnosis

**The brief states the last Railway build failed at the image-build step, so the
live URL is still serving a month-old pre-correction model. That is no longer
true. It was verified fixed at the start of this work and re-verified at the
end.**

```bash
curl https://accident-severity-api-production.up.railway.app/health
```

```json
{"status":"healthy","model":"phase2_xgboost_calibrated","n_features":25,
 "taxonomy":"signature+impulse (v2)","crash_alert_threshold":0.396,
 "classes":["Normal","Moderate","Severe"]}
```

`model` and `taxonomy` are the corrected Phase 2/3 values, so the live endpoint
is the right model. A live prediction was also checked end-to-end: the real
4 g / 90 ms validation vector returns `model+signature`, and the brief 12 g spike
returns `signature_override`. The deployed build behaves correctly.

The most likely cause of the failure described in the brief, and what to check if
it recurs — from inspecting `cloud_api/Dockerfile`, `requirements.txt` and
`railway.toml`:

1. **Memory ceiling during `pip install`.** `requirements.txt` pins
   `numpy==2.2.6`, `scipy==1.16.0`, `xgboost==3.2.0`, `scikit-learn==1.8.0`.
   If any lacks a manylinux wheel for the builder's Python/arch, pip falls back
   to building from source, and SciPy in particular exhausts the build
   container's memory and is OOM-killed. The image builds, then the *build* step
   dies with no application error. Fix: keep the Python base at 3.11 (wheels
   exist for all four), never let the base drift to a version ahead of the
   wheels.
2. **`EXPOSE $PORT` is evaluated at build time**, when `$PORT` is unset, so the
   directive expands to nothing. Harmless on Railway (which routes by the
   published port, and the `CMD` correctly uses `${PORT:-5000}` at runtime), but
   it is a false lead when reading build logs. Replacing it with `EXPOSE 5000`
   removes the noise.
3. **Artifact size.** `artifacts/phase2_xgboost_calibrated.joblib` is 7.9 MB and
   is committed (confirmed tracked, not git-ignored). If it were ever added to
   `.gitignore` or moved to LFS without the LFS smudge available in the builder,
   the image would build but `joblib.load` would fail at boot and the healthcheck
   would fail — which presents very similarly in the Railway UI.
4. **Healthcheck timeout.** `railway.toml` sets `healthcheckTimeout = 30`. Model
   load plus gunicorn's two workers can exceed that on a cold container; the
   deploy is then marked failed while the app is actually fine. Raising it to 60
   is the cheap fix.

Nothing needed changing, so nothing was changed. Do not modify the ML API's
`/predict` contract.

**The defence does not depend on any of this.** Set `INFERENCE_MODE=local` and
the backend loads the same artifact and runs the same vendored inference code
in-process.

---

## 1. Local — the defence configuration

```bash
cd sentinel-backend
cp .env.example .env          # then edit API_KEY
docker compose up --build
```

Brings up PostgreSQL 16 on host port **5433**, runs `alembic upgrade head`, and
serves the backend on **8080**. The compose file defaults `INFERENCE_MODE=local`,
so no internet is required.

```bash
cd sentinel-dashboard
cp .env.example .env
npm install
npm run dev                   # http://localhost:5173
```

`.env` for the dashboard:

```
VITE_API_URL=http://localhost:8080
VITE_WS_URL=ws://localhost:8080
VITE_API_KEY=<same value as the backend's API_KEY>
```

Smoke-test without hardware, using a stored real capture:

```bash
python scripts/replay.py scripts/samples/4_Real_crash_4_g___90_ms.json \
  --lat 5.6581 --lon -0.1812
```

### Running the backend without Docker

```bash
pip install -r requirements.txt
# Postgres:
export DATABASE_URL="postgresql+psycopg2://sentinel:sentinel@localhost:5433/sentinel"
# or SQLite for a laptop demo — same schema, no server:
export DATABASE_URL="sqlite:///./dev.db"
export INFERENCE_MODE=local
alembic upgrade head
uvicorn app.main:app --port 8080
```

### Pointing the ESP32 at your laptop

The device cannot reach `localhost`. Use the machine's LAN IP (`ipconfig` →
IPv4), set it in the firmware's `BACKEND_URL`, and make sure both are on the same
network. Windows Firewall will prompt for port 8080 on first run — allow it.

---

## 2. Railway — backend + PostgreSQL

1. **Postgres**: New → Database → PostgreSQL. Railway provisions
   `DATABASE_URL` as `postgresql://…`; SQLAlchemy 2 needs the driver spelled
   out, so set the backend's own variable to the `postgresql+psycopg2://…` form
   of the same URL.
2. **Backend**: New → GitHub Repo → `sentinel-backend`. The `Dockerfile` is
   detected automatically and runs `alembic upgrade head` before uvicorn, so
   migrations apply on every deploy.
3. **Variables**:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `postgresql+psycopg2://…` (from the Postgres service) |
   | `API_KEY` | a real secret — not the `.env.example` default |
   | `JWT_SECRET` | a separate, randomly generated secret of at least 32 characters |
   | `JWT_ISSUER` | `sentinel-backend` |
   | `JWT_AUDIENCE` | `sentinel-mobile` |
   | `ACCESS_TOKEN_MINUTES` | `15` |
   | `REFRESH_TOKEN_DAYS` | `30` |
   | `INFERENCE_MODE` | `remote`, or `local` to be independent of the ML API |
   | `ML_API_URL` | `https://accident-severity-api-production.up.railway.app` |
   | `ML_TIMEOUT_S` | `10` |
   | `CORS_ORIGINS` | the Vercel URL, e.g. `https://sentinel-dashboard.vercel.app` |
   | `HEARTBEAT_RETENTION_DAYS` | `7` |

   `PORT` is injected by Railway; the Dockerfile's `CMD` already honours it.
4. **Verify**: `curl https://<backend>.up.railway.app/health`.

Note `artifacts/` is copied into the image, so `INFERENCE_MODE=local` works on
Railway too.

---

## 3. Vercel — dashboard

1. New Project → import `sentinel-dashboard`. Framework preset: **Vite**.
   Build `npm run build`, output `dist`.
2. Environment variables:

   | Variable | Value |
   |---|---|
   | `VITE_API_URL` | `https://<backend>.up.railway.app` |
   | `VITE_WS_URL` | `wss://<backend>.up.railway.app` — **`wss`, not `ws`** |
   | `VITE_API_KEY` | same secret as the backend |

3. Add the resulting Vercel URL to the backend's `CORS_ORIGINS` and redeploy the
   backend.

`VITE_*` values are inlined at build time, so changing one requires a redeploy,
and anything in them is visible to anyone who opens the bundle. The dashboard's
shared key is adequate for a thesis demo and deliberately not presented as
production operator auth. The mobile app does not embed this key: drivers use
short-lived JWT access tokens with rotating refresh tokens.

### Why a raster basemap and no Mapbox token

The dashboard uses MapLibre GL against keyless OpenStreetMap raster tiles. A
Mapbox token can expire or rate-limit mid-defence, and CARTO's free basemap now
stamps "API KEY REQUIRED" watermarks across the tiles (observed during
development — this is why OSM is used). OSM tiles need no key; the canvas is
inverted in CSS to produce the dark console look, and markers sit outside the
canvas so their severity colours stay true. Attribution is kept on the map.

---

## 4. Post-deploy checklist

- [ ] `GET /health` on the backend returns `healthy`
- [ ] `GET /api/v1/ml/health` with the key returns `phase2_xgboost_calibrated` and `signature+impulse (v2)`
- [ ] Dashboard top bar shows **LIVE** (WebSocket up over `wss`)
- [ ] `replay.py` against the deployed backend creates an incident that appears without a refresh
- [ ] Re-running `replay.py` with the same `--event-id` creates **no** duplicate
- [ ] Firmware `BACKEND_URL` and `API_KEY` match the deployment
- [ ] Kill and restart the backend: the dashboard shows RECONNECTING, then recovers and backfills
