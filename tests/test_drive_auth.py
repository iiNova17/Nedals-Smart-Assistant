import json
import logging
from unittest.mock import MagicMock

import pytest

from app import drive_auth
from app.config import Settings


@pytest.fixture
def oauth_setup(tmp_path, monkeypatch):
    team = tmp_path / "team.json"
    team.write_text(
        json.dumps(
            {
                "owner_phone": "15555550100",
                "group_name": "Test",
                "drive_folder_id": "folder123",
            }
        )
    )
    settings = Settings(_env_file=None, team_config_path=team)
    monkeypatch.setattr(drive_auth, "Settings", lambda: settings)
    previous_log_disable = logging.root.manager.disable
    yield tmp_path
    logging.disable(previous_log_disable)


def test_check_without_token_never_opens_browser(oauth_setup, monkeypatch, capsys):
    flow = MagicMock()
    monkeypatch.setattr(drive_auth, "InstalledAppFlow", flow)
    monkeypatch.setattr(
        "sys.argv", ["drive_auth", "--check", "--token", str(oauth_setup / "missing")]
    )
    assert drive_auth.main() == 1
    assert "not authorized" in capsys.readouterr().out
    flow.from_client_config.assert_not_called()


@pytest.mark.parametrize("writable,expected", [(True, 0), (False, 1)])
def test_saved_credentials_check_folder_without_modifying_it(
    oauth_setup, monkeypatch, writable, expected
):
    token = oauth_setup / "token.json"
    token.write_text("{}")
    credentials = MagicMock(valid=True, expired=False)
    monkeypatch.setattr(
        drive_auth.Credentials, "from_authorized_user_file", lambda *args: credentials
    )
    service = MagicMock()
    files = service.files.return_value
    files.get.return_value.execute.return_value = {
        "mimeType": "application/vnd.google-apps.folder",
        "capabilities": {"canAddChildren": writable},
    }
    files.list.return_value.execute.return_value = {"files": [{"id": "file1"}]}
    monkeypatch.setattr(drive_auth, "build", lambda *args, **kwargs: service)
    monkeypatch.setattr("sys.argv", ["drive_auth", "--check", "--token", str(token)])
    assert drive_auth.main() == expected
    assert files.get.call_args.kwargs["fileId"] == "folder123"
    files.create.assert_not_called()
    files.update.assert_not_called()
    service.close.assert_called_once()


def test_oauth_errors_do_not_print_secrets(oauth_setup, monkeypatch, capsys):
    token = oauth_setup / "token.json"
    token.write_text("{}")

    def fail(*args):
        raise ValueError("SECRET_REFRESH_TOKEN")

    monkeypatch.setattr(drive_auth.Credentials, "from_authorized_user_file", fail)
    monkeypatch.setattr("sys.argv", ["drive_auth", "--check", "--token", str(token)])
    assert drive_auth.main() == 1
    output = capsys.readouterr().out
    assert "ValueError" in output
    assert "SECRET_REFRESH_TOKEN" not in output
