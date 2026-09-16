"""Re-run classification on stored incident windows with the current thresholds.

Use after changing the signature profile in app/modules/inference/phase2_vendored.py,
so incidents recorded under the old limits are judged by the same rules as new
ones. Only the classification fields are rewritten; status, acknowledgements,
dispatches and notes are left alone. Manual panic incidents are skipped — they
never went through the model.

Dry run by default: prints what would change and writes nothing.

    # against the deployed database (cmd.exe)
    set DATABASE_URL=postgresql+psycopg2://...
    python scripts/reclassify.py            # preview
    python scripts/reclassify.py --apply    # write the changes
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402

from app.db import SessionLocal                                  # noqa: E402
from app.models import Incident, IncidentWindow                  # noqa: E402
from app.modules.inference import phase2_vendored                # noqa: E402
from app.modules.ingest import _apply_classification             # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Write the new labels")
    args = ap.parse_args()

    print("Signature profile:", phase2_vendored.signature_thresholds())
    changed = total = 0

    with SessionLocal() as db:
        rows = db.execute(
            select(Incident, IncidentWindow)
            .join(IncidentWindow, IncidentWindow.incident_id == Incident.id)
            .order_by(Incident.received_at)
        ).all()

        for incident, w in rows:
            if incident.label_source == "manual_panic":
                continue
            total += 1
            res = phase2_vendored.run_inference(w.ax, w.ay, w.az, w.gx, w.gy, w.gz)
            before = incident.severity_name or "pending"
            after = res["severity_name"]
            if before != after or incident.label_source != res["label_source"]:
                changed += 1
                sig = res["crash_signature"]
                print(f"  {incident.event_id}: {before} -> {after} "
                      f"({sig['peak_g']} g, {sig['excursion_ms']:.0f} ms, "
                      f"P(crash) {res['p_crash']}, {res['label_source']})")
            if args.apply:
                _apply_classification(incident, res)

        if args.apply:
            db.commit()

    verb = "Updated" if args.apply else "Would update"
    print(f"{verb} {changed} of {total} incidents.")
    if not args.apply and changed:
        print("Run again with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
