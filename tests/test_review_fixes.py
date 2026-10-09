import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors
from test_whatsapp import pilot as pilot

from app.config import Settings
from app.knowledge import Knowledge
from app.providers.gemini import GeminiProvider, _resolve_keys


def test_key_pool_is_secret_and_deduplicated():
    settings = Settings(
        _env_file=None,
        gemini_api_keys="secret-alpha,secret-beta,secret-alpha",
        gemini_api_key="secret-beta",
    )
    assert _resolve_keys(settings) == ["secret-alpha", "secret-beta"]
    assert "secret-alpha" not in repr(settings)
    assert "secret-beta" not in repr(settings)


def test_cooling_keys_are_not_retried_and_models_have_separate_cooldowns(monkeypatch):
    clients = []

    def build(**kwargs):
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(
            side_effect=errors.ClientError(429, {"error": {"message": "exhausted"}})
        )
        clients.append(client)
        return client

    monkeypatch.setattr("app.providers.gemini.genai.Client", build)
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_keys="alpha,beta"))
    # A previously exhausted key cannot trap the loop on the other key.
    provider._exhausted_until[0, "primary"] = datetime.now(UTC) + timedelta(hours=1)

    async def run():
        for _ in range(2):
            with pytest.raises(errors.APIError):
                await asyncio.wait_for(provider._generate_with_rotation(model="primary"), 1)
        clients[0].aio.models.generate_content.assert_not_awaited()
        clients[1].aio.models.generate_content.assert_awaited_once()
        clients[0].aio.models.generate_content.side_effect = None
        clients[0].aio.models.generate_content.return_value = "fallback answer"
        assert await provider._generate_with_rotation(model="fallback") == "fallback answer"

    asyncio.run(run())


def test_document_client_stays_pinned_to_its_credential(monkeypatch):
    builder = MagicMock()
    monkeypatch.setattr("app.knowledge.genai.Client", builder)
    knowledge = Knowledge(
        Settings(
            _env_file=None,
            gemini_api_key="existing-store-project",
            document_api_key="explicit-store-project",
        ),
        None,
        None,
        api_keys=["chat-project-one", "chat-project-two"],
    )
    assert builder.call_count == 1
    assert builder.call_args.kwargs["api_key"] == "explicit-store-project"
    knowledge.close()
    builder.reset_mock()
    Knowledge(
        Settings(_env_file=None, gemini_api_key="existing-store-project"),
        None,
        None,
        api_keys=["chat-project-one"],
    )
    assert builder.call_args.kwargs["api_key"] == "existing-store-project"


def test_group_discovery_preserves_disabled_state_and_requires_registration(pilot):
    client, db, _ = pilot
    headers = {"Authorization": "Bearer test-bridge-token"}
    with db.connect() as c:
        c.execute("UPDATE registered_groups SET enabled=0 WHERE chat='123@g.us'")
    body = {
        "groups": [
            {"chat": "123@g.us", "name": "Updated name"},
            {"chat": "456@g.us", "name": "Discovered group"},
        ]
    }
    assert client.post("/internal/whatsapp/groups/sync", json=body).status_code == 401
    r = client.post("/internal/whatsapp/groups/sync", json=body, headers=headers)
    assert r.json()["synced"] == 2
    with db.connect() as c:
        rows = c.execute("SELECT name,enabled FROM registered_groups ORDER BY chat").fetchall()
    assert [(r["name"], r["enabled"]) for r in rows] == [
        ("Updated name", 0),
        ("Discovered group", 0),
    ]
    r = client.post(
        "/internal/whatsapp/groups/sync",
        json={"groups": [{"chat": "15555550100@s.whatsapp.net", "name": "Invalid DM"}]},
        headers=headers,
    )
    assert r.status_code == 422
