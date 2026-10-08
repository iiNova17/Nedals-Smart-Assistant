import json

import pytest
from fastapi.testclient import TestClient
from test_chat import FakeProvider

from app.config import Settings
from app.main import create_app


@pytest.fixture
def pilot(tmp_path):
    config = tmp_path / "team.json"
    config.write_text(
        json.dumps(
            {
                "owner_phone": "15555550100",
                "group_name": "Test",
                "group_id": "123@g.us",
            }
        )
    )
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        team_config_path=config,
        bridge_token="test-bridge-token",
        daily_user_requests=10,
    )
    provider = FakeProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        yield client, app.state.db, provider


def incoming(client, **changes):
    body = dict(
        event_id="event1",
        sender_phone="15555550100",
        chat_id="123@g.us",
        channel="group",
        text="Hello",
    )
    body.update(changes)
    return client.post(
        "/internal/whatsapp/messages",
        json=body,
        headers={"Authorization": "Bearer test-bridge-token"},
    )


def test_bridge_auth_and_group_sender_checks(pilot):
    client, _, provider = pilot
    assert client.post("/internal/whatsapp/messages", json={}).status_code == 401
    assert incoming(client, sender_phone="15555550101").status_code == 403
    assert incoming(client, chat_id="other@g.us").status_code == 200
    assert len(provider.calls) == 1  # Owner can conversationally register a new group.
    assert incoming(client, role="owner").status_code == 422
    assert len(provider.calls) == 1


def test_owner_cannot_be_revoked_or_changed(pilot):
    _, db, _ = pilot
    owner = db.whatsapp_user("15555550100")
    assert db.ensure_owner("15555550100") == owner
    with pytest.raises(PermissionError):
        db.revoke(owner.id)
    with pytest.raises(RuntimeError):
        db.ensure_owner("15555550101")
    assert db.is_active(owner.id)


def test_duplicate_delivery_does_not_call_gemini_again(pilot):
    client, db, provider = pilot
    first = incoming(client)
    assert first.status_code == 200
    assert incoming(client).json() == first.json()
    assert len(provider.calls) == 1
    with db.connect() as sql:
        assert sql.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert sql.execute("SELECT SUM(requests) FROM usage").fetchone()[0] == 1


def test_group_dm_and_web_scopes_are_separate(pilot):
    client, db, provider = pilot
    incoming(client, text="Group-only fact")
    incoming(client, channel="dm", chat_id="12345@lid", text="Private-only fact")
    assert [m.text for m in provider.calls[-1]] == ["Private-only fact"]
    incoming(client, event_id="event2", text="Follow-up")
    assert provider.calls[-1][0].text.endswith("] Group-only fact")
    owner = db.whatsapp_user("15555550100")
    cid = db.whatsapp_conversation(owner, "group:123@g.us")
    assert not db.owns_conversation(owner.id, cid)
    assert not db.delete_conversation(owner.id, cid)


def test_document_status_is_honest_and_deduplicated(pilot):
    client, db, provider = pilot
    response = incoming(client, kind="document", text="")
    assert response.status_code == 200
    assert "not uploaded or indexed" in response.json()["text"]
    assert incoming(client, kind="document").json() == response.json()
    assert not provider.calls
    with db.connect() as sql:
        assert sql.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1


def test_regular_member_access_shared_group_separate_dm_and_revocation(pilot):
    client, db, provider = pilot
    phone = "15555550200"
    member = db.approve_whatsapp_member(phone, "Member")
    assert db.approve_whatsapp_member(phone, "Member") == member
    assert next(m for m in db.whatsapp_members() if m["phone"] == phone)["role"] == "member"
    incoming(client, text="Shared group fact")
    assert incoming(client, sender_phone=phone, event_id="member1").status_code == 200
    assert provider.calls[-1][0].text.endswith("] Shared group fact")
    incoming(client, channel="dm", text="Owner private fact")
    assert incoming(client, sender_phone=phone, channel="dm", text="Member DM").status_code == 200
    assert [m.text for m in provider.calls[-1]] == ["Member DM"]
    assert incoming(client, sender_phone=phone, chat_id="other@g.us").status_code == 403
    assert db.revoke(member.id)
    calls = len(provider.calls)
    assert incoming(client, sender_phone=phone, event_id="member1").status_code == 403
    assert incoming(client, sender_phone=phone, kind="document").status_code == 403
    assert len(provider.calls) == calls
    assert phone not in [m["phone"] for m in db.whatsapp_members()]


def test_member_approval_preserves_permanent_owner_role(pilot):
    _, db, _ = pilot
    owner = db.approve_whatsapp_member("15555550100", "Not a role change")
    assert next(m for m in db.whatsapp_members() if m["id"] == owner.id)["role"] == "owner"
    with pytest.raises(PermissionError):
        db.revoke(owner.id)


def test_members_list_requires_bridge_auth(pilot):
    client, db, _ = pilot
    db.approve_whatsapp_member("15555550200", "Member")
    assert client.get("/internal/whatsapp/members").status_code == 401
    response = client.get(
        "/internal/whatsapp/members", headers={"Authorization": "Bearer test-bridge-token"}
    )
    assert response.status_code == 200
    assert {m["role"] for m in response.json()["members"]} == {"owner", "member"}
