"""Bounded temporary files -> canonical Drive files, with persistent deduplication."""

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain import User
from app.drive import DriveClient, DriveError


def safe_filename(value: str) -> str:
    value = value.replace("\\", "/").split("/")[-1]
    value = "".join(c for c in value if c.isprintable() and c not in '<>:"|?*').strip(" .")
    if not value or len(value) > 180:
        raise DriveError("Filename is empty or too long")
    if Path(value).suffix.lower() in {
        ".exe",
        ".dll",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".js",
        ".vbs",
        ".scr",
        ".html",
        ".htm",
    }:
        raise DriveError("Executable or active-content file types are not accepted")
    return value


def destination_from_caption(text: str) -> str:
    """Explicit, bounded destination syntax; unrecognized captions go to Inbox."""
    match = re.fullmatch(r"\s*/upload\s+(.{1,150}?)\s*", text, flags=re.I)
    if not match:
        match = re.fullmatch(
            r"\s*upload (?:this|it) to (?:the )?(.{1,150}?)(?: folder)?[.!]?\s*", text, flags=re.I
        )
    return match[1].strip() if match else ""


class Ingestion:
    def __init__(self, settings: Settings, db: Database, drive: DriveClient, knowledge=None):
        self.settings, self.db, self.drive = settings, db, drive
        self.knowledge = knowledge
        self.lock = threading.Lock()

    def ingest(self, path: Path, filename: str, user: User, destination: str = "") -> dict:
        with self.lock:
            filename = safe_filename(filename)
            if not self.db.is_active(user.id):
                raise DriveError("Team access has been revoked")
            size = path.stat().st_size
            if not 0 < size <= self.settings.max_upload_bytes:
                raise DriveError("File must be nonempty and no larger than 25 MiB")
            with path.open("rb") as file:
                signature = file.read(5)
                if filename.lower().endswith(".pdf") and signature != b"%PDF-":
                    raise DriveError("The attachment does not have a PDF signature")
                file.seek(0)
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            with self.db.connect() as db:
                row = db.execute(
                    "SELECT * FROM documents WHERE root_id=? AND sha256=?",
                    (self.drive.root_id, digest),
                ).fetchone()
            if row and row["status"] == "uploaded":
                # Revalidate access; don't reuse links to moved/trashed/out-of-root files.
                metadata = self.drive.metadata(row["drive_file_id"])
                if metadata.get("sha256") != digest:
                    raise DriveError(
                        "The previously uploaded Drive file has changed; "
                        "its stored duplicate mapping needs reconciliation"
                    )
                return self.result(metadata, True)
            if not row:
                matches = self.drive.search(sha256=digest, limit=1)
                if matches["files"]:
                    metadata = self.drive.metadata(matches["files"][0]["id"])
                    self.record(digest, metadata["id"], filename, "", user.id, "uploaded", metadata)
                    return self.result(metadata, True)
                if matches["incomplete_scan"]:
                    raise DriveError(
                        "Project is too large for a complete duplicate check in this pilot"
                    )
                if destination:
                    folders = self.drive.search(destination, folders_only=True, limit=25)
                    candidates = [
                        f
                        for f in folders["files"]
                        if f["name"].casefold() == destination.casefold()
                    ]
                    if (
                        len(candidates) != 1
                        or folders["incomplete_scan"]
                        or folders["more_matches"]
                    ):
                        raise DriveError(
                            "Destination folder is missing or ambiguous. Resend with "
                            "'/upload Exact folder name', or omit it to use Inbox"
                        )
                    folder = candidates[0]
                else:
                    folder = self.drive.create_folder("Inbox")
                contents = self.drive.list_folder(folder["id"])
                if contents["more_matches"] or any(
                    f["name"] == filename for f in contents["files"]
                ):
                    name = Path(filename)
                    filename = f"{name.stem[:140]} ({digest[:10]}){name.suffix}"
                file_id = self.drive.allocate_id()
                self.record(digest, file_id, filename, folder["id"], user.id, "pending", {})
            else:
                file_id, filename = row["drive_file_id"], row["filename"]
                folder = {"id": row["destination_id"]}
            metadata = self.drive.upload(path, filename, folder["id"], file_id, digest)
            self.record(digest, file_id, filename, folder["id"], user.id, "uploaded", metadata)
            return self.result(metadata, False)

    def result(self, metadata, duplicate):
        status = "not_indexed"
        if self.knowledge:
            status = (
                self.knowledge.document_status(metadata["id"])
                if (
                    metadata.get("mime_type") == "application/pdf"
                    or metadata["name"].lower().endswith(".pdf")
                )
                else "unsupported"
            )
        return {"file": metadata, "duplicate": duplicate, "index_status": status}

    def record(self, digest, file_id, filename, folder_id, user_id, status, metadata):
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(root_id,sha256) DO UPDATE SET "
                "status=excluded.status,metadata=excluded.metadata",
                (
                    self.drive.root_id,
                    digest,
                    file_id,
                    filename,
                    folder_id,
                    user_id,
                    status,
                    json.dumps(metadata),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def receive_bridge(self, token: str, filename: str, user: User, caption: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise DriveError("Invalid attachment token")
        root = self.settings.incoming_path.resolve()
        path = (root / (token + ".bin")).resolve()
        if path.parent != root or not path.is_file():
            raise DriveError("Attachment is missing. Please resend it")
        result = self.ingest(path, filename, user, destination_from_caption(caption))
        file = result["file"]
        action = (
            "Already exists in Drive; reused the existing file"
            if result["duplicate"]
            else "Uploaded to Drive"
        )
        location = f"\nLocation: {file['path']}" if file.get("path") else ""
        index_text = {
            "ready": "This PDF is ready for document questions.",
            "queued": "PDF indexing is queued; ask for document status shortly.",
            "indexing": "PDF indexing is in progress. The file is uploaded but not searchable yet.",
            "failed": "PDF indexing failed. The file is safely in Drive; indexing can be retried.",
            "stale": "The PDF changed; its document index is being refreshed.",
            "unsupported": "Stored in Drive. Document questions currently support PDFs only.",
        }.get(result["index_status"], "Document indexing is not connected yet.")
        return f"{action}: {file['name']}{location}\n{file['drive_url']}\n\n{index_text}"
