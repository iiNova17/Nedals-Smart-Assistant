import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from test_chat import FakeProvider

from app.commands import Commands
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.scheduling import Scheduling, instant
from app.workspace import Workspace


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db",
        context_path=tmp_path / "context",
        team_config_path=tmp_path / "team.json",
        calendar_token_path=tmp_path / "calendar.json",
        bridge_token="bridge-test",
    )
    settings.team_config_path.write_text(
        json.dumps({"owner_phone": "15555550100", "group_name": "Test", "group_id": "123@g.us"})
    )
    db = Database(settings.database_path)
    db.initialize()
    owner = db.ensure_owner("15555550100", "Nedal")
    member = db.approve_whatsapp_member("15555550200", "Member")
    ws = Workspace(db)
    cmd = Commands(ws, settings)
    with db.connect() as c:
        c.execute("INSERT INTO registered_groups VALUES ('123@g.us','Test',1)")
    a = ws.actor(owner, "123@g.us", "group")
    m = ws.actor(member, "123@g.us", "group")
    cmd.context.write(
        "identity", {"name": "Nedal's Smart Assistant", "creator": "Nedal", "purpose": "Help PLUME"}
    )
    cmd.context.write("project", {"name": "PLUME", "objective": "Gas inspection robot"})
    return settings, db, ws, cmd, a, m


def test_introductions_do_not_need_pdf_evidence(env):
    _, _, _, cmd, owner, _ = env
    assert cmd.dispatch(owner, "Please introduce yourself") is None
    assert cmd.dispatch(owner, "What can you do?") is None
    assert cmd.dispatch(owner, "Remember that this is a test") is None


def test_modes_block_precedence_and_owner_protection(env):
    _, db, ws, cmd, owner, member = env
    assert ws.can_access(member.phone, "123@g.us", "group")
    assert not ws.can_access("15555550300")
    assert "permission grant" in cmd.dispatch(member, "/admin mode public")
    request = json.loads(cmd.execute(owner, "/admin mode public"))
    cmd.actions.resolve(owner, request["request_id"], True)
    guest = ws.authorize("15555550300", "other@g.us", "group")
    assert guest and not guest.trusted
    assert ws.role(guest.phone) == "guest"
    cmd.execute(owner, "/admin block +15555550300")
    assert not ws.can_access(guest.phone)
    cmd.execute(owner, "/admin mode whitelist")
    assert not ws.can_access(guest.phone)
    cmd.execute(owner, "/admin mode owner")
    assert not ws.can_access(member.phone) and ws.can_access(owner.phone)
    assert "permanent owner" in cmd.dispatch(owner, "/admin block +15555550100")
    assert db.is_active(owner.user.id)


def test_project_role_is_not_admin_and_only_owner_can_promote(env):
    _, _, ws, cmd, owner, member = env
    cmd.execute(owner, "/admin member +15555550200 | A | Team lead | Everything")
    assert ws.role(member.phone) == "member"
    request = json.loads(cmd.execute(owner, "/admin promote +15555550200"))
    cmd.actions.resolve(owner, request["request_id"], True)
    promoted = ws.actor(member.user, "123@g.us", "group")
    assert promoted.admin
    assert "permanent owner" in cmd.dispatch(promoted, "/admin demote +15555550100")


def test_public_visitor_cannot_save_or_query_private_schedules(env):
    _, _, ws, cmd, owner, _ = env
    request = json.loads(cmd.execute(owner, "/admin mode public"))
    cmd.actions.resolve(owner, request["request_id"], True)
    guest = ws.authorize("15555550300", "guest@lid", "dm")
    assert "approved team members" in cmd.dispatch(guest, "/remember Motor | Test")
    assert "approved team" in cmd.dispatch(guest, "/availability 2026-11-01T10:00 2026-11-01T11:00")


def test_memory_revision_authorization_and_command_dedup(env):
    _, db, ws, cmd, owner, member = env
    first = cmd.dispatch(member, "/remember Motor | Selected X", receipt="one")
    assert cmd.dispatch(member, "/remember Motor | Selected X", receipt="one") == first
    assert len(ws.memories()) == 1
    key = ws.memories()[0]["id"]
    cmd.execute(owner, f"/replace {key} | Motor | Selected Y; changed for payload")
    assert ws.memories()[0]["supersedes"] == key
    assert ws.memories(status="superseded")[0]["body"] == "Selected X"
    with db.connect() as c:
        c.execute("INSERT INTO command_receipts VALUES ('crash','Unconfirmed','now')")
    assert cmd.dispatch(owner, "/remember Other | No", receipt="crash") == "Unconfirmed"
    assert len(ws.memories()) == 1


def test_proposal_confirmation_bound_to_requester_and_conversation(env):
    _, _, ws, cmd, owner, member = env
    proposal = cmd.propose(owner, "/admin mode public")
    key = proposal["confirmation"]
    assert "belongs" in cmd.dispatch(member, key)
    assert ws.get("mode", "whitelist") == "whitelist"
    request = json.loads(cmd.execute(owner, key))
    cmd.actions.resolve(owner, request["request_id"], True)
    assert ws.get("mode") == "public"
    assert "used" in cmd.dispatch(owner, key)


