"""Stats (§5.2). manual_panic incidents are excluded everywhere here — a human
pressing a button must never contaminate model-performance statistics (§5.4)."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Incident, as_aware, utcnow
from app.schemas import StatsSummary

router = APIRouter(tags=["stats"])

SEVERITY_NAMES = {0: "Normal", 1: "Moderate", 2: "Severe"}


@router.get("/stats/summary", response_model=StatsSummary)
def stats_summary(db: Session = Depends(get_db), _=Depends(require_role())):
    base = select(Incident).where(Incident.label_source != "manual_panic")
    rows = db.execute(base).scalars().all()

    by_severity: dict[str, int] = {"Normal": 0, "Moderate": 0, "Severe": 0, "pending": 0}
    by_label_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    inference_ms: list[float] = []
    end_to_end_s: list[float] = []
    now = utcnow()
    last_24h = last_7d = last_30d = 0

    for r in rows:
        if r.severity_class is None:
            by_severity["pending"] += 1
        else:
            name = SEVERITY_NAMES.get(r.severity_class, str(r.severity_class))
            by_severity[name] = by_severity.get(name, 0) + 1
        if r.label_source:
            by_label_source[r.label_source] = by_label_source.get(r.label_source, 0) + 1
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.inference_time_ms is not None:
            inference_ms.append(r.inference_time_ms)
        if r.detected_at is not None and r.received_at is not None:
            delta = (as_aware(r.received_at) - as_aware(r.detected_at)).total_seconds()
            if 0 <= delta < 3600:  # discard clock-skew outliers
                end_to_end_s.append(delta)
        age = now - as_aware(r.received_at)
        if age <= timedelta(hours=24):
            last_24h += 1
        if age <= timedelta(days=7):
            last_7d += 1
        if age <= timedelta(days=30):
            last_30d += 1

    return StatsSummary(
        total_incidents=len(rows),
        by_severity=by_severity,
        by_label_source=by_label_source,
        by_status=by_status,
        mean_inference_ms=round(sum(inference_ms) / len(inference_ms), 2) if inference_ms else None,
        mean_end_to_end_s=round(sum(end_to_end_s) / len(end_to_end_s), 2) if end_to_end_s else None,
        last_24h=last_24h, last_7d=last_7d, last_30d=last_30d)
