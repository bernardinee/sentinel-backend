#!/bin/sh
# Container entrypoint.
#
# Railway's private network (postgres.railway.internal) is not guaranteed to
# resolve at the instant a container starts, so running `alembic upgrade head`
# straight away races the network and dies with:
#
#   could not translate host name "postgres.railway.internal" to address
#
# Waiting for the database to actually accept a connection removes that race,
# and also covers the ordinary case of the DB restarting alongside the app.
set -e

PORT="${PORT:-8080}"
MAX_WAIT="${DB_WAIT_SECONDS:-60}"

echo "[start] waiting up to ${MAX_WAIT}s for the database…"
python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine, text

sys.path.insert(0, "/srv")
from app.config import get_settings          # applies the driver normalisation

url = get_settings().DATABASE_URL
deadline = time.time() + float(os.environ.get("DB_WAIT_SECONDS", "60"))
last = None
while time.time() < deadline:
    try:
        create_engine(url, pool_pre_ping=True).connect().execute(text("SELECT 1"))
        print("[start] database reachable")
        sys.exit(0)
    except Exception as exc:
        last = exc
        time.sleep(2)
print(f"[start] database unreachable after timeout: {last}", file=sys.stderr)
sys.exit(1)
PY

echo "[start] applying migrations…"
alembic upgrade head

echo "[start] launching uvicorn on :${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
