"""Google Drive is canonical storage; all operations stay inside one project root."""

import mimetypes
import re
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from app.drive_auth import SCOPES, save_credentials

FOLDER = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,parents,modifiedTime,size,webViewLink,trashed,sha256Checksum"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class DriveError(Exception):
    pass


def checked_id(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise DriveError("Invalid Drive identifier")
    return value


def public_file(file: dict, path: str = "") -> dict:
    # Never return credentials, descriptions, sharing data, or arbitrary provider fields.
    file_id = checked_id(file["id"])
    url_kind = "drive/folders" if file.get("mimeType") == FOLDER else "file/d"
    return {
        "id": file_id,
        "name": file.get("name", "Unnamed file"),
        "mime_type": file.get("mimeType", "application/octet-stream"),
        "modified_at": file.get("modifiedTime", ""),
        "size": file.get("size"),
        "drive_url": f"https://drive.google.com/{url_kind}/{file_id}",
        "path": path,
        "sha256": file.get("sha256Checksum"),
    }


class DriveClient:
    def __init__(self, root_id: str, token_path: Path):
        self.root_id = checked_id(root_id)
        self.token_path = token_path
        # googleapiclient transports are not thread safe. Each operation gets a new
        # client; serialize refreshes and writes, including after async cancellation.
        self.lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return self.token_path.is_file()

    @contextmanager
    def session(self):
        with self.lock:
            service = None
            try:
                if not self.ready:
                    raise DriveError("Google Drive authorization is not configured")
                credentials = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
                if not credentials.valid:
                    credentials.refresh(Request())
                    save_credentials(credentials, self.token_path)
                service = build(
                    "drive",
                    "v3",
                    http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)),
                    cache_discovery=False,
                )
                root = self.raw_get(service, self.root_id)
                if root.get("mimeType") != FOLDER or root.get("trashed"):
                    raise DriveError("Project Drive folder is unavailable")
                yield service
            except DriveError:
                raise
            except Exception:
                raise DriveError(
                    "Drive request failed; check authorization and try again"
                ) from None
            finally:
                if service:
                    service.close()

    @staticmethod
    def raw_get(service, file_id):
        return (
            service.files()
            .get(fileId=checked_id(file_id), fields=FIELDS, supportsAllDrives=True)
            .execute(num_retries=0)
        )

    def scoped_get(self, service, file_id: str) -> dict:
        file = self.raw_get(service, file_id)
        node = file
        visited = set()
        for _ in range(64):
            if node.get("trashed"):
                raise DriveError("The file or one of its parent folders is in trash")
            if node["id"] == self.root_id:
                return file
            if node["id"] in visited:
                break
            visited.add(node["id"])
            parents = node.get("parents", [])
            if len(parents) != 1:
                break
            node = self.raw_get(service, parents[0])
        raise DriveError("File is outside the configured project folder")

    @staticmethod
    def children(service, folder_id: str):
        token = None
        while True:
            response = (
                service.files()
                .list(
                    q=f"'{checked_id(folder_id)}' in parents and trashed = false",
                    fields=f"files({FIELDS}),nextPageToken",
                    pageSize=100,
                    pageToken=token,
                    orderBy="modifiedTime desc",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=0)
            )
            yield from response.get("files", [])
            token = response.get("nextPageToken")
            if not token:
                break

    def search(
        self,
        query: str = "",
        folders_only: bool = False,
        limit: int = 20,
        sha256: str | None = None,
    ) -> dict:
        if len(query) > 200 or not 1 <= limit <= 25:
            raise DriveError("Invalid search limits")
        with self.session() as service:
            root = self.raw_get(service, self.root_id)
            queue = deque([(self.root_id, root.get("name", "Project"))])
            visited = set()
            matches = []
            scanned = 0
            incomplete = False
            while queue:
                folder_id, path = queue.popleft()
                if folder_id in visited:
                    continue
                visited.add(folder_id)
                if len(visited) > 100:
                    incomplete = True
                    break
                # Recheck each folder's ancestry, including if moved since listing.
                self.scoped_get(service, folder_id)
                for file in self.children(service, folder_id):
                    scanned += 1
                    if scanned > 2000:
                        incomplete = True
                        break
                    is_folder = file.get("mimeType") == FOLDER
                    file_path = f"{path}/{file.get('name', 'Unnamed file')}"
                    if is_folder:
                        queue.append((file["id"], file_path))
                    # Shortcuts are listed as shortcuts, never followed out of the root.
                    if sha256 and file.get("sha256Checksum") != sha256:
                        continue
                    if query.casefold() in file.get("name", "").casefold() and (
                        is_folder or not folders_only
                    ):
                        matches.append(public_file(file, file_path))
                if incomplete:
                    break
            matches.sort(key=lambda file: file["modified_at"], reverse=True)
            return {
                "files": matches[:limit],
                "incomplete_scan": incomplete,
                "more_matches": len(matches) > limit,
                "source": "project_drive_metadata",
            }

    def list_folder(self, folder_id: str = "") -> dict:
        with self.session() as service:
            folder = self.scoped_get(service, folder_id or self.root_id)
            if folder.get("mimeType") != FOLDER:
                raise DriveError("This item is not a folder")
            results = []
            for file in self.children(service, folder["id"]):
                if len(results) == 100:
                    return {"files": results, "more_matches": True}
                results.append(public_file(file))
            return {"files": results, "more_matches": False}

    def metadata(self, file_id: str) -> dict:
        with self.session() as service:
            return public_file(self.scoped_get(service, file_id))

    def pdf_catalog(self) -> list[dict]:
        """Complete bounded catalog; never silently omit files from reconciliation."""
        with self.session() as service:
            queue, visited, files = deque([self.root_id]), set(), []
            scanned = 0
            while queue:
                folder_id = queue.popleft()
                if folder_id in visited:
                    continue
                visited.add(folder_id)
                if len(visited) > 100:
                    raise DriveError("Project exceeds the pilot's folder scan limit")
                self.scoped_get(service, folder_id)
                for file in self.children(service, folder_id):
                    scanned += 1
                    if scanned > 2000:
                        raise DriveError("Project exceeds the pilot's file scan limit")
                    if file.get("mimeType") == FOLDER:
                        queue.append(file["id"])
                    elif file.get("mimeType") == "application/pdf":
                        files.append(public_file(file))
            return files

    def download_pdf(self, file_id: str, path: Path, max_bytes: int) -> dict:
        """Temporary bounded download; recheck access and revision after transfer."""
        with self.session() as service:
            before = self.scoped_get(service, file_id)
            if before.get("mimeType") != "application/pdf":
                raise DriveError("Only PDF content is supported in this increment")
            if int(before.get("size", 0)) > max_bytes:
                raise DriveError("PDF exceeds the configured size limit")
            try:
                with path.open("xb") as output:
                    download = MediaIoBaseDownload(
                        output,
                        service.files().get_media(fileId=file_id, supportsAllDrives=True),
                        chunksize=256 * 1024,
                    )
                    done = False
                    while not done:
                        _, done = download.next_chunk(num_retries=0)
                        if output.tell() > max_bytes:
                            raise DriveError("PDF exceeds the configured size limit")
                after = self.scoped_get(service, file_id)
                if (before.get("sha256Checksum"), before.get("modifiedTime")) != (
                    after.get("sha256Checksum"),
                    after.get("modifiedTime"),
                ):
                    raise DriveError("Drive file changed while it was being downloaded")
                return public_file(after)
            except Exception:
                path.unlink(missing_ok=True)
                raise

    def create_folder(self, name: str, parent_id: str = "") -> dict:
        if not name.strip() or len(name) > 200 or any(c in name for c in "/\\\x00"):
            raise DriveError("Invalid folder name")
        with self.session() as service:
            parent = self.scoped_get(service, parent_id or self.root_id)
            if parent.get("mimeType") != FOLDER:
                raise DriveError("Destination is not a folder")
            matches = [
                f
                for f in self.children(service, parent["id"])
                if f.get("name") == name and f.get("mimeType") == FOLDER
            ]
            if len(matches) > 1:
                raise DriveError("Multiple folders have this name; select a folder by ID")
            if matches:
                return public_file(matches[0])
            created = (
                service.files()
                .create(
                    body={"name": name, "mimeType": FOLDER, "parents": [parent["id"]]},
                    fields=FIELDS,
                    supportsAllDrives=True,
                )
                .execute(num_retries=0)
            )
            return public_file(created)

    def allocate_id(self) -> str:
        with self.session() as service:
            return (
                service.files()
                .generateIds(count=1, space="drive", type="files")
                .execute(num_retries=0)["ids"][0]
            )

    def rename(self, file_id: str, name: str) -> dict:
        if not name.strip() or len(name) > 200 or any(c in name for c in "/\\\x00"):
            raise DriveError("Invalid name")
        with self.session() as service:
            file = self.scoped_get(service, file_id)
            if file["id"] == self.root_id:
                raise DriveError("The configured project root cannot be renamed through chat")
            return public_file(
                service.files()
                .update(
                    fileId=file["id"], body={"name": name}, fields=FIELDS, supportsAllDrives=True
                )
                .execute(num_retries=0)
            )

    def move(self, file_id: str, destination: str) -> dict:
        with self.session() as service:
            file = self.scoped_get(service, file_id)
            folder = self.scoped_get(service, destination)
            if file["id"] == self.root_id or file.get("mimeType") == FOLDER:
                raise DriveError(
                    "Only files can be moved through chat; root/folder moves are disabled"
                )
            if folder.get("mimeType") != FOLDER:
                raise DriveError("Destination must be a project folder")
            return public_file(
                service.files()
                .update(
                    fileId=file["id"],
                    addParents=folder["id"],
                    removeParents=",".join(file.get("parents", [])),
                    fields=FIELDS,
                    supportsAllDrives=True,
                )
                .execute(num_retries=0)
            )

    def upload(self, path: Path, filename: str, folder_id: str, file_id: str, sha256: str) -> dict:
        with self.session() as service:
            folder = self.scoped_get(service, folder_id)
            if folder.get("mimeType") != FOLDER:
                raise DriveError("Upload destination is not a folder")
            # Caller persists this preallocated ID BEFORE uploading. Replays use the
            # same ID, so a lost HTTP response cannot silently create another file.
            try:
                existing = self.scoped_get(service, file_id)
            except HttpError as error:
                if error.resp.status != 404:
                    raise
            else:
                if existing.get("sha256Checksum") != sha256:
                    raise DriveError("Existing file content differs; refusing to overwrite")
                return public_file(existing, f"{folder.get('name', 'Project')}/{existing['name']}")
            with path.open("rb") as source:
                media = MediaIoBaseUpload(
                    source,
                    mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    resumable=True,
                    chunksize=1024 * 1024,
                )
                request = service.files().create(
                    body={"id": checked_id(file_id), "name": filename, "parents": [folder_id]},
                    media_body=media,
                    fields=FIELDS,
                    supportsAllDrives=True,
                )
                result = None
                while result is None:
                    _, result = request.next_chunk(num_retries=0)
                return public_file(result, f"{folder.get('name', 'Project')}/{filename}")
