import hashlib
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.db import Database
from app.drive import FOLDER, DriveClient, DriveError
from app.ingestion import Ingestion, destination_from_caption, safe_filename


class TreeDrive(DriveClient):
    def __init__(self, tmp_path):
        super().__init__("root", tmp_path / "token.json")
        self.nodes = {
            "root": {"id": "root", "name": "Project", "mimeType": FOLDER},
            "folder": {"id": "folder", "name": "Control", "mimeType": FOLDER, "parents": ["root"]},
            "pdf": {
                "id": "pdf",
                "name": "Manual.pdf",
                "mimeType": "application/pdf",
                "parents": ["folder"],
                "modifiedTime": "2026-10-08",
                "sha256Checksum": "checksum",
            },
            "other": {"id": "other", "name": "Private", "mimeType": FOLDER},
            "secret": {"id": "secret", "name": "Secret.pdf", "parents": ["other"]},
            "shortcut": {
                "id": "shortcut",
                "name": "Shortcut",
                "mimeType": "application/vnd.google-apps.shortcut",
                "parents": ["root"],
                "shortcutDetails": {"targetId": "secret"},
            },
        }

    @contextmanager
    def session(self):
        yield None

    def raw_get(self, service, file_id):
        return self.nodes[file_id]

    def children(self, service, folder_id):
        yield from [file for file in self.nodes.values() if folder_id in file.get("parents", [])]


def test_search_recurses_project_only_and_never_follows_shortcuts(tmp_path):
    drive = TreeDrive(tmp_path)
    results = drive.search(".pdf")
    assert [file["id"] for file in results["files"]] == ["pdf"]
    assert results["files"][0]["path"] == "Project/Control/Manual.pdf"
    assert not results["incomplete_scan"]
    assert drive.search(sha256="checksum")["files"][0]["id"] == "pdf"
    assert [f["id"] for f in drive.search(folders_only=True)["files"]] == ["folder"]


def test_root_boundary_includes_ancestor_trash_and_moved_files(tmp_path):
    drive = TreeDrive(tmp_path)
    with pytest.raises(DriveError, match="outside"):
        drive.metadata("secret")
    drive.nodes["folder"]["trashed"] = True
    with pytest.raises(DriveError, match="trash"):
        drive.metadata("pdf")
    drive.nodes["folder"].pop("trashed")
    drive.nodes["folder"]["parents"] = ["other"]
    with pytest.raises(DriveError, match="outside"):
        drive.metadata("pdf")


def test_upload_replay_uses_existing_id_without_another_create(tmp_path):
    drive = TreeDrive(tmp_path)
    service = MagicMock()

    @contextmanager
    def session():
        yield service

    drive.session = session
    result = drive.upload(tmp_path / "not-needed", "Manual.pdf", "folder", "pdf", "checksum")
    assert result["id"] == "pdf"
    service.files.assert_not_called()
    with pytest.raises(DriveError, match="differs"):
        drive.upload(tmp_path / "not-needed", "Manual.pdf", "folder", "pdf", "different")


class UploadDrive:
    root_id = "root"
    ready = True

    def __init__(self):
        self.uploads = []
        self.allocations = 0
        self.failed = False
        self.files = {}

    def search(self, query="", folders_only=False, limit=20, sha256=None):
        return {"files": [], "incomplete_scan": False, "more_matches": False}

    def create_folder(self, name):
        return {"id": "inbox", "name": name}

    def list_folder(self, folder_id):
        return {"files": list(self.files.values()), "more_matches": False}

    def allocate_id(self):
        self.allocations += 1
        return f"file{self.allocations}"

    def upload(self, path, filename, folder_id, file_id, digest):
        self.uploads.append(file_id)
        if self.failed:
            raise DriveError("Simulated lost response")
        result = {
            "id": file_id,
            "name": filename,
            "sha256": digest,
            "drive_url": f"https://drive.google.com/file/d/{file_id}",
        }
        self.files[file_id] = result
        return result

    def metadata(self, file_id):
        return self.files[file_id]


@pytest.fixture
def ingestion(tmp_path):
    settings = Settings(
        _env_file=None, database_path=tmp_path / "db.sqlite3", incoming_path=tmp_path
    )
    db = Database(settings.database_path)
    db.initialize()
    user = db.enroll("Test", "test-token")
    drive = UploadDrive()
    service = Ingestion(settings, db, drive)
    path = tmp_path / "upload.bin"
    path.write_bytes(b"%PDF-test content")
    return service, drive, user, path


def test_identical_uploads_reuse_existing_id_and_hash(ingestion):
    service, drive, user, path = ingestion
    first = service.ingest(path, "test.pdf", user)
    second = service.ingest(path, "renamed.pdf", user)
    assert not first["duplicate"] and second["duplicate"]
    assert drive.uploads == ["file1"]
    assert second["index_status"] == "not_indexed"
    assert second["file"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_interrupted_upload_reuses_preallocated_id(ingestion):
    service, drive, user, path = ingestion
    drive.failed = True
    with pytest.raises(DriveError):
        service.ingest(path, "test.pdf", user)
    drive.failed = False
    service.ingest(path, "test.pdf", user)
    assert drive.allocations == 1
    assert drive.uploads == ["file1", "file1"]


def test_validation_and_missing_destination_do_not_upload(ingestion):
    service, drive, user, path = ingestion
    with pytest.raises(DriveError, match="ambiguous"):
        service.ingest(path, "test.pdf", user, "Missing")
    with pytest.raises(DriveError, match="active-content"):
        service.ingest(path, "malware.exe", user)
    path.write_text("not a pdf")
    with pytest.raises(DriveError, match="PDF signature"):
        service.ingest(path, "test.pdf", user)
    with pytest.raises(DriveError, match="token"):
        service.receive_bridge("../../secrets", "file.pdf", user, "")
    assert drive.uploads == []


def test_same_name_changed_content_is_separate_file(ingestion):
    service, drive, user, path = ingestion
    service.ingest(path, "test.pdf", user)
    path.write_bytes(b"%PDF-new revision")
    result = service.ingest(path, "test.pdf", user)
    assert result["file"]["id"] == "file2"
    assert result["file"]["name"] != "test.pdf"
    assert drive.files["file1"]["name"] == "test.pdf"


def test_explicit_destination_and_safe_filename():
    assert destination_from_caption("Upload this to the Control folder.") == "Control"
    assert destination_from_caption("/upload Electrical") == "Electrical"
    assert destination_from_caption("This belongs somewhere") == ""
    assert safe_filename("../../manual.pdf") == "manual.pdf"
