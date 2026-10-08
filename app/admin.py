"""Local administrator CLI; no unauthenticated enrollment endpoint."""

import argparse
import secrets

from app.config import Settings
from app.db import Database


def main():
    parser = argparse.ArgumentParser(description="Manage Nedal’s Smart Assistant access locally")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("--name", required=True)
    revoke = sub.add_parser("revoke-user")
    revoke.add_argument("--id", required=True)
    member = sub.add_parser("approve-whatsapp-member")
    member.add_argument("--phone", required=True)
    member.add_argument("--name", required=True)
    args = parser.parse_args()
    db = Database(Settings().database_path)
    db.initialize()
    if args.command == "add-user":
        if not args.name.strip() or len(args.name) > 100:
            parser.error("Name must contain 1–100 characters")
        token = secrets.token_urlsafe(32)
        user = db.enroll(args.name.strip(), token)
        print(f"User ID: {user.id}")
        print(
            "Store this bearer token securely. It is shown once; the database stores only its hash."
        )
        print(token)
    elif args.command == "approve-whatsapp-member":
        try:
            user = db.approve_whatsapp_member(args.phone.lstrip("+"), args.name)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"WhatsApp access approved. User ID: {user.id}")
    else:
        try:
            if not db.revoke(args.id):
                parser.error("User not found")
        except PermissionError as exc:
            parser.error(str(exc))
        print("User access revoked.")


if __name__ == "__main__":
    main()
