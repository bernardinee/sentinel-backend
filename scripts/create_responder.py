"""Create (or update) a responder account for the dispatch console.

Run by an operator against the database directly — deliberately NOT an HTTP
endpoint. `/auth/register` only ever creates drivers; if it could mint
responders, anyone who reached the API could grant themselves dispatch control
over the whole fleet. Responder accounts are therefore provisioned out of band,
the same way you would add an operator to any real dispatch system.

    # local
    python scripts/create_responder.py --email ops@sentinel.gh --name "Control Room"

    # against the deployed database
    set DATABASE_URL=postgresql+psycopg2://...
    python scripts/create_responder.py --email ops@sentinel.gh --name "Control Room"

The password is prompted for and never taken from argv, so it cannot leak into
shell history or process listings. Use --reset-password to rotate an existing
account's password.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pwdlib import PasswordHash                      # noqa: E402
from sqlalchemy import select                        # noqa: E402

from app.db import SessionLocal                      # noqa: E402
from app.models import User, utcnow                  # noqa: E402

MIN_PASSWORD_LEN = 12


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--phone", default="")
    ap.add_argument("--reset-password", action="store_true",
                    help="Rotate the password of an existing account")
    ap.add_argument("--deactivate", action="store_true",
                    help="Disable the account instead of creating it")
    args = ap.parse_args()

    email = args.email.strip().lower()
    hasher = PasswordHash.recommended()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))

        if args.deactivate:
            if user is None:
                print(f"No account for {email}", file=sys.stderr)
                return 1
            user.active = False
            db.commit()
            print(f"Deactivated {email}. Existing access tokens expire within "
                  f"the access-token lifetime.")
            return 0

        if user is not None and not args.reset_password:
            print(f"{email} already exists (role={user.role}, active={user.active}).\n"
                  f"Use --reset-password to set a new password.", file=sys.stderr)
            return 1

        password = getpass.getpass("New password: ")
        if len(password) < MIN_PASSWORD_LEN:
            print(f"Password must be at least {MIN_PASSWORD_LEN} characters.",
                  file=sys.stderr)
            return 1
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1

        if user is None:
            user = User(
                name=args.name.strip(),
                email=email,
                phone=args.phone.strip(),
                password_hash=hasher.hash(password),
                role="responder",
                device_id=None,        # responders are not bound to a device
                active=True,
            )
            db.add(user)
            action = "Created"
        else:
            user.password_hash = hasher.hash(password)
            user.name = args.name.strip() or user.name
            user.role = "responder"
            user.active = True
            user.updated_at = utcnow()
            action = "Updated"

        db.commit()
        print(f"{action} responder account: {email}")
        print("Sign in at the dashboard with this email and password.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
