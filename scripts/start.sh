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
import os, re, sys, time
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, "/srv")
from app.config import get_settings          # applies the driver normalisation


def redact(u: str) -> str:
    """Show enough of the URL to debug it, without leaking the password."""
    return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", u)


url = get_settings().DATABASE_URL

# A malformed URL is a CONFIGURATION error, not a transient one — retrying it
# for a minute only buries the real message. Fail immediately and say what we
# actually received, because the usual cause is an unresolved platform
# variable reference arriving as literal text.
if not url.strip():
    print("[start] FATAL: DATABASE_URL is empty. Set it on THIS service "
          "(variable scopes are per-service).", file=sys.stderr)
    sys.exit(1)
try:
    make_url(url)
except Exception as exc:
    print(f"[start] FATAL: DATABASE_URL is not a valid SQLAlchemy URL: {exc}",
          file=sys.stderr)
    print(f"[start] received: {redact(url)!r}", file=sys.stderr)
    if "${{" in url or "${" in url:
        print("[start] hint: that looks like an UNRESOLVED variable reference. "
              "Check the referenced service name matches exactly, or paste the "
              "literal connection string instead.", file=sys.stderr)
    sys.exit(1)

# Past this point the URL is well-formed, so failures really are connectivity
# and are worth waiting out (platform DNS can lag container start).
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
print(f"[start] database unreachable after {int(float(os.environ.get('DB_WAIT_SECONDS','60')))}s",
      file=sys.stderr)
print(f"[start] host: {make_url(url).host} — last error: {last}", file=sys.stderr)
sys.exit(1)
PY

echo "[start] applying migrations…"
alembic upgrade head

echo "[start] launching uvicorn on :${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
