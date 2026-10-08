"""Interactive, local Google Drive authorization. No token is exposed to Gemini."""

import argparse
import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import Settings, load_team

# Existing arbitrary files and future uploads need Drive access. drive.file alone
# does not grant recursive access to an existing folder's contents.
SCOPES = ["https://www.googleapis.com/auth/drive"]


def save_credentials(credentials: Credentials, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect the project Drive folder locally")
    parser.add_argument("--client", type=Path, default=Path("secrets/google-oauth-client.json"))
    parser.add_argument("--token", type=Path, default=Path("secrets/google-drive-token.json"))
    parser.add_argument(
        "--check", action="store_true", help="Check existing access without a login"
    )
    args = parser.parse_args()
    team = load_team(Settings())
    if not team or not team.drive_folder_id:
        parser.error("Configure a Drive project folder using app.setup first")
    # Library logs can include OAuth callback URLs or request data.
    logging.disable(logging.CRITICAL)
    try:
        credentials = None
        if args.token.is_file():
            credentials = Credentials.from_authorized_user_file(str(args.token), SCOPES)
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            save_credentials(credentials, args.token)
        if not credentials or not credentials.valid:
            if args.check:
                print("Drive is not authorized yet. Run this command without --check to sign in.")
                return 1
            if not args.client.is_file():
                print("Save your Desktop app OAuth JSON as secrets/google-oauth-client.json first.")
                print("See docs/drive-setup.md for the Google Cloud setup steps.")
                return 1
            client = json.loads(args.client.read_text("utf-8"))
            if "installed" not in client:
                print("The OAuth client must be a Desktop app, not a Web application.")
                return 1
            print("Opening Google sign-in. Choose the account that can edit the project folder.")
            print(
                "Google will request Drive-wide access; folder boundaries are enforced by the app."
            )
            flow = InstalledAppFlow.from_client_config(
                client, SCOPES, autogenerate_code_verifier=True
            )
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                authorization_prompt_message="",
                success_message="Drive authorization received. You may close this window.",
                access_type="offline",
                prompt="consent",
                timeout_seconds=300,
            )
            save_credentials(credentials, args.token)
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        try:
            folder = (
                service.files()
                .get(
                    fileId=team.drive_folder_id,
                    fields="id,mimeType,trashed,capabilities(canAddChildren)",
                    supportsAllDrives=True,
                )
                .execute(num_retries=0)
            )
            if (
                folder.get("trashed")
                or folder.get("mimeType") != "application/vnd.google-apps.folder"
            ):
                print("The configured Drive ID is not an active folder.")
                return 1
            if not folder.get("capabilities", {}).get("canAddChildren"):
                print("Drive connected, but this account cannot upload into the project folder.")
                return 1
            files = (
                service.files()
                .list(
                    q=f"'{team.drive_folder_id}' in parents and trashed = false",
                    pageSize=100,
                    fields="files(id,mimeType),nextPageToken",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=0)
            )
            count = len(files.get("files", []))
            suffix = "+" if files.get("nextPageToken") else ""
            print(
                f"Drive connected: folder is readable/writable; {count}{suffix} direct items found."
            )
            print(
                "Credentials saved locally. No file was uploaded, changed or indexed by this check."
            )
            return 0
        finally:
            service.close()
    except Exception as exc:
        # Safe class name only: HTTP/OAuth exception strings may contain credentials.
        print(
            f"Drive setup failed ({type(exc).__name__}). Check API enablement, test-user access, "
            "OAuth client type, and folder permissions."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
