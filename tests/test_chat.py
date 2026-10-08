import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain import Completion, QuotaExceeded
from app.main import create_app


class FakeProvider:
    ready = True

    def __init__(self):
        self.calls = []
        self.closed = False
        self.error = False
        self.on_call = None

    async def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise RuntimeError("PRIVATE_PROVIDER_SECRET_AND_PROMPT")
        if self.on_call:
            self.on_call()
        return Completion("Test reply", "test-model", 12, 5)

    async def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "test.sqlite3",
        gemini_api_key="",
        context_turns=2,
        daily_user_requests=3,
    )
    provider = FakeProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        first = app.state.db.enroll("Alice", "alice-test-token")
        second = app.state.db.enroll("Bob", "bob-test-token")
        yield client, app, provider, first, second
    assert provider.closed


def auth(token="alice-test-token"):
    return {"Authorization": f"Bearer {token}"}


def conversation(client, token="alice-test-token"):
    response = client.post("/api/v1/conversations", headers=auth(token))
    assert response.status_code == 201
    return response.json()["id"]


def send(client, cid, text="Hello", token="alice-test-token"):
    return client.post(
        f"/api/v1/conversations/{cid}/messages", headers=auth(token), json={"text": text}
    )


def test_access_required_and_spoofed_identity_rejected(setup):
    client, _, provider, _, _ = setup
    assert client.get("/api/v1/knowledge").status_code == 401
    assert client.get("/api/v1/knowledge", headers=auth()).json()["disabled"]
    assert client.post("/api/v1/conversations").status_code == 401
    assert client.post("/api/v1/conversations", headers=auth("unknown")).status_code == 401
    cid = conversation(client)
    response = client.post(
        f"/api/v1/conversations/{cid}/messages",
        headers=auth(),
        json={"text": "Hello", "user_id": "someone-else", "role": "system"},
    )
    assert response.status_code == 422
    assert not provider.calls


def test_users_and_sessions_are_isolated(setup):
    client, _, provider, _, _ = setup
    cid = conversation(client)
    assert send(client, cid, "Shoulder is RS06 in this example").status_code == 200
    assert send(client, cid, "Its torque?").status_code == 200
    assert [m.text for m in provider.calls[1]] == [
        "Shoulder is RS06 in this example",
        "Test reply",
        "Its torque?",
    ]
    assert send(client, cid, token="bob-test-token").status_code == 404
    other = conversation(client, "bob-test-token")
    assert send(client, other, token="bob-test-token").status_code == 200
    assert len(provider.calls[-1]) == 1
    assert send(client, conversation(client)).status_code == 200
    assert len(provider.calls[-1]) == 1


def test_context_is_bounded_and_request_limit_persists(setup):
    client, app, provider, user, _ = setup
    cid = conversation(client)
    for text in ("One", "Two", "Three"):
        assert send(client, cid, text).status_code == 200
    history = app.state.db.history(cid, 20, 10000)
    assert [m.text for m in history if m.role == "user"] == ["Two", "Three"]
    reopened = Database(app.state.db.path)
    assert reopened.authenticate("alice-test-token").id == user.id
    assert len(reopened.history(cid, 20, 10000)) == 4
    assert send(client, cid).status_code == 429
    assert len(provider.calls) == 3


def test_provider_failure_sanitized_no_partial_turn(setup, caplog):
    client, app, provider, _, _ = setup
    cid = conversation(client)
    provider.error = True
    response = send(client, cid, "PRIVATE_USER_TEXT")
    assert response.status_code == 503
    assert "PRIVATE" not in response.text
    assert "PRIVATE" not in caplog.text
    assert app.state.db.history(cid, 20, 10000) == []


def test_revoked_users_cannot_access(setup):
    client, app, provider, user, _ = setup
    cid = conversation(client)
    app.state.db.revoke(user.id)
    assert send(client, cid).status_code == 401
    assert not provider.calls


def test_revocation_during_generation_blocks_delivery(setup):
    client, app, provider, user, _ = setup
    cid = conversation(client)
    provider.on_call = lambda: app.state.db.revoke(user.id)
    assert send(client, cid).status_code == 401
    assert app.state.db.history(cid, 20, 10000) == []


def test_retention_and_delete_ownership(setup):
    client, app, _, _, _ = setup
    cid = conversation(client)
    assert send(client, cid).status_code == 200
    with app.state.db.connect() as db:
        db.execute(
            "UPDATE turns SET created_at = ?",
            ((datetime.now(UTC) - timedelta(days=8)).isoformat(),),
        )
    assert send(client, cid).status_code == 200
    assert len(app.state.db.history(cid, 20, 10000)) == 2
    url = f"/api/v1/conversations/{cid}"
    assert client.delete(url, headers=auth("bob-test-token")).status_code == 404
    assert client.delete(url, headers=auth()).status_code == 204
    assert send(client, cid).status_code == 404


@pytest.mark.parametrize("text", ["", "   ", "a" * 8001])
def test_invalid_input_never_reaches_provider(setup, text):
    client, _, provider, _, _ = setup
    assert send(client, conversation(client), text).status_code == 422
    assert not provider.calls


def test_liveness_without_credentials(tmp_path):
    app = create_app(Settings(_env_file=None, database_path=tmp_path / "db", gemini_api_key=""))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        app.state.db.enroll("Alice", "alice-test-token")
        assert send(client, conversation(client)).status_code == 503


def test_team_budget_and_hash_only_storage(tmp_path):
    db = Database(tmp_path / "db")
    db.initialize()
    alice = db.enroll("Alice", "alice-secret")
    bob = db.enroll("Bob", "bob-secret")
    db.reserve_request(alice.id, 1, 10)
    with pytest.raises(QuotaExceeded):
        db.reserve_request(bob.id, 1, 10)
    with db.connect() as connection:
        assert (
            connection.execute("SELECT token_hash FROM users WHERE id = ?", (alice.id,)).fetchone()[
                0
            ]
            != "alice-secret"
        )


def test_busy_request_does_not_consume_budget(setup):
    client, app, provider, _, _ = setup
    cid = conversation(client)
    asyncio.run(app.state.assistant.lock.acquire())
    try:
        assert send(client, cid).status_code == 429
        assert client.delete(f"/api/v1/conversations/{cid}", headers=auth()).status_code == 409
        assert not provider.calls
    finally:
        app.state.assistant.lock.release()
    assert send(client, cid).status_code == 200


def test_context_character_budget_preserves_complete_turns(setup):
    client, app, _, _, _ = setup
    cid = conversation(client)
    send(client, cid, "a" * 100)
    assert app.state.db.history(cid, 2, 10) == []
    assert len(app.state.db.history(cid, 2, 200)) == 2