def test_editable_project_status_and_history(env):
    _, db, ws, cmd, owner, _ = env
    cmd.execute(owner, "/admin work-package Navigation | awaiting planning | Preliminary")
    cmd.execute(owner, "/admin work-package Navigation | active | Approved scope documented")
    assert ws.project_status()[0]["status"] == "active"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM work_package_history").fetchone()[0] == 2


def test_weekly_busy_unknown_and_completion(env):
    _, _, _, cmd, owner, member = env
    cmd.execute(member, "/busy weekly Mon 09:00 11:00 | Class")
    result = cmd.schedule.availability("2026-11-02T10:00", "2026-11-02T10:30")
    statuses = {r["name"]: r["status"] for r in result["members"]}
    assert statuses["Member"] == "busy" and statuses["Nedal"] == "unknown"
    assert "Class" not in json.dumps(result)
    cmd.execute(member, "/schedule complete 2026-11-30")
    result = cmd.schedule.availability("2026-11-02T11:00", "2026-11-02T12:00")
    assert (
        next(r for r in result["members"] if r["name"] == "Member")["status"]
        == "no recorded conflict"
    )
    cmd.execute(member, "/calendar shared@example.com")
    cmd.schedule.calendar.busy = MagicMock(return_value=None)
    result = cmd.schedule.availability("2026-11-02T11:00", "2026-11-02T12:00")
    assert next(r for r in result["members"] if r["name"] == "Member")["status"] == "unknown"


def test_dst_and_offset_validation():
    with pytest.raises(ValueError, match="does not exist"):
        instant("2026-03-08T02:30", "America/New_York")
    with pytest.raises(ValueError, match="Ambiguous"):
        instant("2026-11-01T01:30", "America/New_York")
    assert instant("2026-11-01T01:30-04:00", "America/New_York").hour == 5


def test_reminder_tick_dedup_cancellation_and_offline_miss(env):
    settings, db, ws, cmd, owner, _ = env
    schedule = Scheduling(ws, settings)
    clock = datetime(2026, 11, 1, 10, tzinfo=UTC)
    key = schedule.create_reminder(
        owner, owner.chat, "Meeting", clock, {"kind": "daily", "days": 1}, "Africa/Cairo"
    )
    schedule.tick(clock)
    schedule.tick(clock)
    with db.connect() as c:
        rows = c.execute("SELECT * FROM notifications").fetchall()
        assert len(rows) == 1
    assert schedule.notification_allowed(rows[0]["id"])
    cmd.execute(owner, "/admin cancel " + key)
    assert not schedule.notification_allowed(rows[0]["id"])
    key = schedule.create_reminder(
        owner, owner.chat, "Old", clock, {"kind": "once"}, "Africa/Cairo"
    )
    schedule.tick(clock + timedelta(hours=2))
    with db.connect() as c:
        assert (
            c.execute("SELECT state FROM notifications WHERE reminder_id=?", (key,)).fetchone()[0]
            == "missed"
        )


def test_local_browser_session_no_token_and_cross_origin_denied(env):
    settings, _, _, _, _, _ = env
    app = create_app(settings, FakeProvider())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post("/api/v1/browser-session").status_code == 403
        assert (
            client.post(
                "/api/v1/browser-session", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        headers = {"Origin": "http://127.0.0.1:8000"}
        response = client.post("/api/v1/browser-session", headers=headers)
        assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/v1/me").json()["admin"]
        assert client.post("/api/v1/conversations").status_code == 403
        assert client.post("/api/v1/conversations", headers=headers).status_code == 201
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").headers["content-type"].startswith("text/javascript")
        assert client.get("/assets/style.css").headers["content-type"].startswith("text/css")
        assert client.get("/assets/unknown.js").status_code == 404


def test_context_file_paths_cannot_escape(env):
    _, _, _, cmd, owner, _ = env
    assert "Unknown context file" in cmd.dispatch(owner, "/admin context ../../secrets | {}")


def test_notification_api_auth_delivery_and_cancel(env):
    settings, db, ws, cmd, owner, _ = env
    app = create_app(settings, FakeProvider())
    due = datetime.now(UTC) + timedelta(days=1)
    key = cmd.schedule.create_reminder(
        owner, owner.chat, "Meeting", due, {"kind": "once"}, "Africa/Cairo"
    )
    cmd.schedule.tick(due)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer bridge-test"}
        assert client.get("/internal/whatsapp/notifications").status_code == 401
        item = client.get("/internal/whatsapp/notifications", headers=headers).json()[
            "notifications"
        ][0]
        url = "/internal/whatsapp/notifications/" + item["id"]
        assert client.get(url, headers=headers).json()["allowed"]
        assert client.post(url, headers=headers, json={"state": "queued"}).status_code == 200
        cmd.execute(owner, "/admin cancel " + key)
        assert not client.get(url, headers=headers).json()["allowed"]
        client.post(url, headers=headers, json={"state": "sent"})
        with db.connect() as c:
            assert (
                c.execute("SELECT state FROM notifications WHERE id=?", (item["id"],)).fetchone()[0]
                == "cancelled"
            )
