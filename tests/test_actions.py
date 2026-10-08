import json
from datetime import datetime
from unittest.mock import MagicMock

from test_workspace_features import env as env

from app.project_tools import ProjectTools
from app.workspace import current_actor, current_request


def test_member_actions_are_direct_and_idempotent(env):
    _, db, ws, cmd, owner, member = env
    cmd.schedule.calendar.event = MagicMock(return_value={"status": "created", "id": "test"})
    ws.set("shared_calendar", "test@example.com")
    request = current_request.set("incoming-event")
    try:
        payload = {
            "action": "event_create",
            "title": "Review",
            "start": "2026-11-01T15:00",
            "end": "2026-11-01T16:00",
        }
        first = cmd.actions.execute(member, payload)
        assert "created" in first["result"]
        assert first == cmd.actions.execute(member, payload)
        assert cmd.schedule.calendar.event.call_count == 1
    finally:
        current_request.reset(request)
    reply = cmd.actions.execute(member, {"action": "memory_save", "title": "Motor", "text": "RS06"})
    assert "Saved" in reply["result"]
    memory = ws.memories()[0]
    assert memory["author"] == member.user.id
    assert (
        "retired"
        in cmd.actions.execute(member, {"action": "memory_forget", "id": memory["id"]})["result"]
    )
    assert not ws.memories()


def test_approval_is_enforced_for_tools_and_legacy_commands(env):
    _, db, ws, cmd, owner, member = env
    ws.set("shared_calendar", "test@example.com")
    cmd.schedule.calendar.event = MagicMock(return_value={"status": "created", "id": "test"})
    rule = {
        "action": "permission_set",
        "capability": "calendar_write",
        "effect": "approval",
        "approvers": [owner.phone],
        "target": "*",
    }
    assert "error" in cmd.actions.execute(member, rule)
    assert cmd.actions.execute(owner, rule)["status"] == "updated"
    # The old slash path must not bypass a newly imposed rule.
    response = json.loads(
        cmd.execute(member, "/admin event create 2026-11-01T15:00 2026-11-01T16:00 | Review")
    )
    assert response["status"] == "awaiting_approval"
    assert not cmd.schedule.calendar.event.called
    key = response["request_id"]
    with db.connect() as c:
        n = c.execute(
            "SELECT * FROM notifications WHERE body LIKE ?", ("%" + key + "%",)
        ).fetchone()
    assert n["chat"] == owner.phone + "@s.whatsapp.net"
    assert cmd.schedule.notification_allowed(n["id"])
    assert "error" in cmd.actions.execute(
        member, {"action": "approval_resolve", "id": key, "decision": "approve"}
    )
    assert cmd.actions.resolve(owner, key, True)["status"] == "completed"
    assert cmd.schedule.calendar.event.call_count == 1
    assert not cmd.schedule.notification_allowed(n["id"])
    assert cmd.actions.resolve(owner, key, True)["status"] == "completed"
    assert cmd.schedule.calendar.event.call_count == 1


def test_changed_rule_and_revocation_prevent_old_approvals(env):
    _, _, ws, cmd, owner, member = env
    ws.set("shared_calendar", "test@example.com")
    cmd.schedule.calendar.event = MagicMock()
    cmd.actions.run(
        owner,
        {
            "action": "permission_set",
            "capability": "calendar_write",
            "effect": "approval",
            "approvers": [owner.phone],
        },
    )
    pending = json.loads(
        cmd.execute(member, "/admin event create 2026-11-01T15:00 2026-11-01T16:00 | Review")
    )
    cmd.actions.run(
        owner,
        {
            "action": "permission_set",
            "capability": "calendar_write",
            "effect": "deny",
            "target": member.phone,
        },
    )
    assert cmd.actions.resolve(owner, pending["request_id"], True)["status"] == "failed"
    assert not cmd.schedule.calendar.event.called


def test_scope_grant_does_not_grant_general_admin(env):
    _, _, ws, cmd, owner, member = env
    cmd.actions.run(
        owner,
        {
            "action": "permission_set",
            "capability": "members",
            "effect": "allow",
            "target": member.phone,
        },
    )
    result = cmd.actions.execute(
        member,
        {
            "action": "member_update",
            "operation": "whitelist",
            "target": "+15555550300",
            "title": "New member",
        },
    )
    assert "applied" in result["result"]
    assert not ws.actor(member.user).admin
    assert "error" in cmd.actions.execute(
        member, {"action": "settings_update", "operation": "mode", "target": "owner"}
    )
    assert "error" in cmd.actions.execute(
        member, {"action": "member_update", "operation": "promote", "target": owner.phone}
    )
    assert "error" in cmd.actions.execute(
        member, {"action": "member_update", "operation": "block", "target": owner.phone}
    )


def test_calendar_relative_week_and_member_read(env):
    _, _, ws, cmd, _, member = env
    ws.set("shared_calendar", "test@example.com")
    cmd.schedule.calendar.event = MagicMock(return_value={"events": []})
    response = cmd.actions.read_calendar(member, period="this_week")
    start, end = (
        datetime.fromisoformat(response["start"]),
        datetime.fromisoformat(response["end_exclusive"]),
    )
    assert start.weekday() == cmd.settings.week_start and (end - start).days == 7
    assert response["events"] == [] and response["source"] == "Google Calendar"
    assert cmd.schedule.calendar.event.call_args.args[0] == "test@example.com"


def test_notification_recipient_block_and_unknown_recipient(env):
    _, _, ws, cmd, owner, member = env
    result = cmd.actions.execute(
        owner, {"action": "message_send", "target": member.phone, "text": "Meeting update"}
    )
    assert result["status"] == "queued"
    assert cmd.schedule.notification_allowed(result["delivery_id"])
    ws.member_change(owner, member.phone, "block")
    assert not cmd.schedule.notification_allowed(result["delivery_id"])
    assert "error" in cmd.actions.execute(
        owner, {"action": "message_send", "target": "15555550400", "text": "Not approved"}
    )


def test_unknown_block_is_visible_to_bridge_and_unblock_can_enroll(env):
    _, _, ws, _, owner, _ = env
    ws.set("mode", "public")
    phone = "15555550999"
    ws.member_change(owner, phone, "block")
    assert next(m for m in ws.policy()["members"] if m["phone"] == phone)["blocked"]
    assert ws.authorize(phone, "test@lid", "dm") is None
    ws.member_change(owner, phone, "unblock")
    guest = ws.authorize(phone, "test@lid", "dm")
    assert guest and not guest.trusted


def test_prompt_is_conversational_and_calendar_has_dedicated_tool(env):
    _, _, _, cmd, owner, _ = env
    tools = ProjectTools(None, None, cmd)
    token = current_actor.set(owner)
    try:
        prompt = tools.instructions()
        names = {d.name for d in tools.declarations()[0].function_declarations}
        assert {"calendar_events", "assistant_action", "workspace_info"} <= names
        assert "propose_action" not in names
        assert "NEVER PDF questions" in prompt
        assert "do not tell people to type slash" in prompt
    finally:
        current_actor.reset(token)
