# Sentinel — Final Report

Software tier for an ML-based road accident detection and severity
classification system (BSc Computer Engineering thesis, University of Ghana).

Three repositories, all under `C:\dev\` and all git-initialised with commits:

| Repo | What it is |
|---|---|
| `C:\dev\sentinel-backend` | FastAPI modular monolith, Alembic, docker-compose, 19 tests |
| `C:\dev\sentinel-dashboard` | React + Vite + TS responder console |
| `C:\dev\sentinel-firmware` | Complete updated `.ino` |

Documents: `API_CONTRACT.md`, `ARCHITECTURE.md`, `DEPLOYMENT.md` (in the backend
repo) and a README per repo.

---

## 1. What was built

**Backend.** `POST /api/v1/events` validates the payload (exactly 500 samples,
`fs_hz == 100`, `units_accel == "g"`, plus a semantic unit assertion), enforces
idempotency on `event_id`, upserts the device, and **persists the incident and
the raw 500×6 window before calling the model**. It then classifies (remote ML
API with timeout and retries, or the vendored model in-process), writes the
result back, broadcasts over WebSocket, and returns a small flat ACK to the
device. Query, action and Sentinel-hook endpoints are complete, along with a
WebSocket hub with 20 s keepalive, a background retry loop for pending
classifications, and heartbeat pruning.

**Dashboard.** Four screens — Live Operations, Incident Detail, History &
Analytics, Devices — all reading live API data. No seeded rows, no
`Math.random()`, no placeholder incidents.

**Firmware.** All six §6 changes, compiling against ESP32 core 3.3.8.

---

## 2. Verified working

Everything below was executed, not assumed.

| # | Check | Result |
|---|---|---|
| 1 | Backend test suite | **19 passed** — ingest validation, idempotency, ML retry/fallback, dispatch state machine, panic exclusion |
| 2 | Real crash window through full ingest | `Moderate`, `model+signature`, peak 4.404 g, `accident_confirmed: true` |
| 3 | **Negative case**: brief 12 g spike | `Normal`, `signature_override`, `signature_match: false` — the physics gate correctly rejects it |
| 4 | Idempotency | Same `event_id` posted 3× → 1 incident, inference ran once |
| 5 | WebSocket broadcast | `wscat`-style subscriber received `incident.created` with correct severity and coordinates |
| 6 | Cross-tab live update | Acknowledge in tab A → tab B showed `ACKNOWLEDGED` **without a refresh** |
| 7 | Kill backend mid-session | Dashboard showed `RECONNECTING`, recovered to `LIVE` |
| 8 | Reconnect backfill | Incident created while the socket was down appeared after reconnect, no reload |
| 9 | ML API unreachable | Incident + window still persisted, `classification_pending: true`, retry queued |
| 10 | Unit assertion | Window scaled to m/s² → **422**, naming the likely mistake |
| 11 | Firmware compiles | 84 % flash, 19 % RAM, **262 kB heap free** (both `ENABLE_GSM` 0 and 1) |
| 12 | Firmware JSON contract | Exact envelope accepted: 28,342-byte payload (< 40 kB reserve), 246-byte flat ACK (< 512 B buffer), retry created no duplicate |
| 13 | Live ML API | `phase2_xgboost_calibrated` / `signature+impulse (v2)` — **the corrected model** |

**Not verified:** anything requiring the physical rig. The §10 bench test (shake
the device, watch the card animate in, confirm the SMS) needs hardware I do not
have. Every layer it depends on has been exercised independently, including the
exact firmware payload shape.

---

## 3. Measured latency

Backend `/api/v1/stats/summary`, from real ingests:

```
mean_inference_ms  17.74
mean_end_to_end_s   2.54     (device detected_at → server received_at)
```

Inference, 3 runs on the real 4 g / 90 ms window:

| Mode | Median |
|---|---|
| Local (in-process) | **8.6 ms** |
| Remote server-side | 192 ms |
| Remote round-trip from Accra | **926 ms** |

The remote round-trip is dominated by the network, not the model. Local mode is
roughly 100× faster end-to-end and removes the dependency entirely.

**Expected bench figure.** With the impact-centred window, the device waits
2.5 s of post-roll, then uploads (~0.5–2.5 s depending on link), then the backend
classifies (~9 ms local). Under the old firmware the wait was 5 s, so the change
should cut roughly 2.5 s off every alert. Quote the real number from the rig;
the 2.54 s above is over LAN with a synthetic `detected_at`.

---

## 4. Findings — things I changed or think you should know

### 4.1 The accelerometer has been clipping every crash (highest priority)

The previous firmware called `mpu.initialize()`. In the MPU6050 library that
sets `MPU6050_ACCEL_FS_2` — **±2 g** — while the pin comment claimed "±16g
range". `RAW_ACCEL_TO_G = 1/16384` is the correct scale *for ±2 g*, so the units
were right and nothing looked wrong.

But the sensor physically saturates at 2 g per axis. The model's entire crash
band is 2–7 g. The theoretical maximum measurable resultant was
√(3 × 2²) = **3.46 g**, and only with all three axes saturated simultaneously.

So no real crash could ever have been measured in-band. Peaks above 3.46 g
reported in the field (4.9 g, 6.3 g in the SMS examples) cannot have come from
unclipped data — the server-side 20 Hz `filtfilt` overshoots on clipped square-ish
transients and inflates the peak, which is the most likely explanation.

**Changed:** the firmware now sets `MPU6050_ACCEL_FS_16` explicitly with
`RAW_ACCEL_TO_G = 1/2048`. This is outside the six §6 changes, but the acceptance
test cannot pass honestly without it.

**Please verify on the bench** by printing `mpu.getFullScaleAccelRange()` (expect
`3`) and confirming a hard tap now reads above 3.5 g. **If any field data was
collected with the old setting, treat its peak values as censored at ~3.46 g.**

I left the **gyro** at the library default (±250 °/s) to keep the change minimal,
but it will also clip during a crash tumble. Only 2 of 25 features are
gyro-derived (`gx_mean`, `gx_kurt`), so the impact is smaller — worth a look.

### 4.2 The Railway ML API is not broken

The brief says to treat the live URL as untrusted until verified. Verified, twice:

```json
{"model":"phase2_xgboost_calibrated","taxonomy":"signature+impulse (v2)",
 "crash_alert_threshold":0.396,"n_features":25,"status":"healthy"}
