import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from test_chat import FakeProvider
from test_scheduler_and_features import test_env as test_env

from app.main import create_app
from app.scheduler import compute_next_run
from app.workspace import current_request


def propose(env, scheduler=None, actor=None, destination="123@g.us"):
    scheduler = scheduler or env["scheduler"]
    return scheduler.propose_task(
        actor or env["owner"],
        "reminder",
        destination,
        {"message": "Bring the report"},
        {"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "kind": "once"},
        "UTC",
    )


def test_confirmation_is_separate_and_consumed_once(test_env):
    scheduler = test_env["scheduler"]
    actor = test_env["owner"]
    token = current_request.set("first-message")
    try:
        p = propose(test_env)
        assert not scheduler.confirm_proposal(actor, p["proposal_id"])[0]
        current_request.set("second-message")
        assert scheduler.confirm_proposal(actor, p["proposal_id"])[0]
        assert not scheduler.confirm_proposal(actor, p["proposal_id"])[0]
    finally:
        current_request.reset(token)


def test_capability_denial_and_approval_enforced(test_env):
    ws = test_env["ws"]
    scheduler = test_env["cmd"].schedule.scheduler
    actor = ws.actor(ws.db.whatsapp_user("15555550200"), "123@g.us", "group")
    with ws.db.connect() as c:
        c.execute("UPDATE capability_rules SET effect='deny' WHERE capability='reminders'")
    with pytest.raises(PermissionError):
        propose(test_env, scheduler, actor)
    with ws.db.connect() as c:
        c.execute(
            "UPDATE capability_rules SET effect='approval',approvers=? "
            "WHERE capability='reminders'",
            (json.dumps([test_env["owner"].phone]),),
        )
    p = propose(test_env, scheduler, actor)
    ok, _, task = scheduler.confirm_proposal(actor, p["proposal_id"])
    assert ok
    assert scheduler.get_task(task["task_id"])["status"] == "awaiting_approval"
    request = test_env["cmd"].actions.pending(test_env["owner"])[0]
    result = test_env["cmd"].actions.resolve(test_env["owner"], request["id"], True)
    assert result["status"] == "completed"
    assert scheduler.get_task(task["task_id"])["status"] == "scheduled"
    assert scheduler.delivery_allowed(task["task_id"])
    with ws.db.connect() as c:
        c.execute("UPDATE capability_rules SET effect='deny' WHERE capability='reminders'")
    assert not scheduler.delivery_allowed(task["task_id"])


def test_reschedule_requires_new_confirmation_and_private_history_stays_private(test_env):
    scheduler = test_env["scheduler"]
    owner = test_env["owner"]
    p = propose(test_env, destination=owner.phone + "@s.whatsapp.net")
    _, _, task = scheduler.confirm_proposal(owner, p["proposal_id"])
    task_id = task["task_id"]
    assert scheduler.list_tasks(owner, include_completed=True) == []  # group output
    dm = test_env["ws"].actor(owner.user, owner.phone + "@s.whatsapp.net", "dm")
    assert len(scheduler.list_tasks(dm, include_completed=True)) == 1
    ok, preview = scheduler.manage_task(
        dm, "reschedule", task_id, new_due=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    assert ok and "confirm" in preview
    assert scheduler.get_task(task_id)["status"] == "paused"
    with pytest.raises(PermissionError):
        scheduler.execution_history(task_id)


def test_unsupported_handlers_never_claim_success(test_env):
    for kind in ["calendar", "report"]:
        with pytest.raises(ValueError):
            test_env["scheduler"].propose_task(
                test_env["owner"],
                kind,
                "123@g.us",
                {},
                {"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
                "UTC",
            )


def test_invalid_recurrence_rejected(test_env):
    for rule in [
        {"kind": "daily", "days": 0},
        {"kind": "weekly", "weekdays": []},
        {"kind": "unknown"},
    ]:
        with pytest.raises(ValueError):
            test_env["scheduler"].propose_task(
                test_env["owner"],
                "reminder",
                "123@g.us",
                {"message": "Test"},
                {"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), **rule},
                "UTC",
            )


def test_private_sticker_duplicate_cannot_be_published_by_other_member(test_env):
    manager = test_env["stickers"]
    owner = test_env["owner"]
    actor = test_env["ws"].actor(test_env["db"].whatsapp_user("15555550200"), "123@g.us", "group")
    payload = b"WEBPVP8 " + b"\x04\x00\x00\x00" + b"test"
    data = b"RIFF" + len(payload).to_bytes(4, "little") + payload
    saved = manager.save_sticker(owner, data, "Private", visibility="personal")
    with pytest.raises(PermissionError):
        manager.save_sticker(actor, data, "Stolen", visibility="shared")
    assert manager.get_sticker(saved["id"])["visibility"] == "personal"
    assert manager.list_stickers(owner) == []  # do not expose personal stickers in a group
    with pytest.raises(ValueError):
        manager.save_sticker(owner, b"not an image", "Bad")


def test_actual_bridge_audio_filename_and_reply_path_validation(test_env):
    settings = test_env["settings"].model_copy(
        update={"incoming_path": test_env["tmp_path"] / "incoming"}
    )
    settings.incoming_path.mkdir()
    token = "a" * 64
    path = settings.incoming_path / (token + ".bin")
    path.write_bytes(b"voice audio")
    provider = FakeProvider()
    provider._generate_with_rotation = AsyncMock()
    transcriber = AsyncMock()
    transcriber.transcribe.return_value = "What can you do?"
    app = create_app(settings, provider)
    body = {
        "event_id": "audio1",
        "sender_phone": "15555550100",
        "chat_id": "123@g.us",
        "channel": "group",
        "kind": "audio",
        "file_token": token,
    }
    with (
        TestClient(app) as client,
        patch("app.whatsapp.GeminiAudioTranscriber", return_value=transcriber),
    ):
        r = client.post(
            "/internal/whatsapp/messages",
            json=body,
            headers={"Authorization": "Bearer bridge-test-token"},
        )
        assert r.status_code == 200 and r.json()["text"] == "Test reply"
        transcriber.transcribe.assert_awaited_once()
        assert not path.exists()
        body.update(
            kind="text",
            text="Explain",
            reply_context={"chat_id": "123@g.us", "attachment_reference": "../secret"},
        )
        assert (
            client.post(
                "/internal/whatsapp/messages",
                json=body,
                headers={"Authorization": "Bearer bridge-test-token"},
            ).status_code
            == 422
        )
        body["reply_context"] = {"chat_id": "other@g.us"}
        assert (
            client.post(
                "/internal/whatsapp/messages",
                json=body,
                headers={"Authorization": "Bearer bridge-test-token"},
            ).status_code
            == 422
        )


def test_restart_marks_claimed_occurrence_uncertain_without_replaying(test_env):
    scheduler = test_env["scheduler"]
    p = propose(test_env)
    _, _, task = scheduler.confirm_proposal(test_env["owner"], p["proposal_id"])
    task_id = task["task_id"]
    record = scheduler.get_task(task_id)
    occurrence = task_id + ":" + record["next_run_at"]
    with test_env["db"].connect() as c:
        c.execute(
            "INSERT INTO task_executions(occurrence_id,task_id,scheduled_for,started_at,status) "
            "VALUES (?,?,?,?, 'running')",
            (occurrence, task_id, record["next_run_at"], record["created_at"]),
        )
    scheduler.recover()
    assert scheduler.get_task(task_id)["status"] == "uncertain"
    assert scheduler.tick() == []


def test_recurrence_survives_multiple_years_offline():
    due = datetime(2020, 1, 1, 9, tzinfo=UTC)
    clock = datetime(2026, 10, 9, 12, tzinfo=UTC)
    assert compute_next_run(due, {"kind": "daily"}, "UTC", clock) == datetime(
        2026, 10, 10, 9, tzinfo=UTC
    )
    weekly = compute_next_run(due, {"kind": "weekly", "weekdays": [0]}, "UTC", clock)
    assert weekly == datetime(2026, 10, 12, 9, tzinfo=UTC)


def test_sticker_grant_can_be_revoked(test_env):
    with test_env["db"].connect() as c:
        c.execute("INSERT INTO capability_rules VALUES ('stickers','*','deny','[]','now')")
    with pytest.raises(PermissionError):
        test_env["stickers"].list_stickers(test_env["owner"])


def test_legacy_reminders_obey_current_grants(test_env):
    schedule = test_env["cmd"].schedule
    due = datetime.now(UTC) + timedelta(minutes=1)
    key = schedule.create_reminder(
        test_env["owner"], "123@g.us", "Legacy reminder", due, {"kind": "once"}, "UTC"
    )
    schedule.tick(due)
    with test_env["db"].connect() as c:
        notification = c.execute(
            "SELECT id FROM notifications WHERE reminder_id=?", (key,)
        ).fetchone()[0]
    assert schedule.notification_allowed(notification)
    with test_env["db"].connect() as c:
        c.execute("UPDATE capability_rules SET effect='deny' WHERE capability='reminders'")
    assert not schedule.notification_allowed(notification)
