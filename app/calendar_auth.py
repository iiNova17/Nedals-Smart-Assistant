"""Local, separate Calendar free/busy consent; never reuse or broaden the Drive token."""

import argparse
import logging
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import Settings
from app.drive_auth import save_credentials
from app.scheduling import SCOPES


def main():
    parser = argparse.ArgumentParser(description="Authorize calendar availability queries")
    parser.add_argument("--client", type=Path, default=Path("secrets/google-oauth-client.json"))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(args.client), SCOPES)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            authorization_prompt_message="",
            success_message="Calendar connected. You may close this window.",
            access_type="offline",
            prompt="consent",
            timeout_seconds=300,
        )
        save_credentials(credentials, Settings().calendar_token_path)
        print(
            "Calendar credentials saved separately. Share calendars with this "
            "account and link their IDs."
        )
    except Exception:
        print("Calendar setup failed. Check API enablement, test users and Desktop OAuth client.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