```

Live predictions are also correct — the real crash returns `model+signature`, the
brief spike returns `signature_override`. The deployment self-resolved. Nothing
was changed. `DEPLOYMENT.md` §0 still diagnoses the four most likely causes for
recurrence (SciPy source-build OOM, `EXPOSE $PORT` evaluated at build time, the
7.9 MB artifact's git tracking, and a 30 s healthcheck timeout against cold-start
model load).

### 4.3 Raw peak and filtered peak legitimately disagree

The dashboard plots the raw stored window; the signature figures are computed on
the 20 Hz low-passed window (to match training). Zero-phase `filtfilt` overshoots
on sharp transients, so on the 12 g spike the **raw peak is 12.15 g and the
filtered peak is 14.02 g**.

Rather than hide this, both are labelled on screen. It is defensible and shows
the pipeline is faithful to training — but expect the question, and know the
answer.

### 4.4 `signature_override` does not mean "the model said crash"

The gate fires on `p_crash` crossing the alert threshold, which is **not** the
model's argmax. On the 12 g spike the top class was Normal (51.5 %) while
`p_crash` was 0.485 against a 0.396 threshold — so the override fired with
`model_severity: "Normal"`.

My first draft of the explanation text read *"Model flagged this (Normal)"*,
which is nonsense. The copy now quotes P(crash) against the threshold instead.
Worth being precise about this in the write-up.

### 4.5 CARTO watermarks its free basemap

The brief specifies a free raster basemap and no Mapbox token. CARTO's dark
basemap — the obvious choice — now stamps **"API KEY REQUIRED"** across every
tile. Switched to keyless OpenStreetMap tiles, inverted in CSS for the dark look
(markers sit outside the canvas and keep their true colours).

### 4.6 Coordinate-format guard in firmware

TinyGPSPlus returns decimal degrees, but raw NMEA is `ddmm.mmmm` — `539.5128`
means 5.6585° N, not 539°. A sketch that prints or sends the raw value produces a
valid-looking, badly wrong fix. `gpsFixValid()` now rejects `|lat| > 90` or
`|lon| > 180` before marking a fix valid. Cheap insurance against a class of bug
that is invisible until someone looks at the map.

### 4.7 NTP added for `detected_at`

The §5.1 payload includes `detected_at` and the dashboard shows a latency
breakdown, but the ESP32 has no RTC. The firmware now syncs UTC over NTP after
WiFi and back-dates the timestamp to the trigger instant (not the upload
instant), so the latency metric measures the network rather than our own
post-roll wait. If NTP has not synced, the field is omitted — a wrong timestamp
is worse than an absent one.

---

## 5. Things I did not do

- **Did not touch the model, its thresholds, or the `/predict` contract.**
- **Did not add a peak-g escalation.** Severity is graded by impulse only.
- **Did not build the Sentinel mobile app** — endpoints only, documented in
  `API_CONTRACT.md` §4.
- **Did not build real auth.** A shared `X-API-Key` with a role stub, structured
  so JWT drops into one dependency without touching a route handler.
- **Did not split into microservices.** Rationale in `ARCHITECTURE.md` §3.
- **Did not restructure the firmware** beyond the six changes plus §4.1 and §4.7.

---

## 6. Disagreements with the brief

**"Repoint the ESP32, then the SMS path stays exactly as it is."** The two pull
in opposite directions. §6.3 says SMS fires on the local threshold decision
regardless of the POST; §6.7 says SMS is driven by the response fields; and the
field examples include a confidence figure, which only exists after
classification. My resolution: attempt the POST first, then **always** send the
SMS, using backend values when they arrived and local measurements when they did
not. That keeps the message format and preserves independence — the SMS goes out
whether or not the network worked. Flagging it because it is a judgement call on
a fail-safe.

**GPS/GSM firmware was not in the workspace.** The brief references
`ESP32_Crash_Detection_GPS_GSM.ino` and instructs me to read the real `#define`
block rather than trust the document. I searched the whole Desktop tree; only
`ESP32_Complete_3LED_System.ino` exists, and it has no GPS and no GSM
(`GPS_TX` is commented out). Those sections are therefore **reconstructed from
the brief's hardware description**, and the placeholder `GSM_RX_PIN 26` visibly
collides with `LED_GREEN 26`. I left the collision visible and `ENABLE_GSM 0`
rather than inventing a plausible-looking pin map — the brief's own warning about
pin drift is exactly why. **Reconcile this against your working sketch before
flashing.**

**`/api/v1/incidents/active` sorts by severity with NULLs last.** Pending
classifications sort below Severe rather than to the top. Arguable: an unclassified
incident might deserve attention first. Left as specified.

---

## 7. Next steps

1. Flash the firmware, fix the GSM pins, set `ENABLE_GSM 1`, confirm
   `getFullScaleAccelRange()` returns 3 (§4.1).
2. Run the §10 acceptance test on the bench, including the negative case (a sharp
   tap should produce `signature_override` and the plain-English explanation).
3. Record the real end-to-end latency for Chapter 4 — the numbers in §3 are
   LAN/replay figures.
4. Deploy per `DEPLOYMENT.md`, or run locally with `INFERENCE_MODE=local`; the
   defence does not need the internet.
5. Re-check whether any existing field data was collected under the ±2 g setting.
