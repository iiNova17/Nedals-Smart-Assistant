import json

from fastapi.testclient import TestClient
from test_chat import FakeProvider
from test_drive import UploadDrive

from app.config import Settings
from app.main import create_app


def test_web_upload_requires_auth_and_cleans_temporary_files(tmp_path, monkeypatch):
    team = tmp_path / "team.json"
    team.write_text(
        json.dumps({"owner_phone": "15555550100", "group_name": "Test", "drive_folder_id": "root"})
    )
    drive = UploadDrive()
    monkeypatch.setattr("app.main.DriveClient", lambda *args: drive)
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        incoming_path=tmp_path / "incoming",
        team_config_path=team,
        max_upload_bytes=1024,
    )
    app = create_app(settings, FakeProvider())
    with TestClient(app) as client:
        app.state.db.enroll("Test", "test-token")
        url = "/api/v1/drive/files?filename=manual.pdf"
        assert client.post(url, content=b"%PDF-test").status_code == 401
        headers = {"Authorization": "Bearer test-token", "Content-Type": "application/pdf"}
        first = client.post(url, content=b"%PDF-test", headers=headers)
        assert first.status_code == 201
        assert not first.json()["duplicate"]
        repeated = client.post(url, content=b"%PDF-test", headers=headers)
        assert repeated.json()["duplicate"]
        assert drive.uploads == ["file1"]
        assert client.post(url, content=b"X" * 1025, headers=headers).status_code == 413
        assert list(settings.incoming_path.iterdir()) == []
