"""Local document-index inspection and deliberate retry; never alters Drive originals."""

import argparse
import json
import logging

from app.config import Settings, load_team
from app.db import Database
from app.drive import DriveClient
from app.knowledge import Knowledge


def main():
    parser = argparse.ArgumentParser(description="Manage the project PDF index")
    parser.add_argument("command", choices=["status", "sync", "retry"])
    parser.add_argument("--file-id")
    args = parser.parse_args()
    settings = Settings()
    team = load_team(settings)
    if not team or not team.drive_folder_id or not settings.gemini_api_key.get_secret_value():
        parser.error("Configure the project Drive folder and Gemini key first")
    if args.command == "retry" and not args.file_id:
        parser.error("retry requires --file-id")
    # Vendor error logs must not expose credentials or document contents.
    logging.disable(logging.CRITICAL)
    db = Database(settings.database_path)
    db.initialize()
    knowledge = Knowledge(
        settings, db, DriveClient(team.drive_folder_id, settings.drive_token_path)
    )
    try:
        if args.command == "retry":
            rows = {row["drive_id"]: row for row in knowledge.records()}
            if args.file_id not in rows or rows[args.file_id]["status"] != "failed":
                parser.error("Select a failed document from status")
            knowledge.update(args.file_id, status="stale", error="")
        result = knowledge.status() if args.command == "status" else knowledge.sync()
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        knowledge.close()


if __name__ == "__main__":
    main()
