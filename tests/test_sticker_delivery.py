from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_chat import FakeProvider
from test_scheduler_and_features import test_env as test_env

from app.main import create_app
from app.project_tools import ProjectTools
from app.workspace import current_actor, current_request


def saved(env, visibility="shared"):
    payload = b"WEBPVP8 " + b"\x04\x00\x00\x00" + b"test"
    data = b"RIFF" + len(payload).to_bytes(4, "little") + payload
    result = env["cmd"].stickers.save_sticker(
        env["owner"], data, "Celebrate", usage_guidance="Celebrate progress", visibility=visibility
    )
    return result["id"], data


def test_sticker_outbox_is_idempotent_and_rechecks_permissions(test_env):
    manager = test_env["cmd"].stickers
    sticker_id, data = saved(test_env)
    token = current_request.set("same-user-message")
    try:
        first = manager.queue_send(test_env["owner"], sticker_id)
        assert first == manager.queue_send(test_env["owner"], sticker_id)
    finally:
        current_request.reset(token)
    key = first["delivery_id"]
    assert manager.delivery_bytes(key) == data
    assert test_env["cmd"].schedule.notification_allowed(key)
    with test_env["db"].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM outbound_stickers").fetchone()[0] == 1
        c.execute("INSERT INTO capability_rules VALUES ('stickers','*','deny','[]','now')")
    assert not test_env["cmd"].schedule.notification_allowed(key)
    with pytest.raises(PermissionError):
        manager.delivery_bytes(key)


def test_private_sticker_and_changed_files_are_not_sent(test_env):
    manager = test_env["cmd"].stickers
    sticker_id, _ = saved(test_env, "personal")
    with pytest.raises(PermissionError):
        manager.queue_send(test_env["owner"], sticker_id)
    owner = test_env["owner"]
    dm = test_env["ws"].actor(owner.user, owner.phone + "@s.whatsapp.net", "dm")
    delivery = manager.queue_send(dm, sticker_id)
    manager.manage_sticker(dm, "delete", sticker_id)
    with pytest.raises(PermissionError):
        manager.delivery_bytes(delivery["delivery_id"])
    sticker_id, _ = saved(test_env)
    record = manager.get_sticker(sticker_id)
    (manager.storage_dir / (record["sha256"] + ".webp")).write_bytes(b"changed")
    with pytest.raises(ValueError):
        manager.queue_send(owner, sticker_id)


def test_media_endpoint_is_authenticated_and_returns_binary(test_env):
    app = create_app(test_env["settings"], FakeProvider())
    with TestClient(app) as client:
        sticker_id, data = saved(test_env)
        key = test_env["cmd"].stickers.queue_send(test_env["owner"], sticker_id)["delivery_id"]
        url = "/internal/whatsapp/notifications/" + key + "/media"
        assert client.get(url).status_code == 401
        headers = {"Authorization": "Bearer bridge-test-token"}
        result = client.get(url, headers=headers)
        assert result.status_code == 200 and result.content == data
        assert result.headers["content-type"] == "image/webp"
        notifications = client.get("/internal/whatsapp/notifications", headers=headers).json()
        assert notifications["notifications"][0]["media_kind"] == "sticker"
        client.post(url.removesuffix("/media"), headers=headers, json={"state": "sent"})
        assert client.get(url, headers=headers).status_code == 403


def test_model_sticker_tool_is_connected(test_env):
    import asyncio

    tools = ProjectTools(None, None, test_env["cmd"])
    manager = test_env["cmd"].stickers
    manager.queue_send = Mock(return_value={"status": "pending", "media_type": "sticker"})
    actor_token = current_actor.set(test_env["owner"])
    try:
        result = asyncio.run(tools.execute("sticker_send", {"sticker_id": "saved-id"}))
        assert result["media_type"] == "sticker"
        manager.queue_send.assert_called_once_with(test_env["owner"], "saved-id")
    finally:
        current_actor.reset(actor_token)


def test_sticker_reply_supports_authenticated_lid_dm(test_env):
    sticker_id, data = saved(test_env)
    actor = test_env["ws"].actor(test_env["owner"].user, "123456789012345@lid", "dm")
    manager = test_env["cmd"].stickers
    queued = manager.queue_send(actor, sticker_id)
    assert manager.delivery_bytes(queued["delivery_id"]) == data
