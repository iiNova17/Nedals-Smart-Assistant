"""Local pilot configuration. Never writes personal identifiers into source control."""

import argparse
import secrets
from pathlib import Path

from app.config import Settings, TeamConfig
from app.db import Database


def main():
    parser = argparse.ArgumentParser(description="Configure the owner-only WhatsApp pilot")
    parser.add_argument("--owner-phone", required=True)
    parser.add_argument("--group-name", required=True)
    parser.add_argument("--drive-folder-id", default="")
    args = parser.parse_args()
    settings = Settings()
    config = TeamConfig(
        owner_phone=args.owner_phone.removeprefix("+"),
        group_name=args.group_name,
        drive_folder_id=args.drive_folder_id,
    )
    path = settings.team_config_path
    if path.exists():
        previous = TeamConfig.model_validate_json(path.read_text("utf-8"))
        if previous.owner_phone != config.owner_phone:
            parser.error("The permanent owner cannot be replaced by setup")
        if previous.group_name == config.group_name:
            config.group_id = previous.group_id
    db = Database(settings.database_path)
    db.initialize()
    db.ensure_owner(config.owner_phone)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(config.model_dump_json(indent=2), "utf-8")
    temporary.replace(path)
    if not settings.bridge_token.get_secret_value():
        with Path(".env").open("a", encoding="utf-8") as file:
            file.write("\nPLUME_BRIDGE_TOKEN=" + secrets.token_urlsafe(48) + "\n")
    Path("secrets").mkdir(exist_ok=True)
    print("Owner registered; team settings and bridge token saved in ignored local files.")


if __name__ == "__main__":
    main()
