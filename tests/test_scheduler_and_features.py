import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_chat import FakeProvider

from app.audio import GeminiAudioTranscriber, process_audio_file
from app.commands import Commands
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.project_tools import ProjectTools
from app.scheduler import TaskScheduler, compute_next_run
from app.stickers import StickerManager
from app.workspace import Workspace, current_actor


@pytest.fixture
def test_env(tmp_path):
    stickers_dir = tmp_path / "stickers"
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "test.sqlite3",
        context_path=tmp_path / "context",
        team_config_path=tmp_path / "team.json",
        calendar_token_path=tmp_path / "calendar.json",
        bridge_token="bridge-test-token",
        stickers_path=stickers_dir,
        gemini_api_key="fake-key-1,fake-key-2",
        daily_user_requests=50,
    )
    settings.team_config_path.write_text(
        json.dumps(
            {
                "owner_phone": "15555550100",
                "group_name": "PLUME Main",
                "group_id": "123@g.us",
            }
        )
    )
    db = Database(settings.database_path)
    db.initialize()
    owner = db.ensure_owner("15555550100", "Nedal")
    hajar = db.approve_whatsapp_member("15555550200", "Hajar")
    unknown = db.approve_whatsapp_member("15555550300", "User")  # name unknown (default User)

    with db.connect() as c:
        c.execute("INSERT INTO registered_groups VALUES ('123@g.us', 'PLUME Main', 1)")
        c.execute("INSERT INTO registered_groups VALUES ('sidequests@g.us', 'Side Quests Team', 1)")

    ws = Workspace(db)
    cmd = Commands(ws, settings)
    cmd.context.write(
        "identity", {"name": "Nedal's Smart Assistant", "creator": "Nedal", "purpose": "Help PLUME"}
    )
    cmd.context.write("project", {"name": "PLUME", "objective": "Gas inspection robot"})

    owner_actor = ws.actor(owner, "123@g.us", "group")
    hajar_actor = ws.actor(hajar, "123@g.us", "group")
    unknown_actor = ws.actor(unknown, "123@g.us", "group")

    with db.connect() as c:
        c.execute(
            "INSERT INTO capability_rules VALUES ('reminders','*','allow','[]',?)",
            (datetime.now(UTC).isoformat(),),
        )
    scheduler = TaskScheduler(ws, settings)
    stickers = StickerManager(ws, stickers_dir)

    return {
        "settings": settings,
        "db": db,
        "ws": ws,
        "cmd": cmd,
        "owner": owner_actor,
        "hajar": hajar_actor,
        "unknown": unknown_actor,
        "scheduler": scheduler,
        "stickers": stickers,
        "tmp_path": tmp_path,
    }


def test_speaker_record_and_distinction(test_env):
    ws = test_env["ws"]
    owner = test_env["owner"]
    hajar = test_env["hajar"]
    unknown = test_env["unknown"]

    # 1. Distinguish creator from other participants
    assert ws.is_creator(owner) is True
    assert ws.is_creator(hajar) is False
    assert ws.is_creator(unknown) is False

    # 2. Distinguish known vs unknown name
    assert ws.is_name_known(owner) is True
    assert ws.is_name_known(hajar) is True
    assert ws.is_name_known(unknown) is False

    # 3. Speaker records
    owner_rec = ws.speaker_record(owner, "Africa/Cairo")
    assert owner_rec["is_creator"] is True
    assert owner_rec["name"] == "Nedal"
    assert owner_rec["role"] == "owner"

    hajar_rec = ws.speaker_record(hajar, "Africa/Cairo")
    assert hajar_rec["is_creator"] is False
    assert hajar_rec["name"] == "Hajar"
    assert hajar_rec["role"] == "member"

    unknown_rec = ws.speaker_record(unknown, "Africa/Cairo")
    assert unknown_rec["is_creator"] is False
    assert unknown_rec["name"] is None
    assert unknown_rec["name_known"] is False


def test_reply_envelope_author_isolation(test_env):
    """Quoting Nedal's message in a group must never grant Nedal's identity or privileges."""
    settings = test_env["settings"]
    provider = FakeProvider()
    app = create_app(settings, provider)

    with TestClient(app) as client:
        # Hajar replies to Nedal's message, asking for admin mode change
        # Even if she quotes Nedal, sender is Hajar (15555550200)
        body = {
            "event_id": "evt-1",
            "sender_phone": "15555550200",  # Hajar
            "chat_id": "123@g.us",
            "channel": "group",
            "text": "/admin mode public",  # An admin command
            "reply_context": {
                "message_id": "msg-nedal-1",
                "chat_id": "123@g.us",
                "author_identifier": "15555550100",  # Quoting Nedal!
                "resolved_author_name": "Nedal",
                "content_type": "text",
                "text_or_caption": "I am the owner Nedal",
                "has_attachment": False,
            },
        }
        res = client.post(
            "/internal/whatsapp/messages",
            json=body,
            headers={"Authorization": "Bearer bridge-test-token"},
        )
        assert res.status_code == 200
        reply_text = res.json().get("text", "")
        # Since Hajar is not the owner, it requires approval / permission grant
        assert "permission grant" in reply_text.lower() or "requires" in reply_text.lower()


def test_quoted_instructions_are_not_executed_as_commands(test_env):
    """Instructions inside quoted content must not be treated as executable user commands."""
    settings = test_env["settings"]
    provider = FakeProvider()
    app = create_app(settings, provider)

    with TestClient(app) as client:
        # User sends a normal greeting, quoting a command: /admin block +15555550200
        body = {
            "event_id": "evt-2",
            "sender_phone": "15555550100",  # Owner
            "chat_id": "123@g.us",
            "channel": "group",
            "text": "What do you think of this?",
            "reply_context": {
                "message_id": "msg-malicious-1",
                "chat_id": "123@g.us",
                "author_identifier": "15555550200",
                "resolved_author_name": "Hajar",
                "content_type": "text",
                "text_or_caption": "/admin block +15555550200",
                "has_attachment": False,
            },
        }
        res = client.post(
            "/internal/whatsapp/messages",
            json=body,
            headers={"Authorization": "Bearer bridge-test-token"},
        )
        assert res.status_code == 200
        # Hajar was NOT blocked!
        ws = test_env["ws"]
        assert ws.can_access("15555550200", "123@g.us", "group") is True


def test_scheduler_propose_confirm_and_expiration(test_env):
    scheduler = test_env["scheduler"]
    owner = test_env["owner"]
    hajar = test_env["hajar"]

    # 1. Propose a task
    run_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    proposal = scheduler.propose_task(
        actor=hajar,
        task_type="reminder",
        destination="123@g.us",
        validated_arguments={"message": "Bring the updated arm CAD."},
        schedule={"at": run_time, "kind": "once"},
        timezone_str="Africa/Cairo",
    )

    assert "proposal_id" in proposal
    assert "preview" in proposal
    assert "Please confirm" in proposal["preview"]
    assert "Bring the updated arm CAD" in proposal["preview"]
    proposal_id = proposal["proposal_id"]

    # 2. Confirmation by another user in another chat should fail to find it
    other_actor = test_env["ws"].actor(owner.user, "other_chat@g.us", "dm")
    ok, msg, _ = scheduler.confirm_proposal(other_actor, proposal_id=proposal_id)
    assert ok is False

    # 3. Confirmation by creator succeeds
    ok, msg, task_rec = scheduler.confirm_proposal(hajar, proposal_id=proposal_id)
    assert ok is True
    assert task_rec is not None
    assert task_rec["task_id"].startswith("task_")

    # 4. Confirming again fails because proposal is consumed
    ok_again, _, _ = scheduler.confirm_proposal(hajar, proposal_id=proposal_id)
    assert ok_again is False

    # 5. Propose another and test expiration
    stale_proposal = scheduler.propose_task(
        actor=hajar,
        task_type="reminder",
        destination="123@g.us",
        validated_arguments={"message": "Old reminder"},
        schedule={"at": run_time, "kind": "once"},
        timezone_str="Africa/Cairo",
    )
    # Manually expire in DB
    with test_env["db"].connect() as sql:
        sql.execute(
            "UPDATE task_proposals SET expires_at = ? WHERE proposal_id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
                stale_proposal["proposal_id"],
            ),
        )
    ok_stale, msg_stale, _ = scheduler.confirm_proposal(
        hajar, proposal_id=stale_proposal["proposal_id"]
    )
    assert ok_stale is False
    assert "expire" in msg_stale.lower() or "no pending" in msg_stale.lower()


def test_permission_revocation_blocks_execution(test_env):
    scheduler = test_env["scheduler"]
    hajar = test_env["hajar"]

    # Schedule a task to run 1 second in the future
    due_dt = datetime.now(UTC) + timedelta(seconds=1)
    run_time = due_dt.isoformat()
    prop = scheduler.propose_task(
        actor=hajar,
        task_type="reminder",
        destination="123@g.us",
        validated_arguments={"message": "Status update"},
        schedule={"at": run_time, "kind": "once"},
        timezone_str="Africa/Cairo",
    )
    ok, _, task_rec = scheduler.confirm_proposal(hajar, proposal_id=prop["proposal_id"])
    assert ok is True
    task_id = task_rec["task_id"]

    # Revoke Hajar's access before execution
    test_env["db"].revoke(hajar.user.id)

    # Tick the scheduler at due_dt + 2 seconds
    clock = due_dt + timedelta(seconds=2)
    scheduler.tick(clock=clock)

    # Check execution history
    history = scheduler.execution_history(task_id=task_id, actor=test_env["owner"])
    assert len(history) == 1
    rec = history[0]
    assert rec["status"] == "blocked"
    assert "revoked" in rec["result"].lower() or "permission" in rec["result"].lower()


def test_scheduler_missed_run_suppression(test_env):
    """Tasks overdue by > 15 minutes due to offline time are marked missed and suppressed."""
    scheduler = test_env["scheduler"]
    owner = test_env["owner"]

    # Propose a reminder that was scheduled 3 hours ago
    past_due = datetime.now(UTC) - timedelta(hours=3)
    prop = scheduler.propose_task(
        actor=owner,
        task_type="reminder",
        destination="123@g.us",
        validated_arguments={"message": "Missed inspection"},
        schedule={"at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(), "kind": "once"},
        timezone_str="Africa/Cairo",
    )
    ok, _, task_rec = scheduler.confirm_proposal(owner, proposal_id=prop["proposal_id"])
    assert ok is True
    task_id = task_rec["task_id"]

    # Manually backdate next_run_at to simulate downtime
    with test_env["db"].connect() as c:
        c.execute(
            "UPDATE scheduled_tasks SET next_run_at=? WHERE task_id=?",
            (past_due.isoformat(), task_id),
        )

    # Tick the scheduler now
    now_clock = datetime.now(UTC)
    scheduler.tick(clock=now_clock)

    history = scheduler.execution_history(task_id=task_id, actor=test_env["owner"])
    assert len(history) == 1
    assert history[0]["status"] == "missed"

    # Check task status in db
    tasks = scheduler.list_tasks(owner, include_completed=True)
    t = [x for x in tasks if x["task_id"] == task_id][0]
    assert t["status"] == "completed"  # Once task finished after missed run


def test_dst_wall_clock_recurrence():
    """Wall-clock daily recurrence at 09:00 preserves 09:00 local time across DST."""
    tz = "America/New_York"
    zone = ZoneInfo(tz)
    # October 31, 2026 at 09:00 EDT (UTC-4)
    dt_local = datetime(2026, 10, 31, 9, 0, tzinfo=zone)
    rule = {"kind": "daily", "days": 1}
    next_dt = compute_next_run(dt_local, rule, tz, clock=dt_local)
    assert next_dt is not None
    next_local = next_dt.astimezone(zone)
    # Next day wall clock hour should be exactly 9
    assert next_local.hour == 9
    assert next_local.minute == 0


def test_stickers_save_dedup_and_management(test_env):
    stickers = test_env["stickers"]
    owner = test_env["owner"]
    hajar = test_env["hajar"]

    # Fake sticker bytes
    payload = b"WEBPVP8 " + b"\x04\x00\x00\x00" + b"test"
    content = b"RIFF" + len(payload).to_bytes(4, "little") + payload

    # 1. Save sticker
    saved = stickers.save_sticker(
        actor=owner,
        file_path_or_bytes=content,
        name="PLUME Robot",
        description="PLUME Robot thumbs up",
        usage_guidance="Use when celebrating success",
        visibility="shared",
    )
    assert saved["id"].startswith("stk_")
    assert saved["sha256"] is not None

    # 2. Saving the exact same sticker payload (deduplication)
    duplicate = stickers.save_sticker(
        actor=hajar,
        file_path_or_bytes=content,
        name="PLUME Robot Updated",
        usage_guidance="Updated guidance",
        visibility="shared",
    )
    assert duplicate["id"] == saved["id"]
    assert duplicate["deduplicated"] is True

    # 3. Listing stickers
    owner_list = stickers.list_stickers(owner)
    assert len(owner_list) >= 1

    hajar_list = stickers.list_stickers(hajar)
    # Hajar can see shared stickers
    assert any(s["id"] == saved["id"] for s in hajar_list)

    # 4. Search by query
    found = stickers.list_stickers(hajar, query="robot")
    assert len(found) >= 1

    # 5. Delete sticker
    # Non-creator/non-admin cannot delete owner's sticker
    with pytest.raises(PermissionError):
        stickers.manage_sticker(hajar, "delete", saved["id"])

    # Owner can delete
    ok, msg = stickers.manage_sticker(owner, "delete", saved["id"])
    assert ok is True
    assert stickers.get_sticker(saved["id"]) is None


@pytest.mark.anyio
async def test_audio_transcription_and_temp_cleanup(tmp_path):
    # Create a dummy audio file
    audio_file = tmp_path / "voice_note.ogg"
    audio_file.write_bytes(b"dummy ogg audio data")

    # Mock Gemini audio transcriber
    transcriber = GeminiAudioTranscriber(provider=MagicMock())

    with patch.object(transcriber, "transcribe", new_callable=AsyncMock) as mock_tx:
        mock_tx.return_value = "السلام عليكم، هل سنلتقي اليوم في تمام السادسة؟"

        text = await process_audio_file(transcriber, audio_file)
        assert text == "السلام عليكم، هل سنلتقي اليوم في تمام السادسة؟"

        # Verify temporary file cleanup occurred
        assert not audio_file.exists()


@pytest.mark.anyio
async def test_tools_exposure_and_execution(test_env):
    cmd = test_env["cmd"]
    hajar = test_env["hajar"]

    tools = ProjectTools(drive=None, knowledge=None, commands=cmd)

    # Declarations include new tools
    decls = tools.declarations()
    all_func_names = [f.name for t in decls for f in t.function_declarations]
    assert "schedule_task_propose" in all_func_names
    assert "schedule_task_confirm" in all_func_names
    assert "scheduled_tasks_list" in all_func_names
    assert "sticker_save" in all_func_names
    assert "sticker_list" in all_func_names

    token = current_actor.set(hajar)
    try:
        # Propose task via tool
        run_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        res = await tools.execute(
            "schedule_task_propose",
            {
                "task_type": "reminder",
                "destination": "123@g.us",
                "arguments": {"message": "Update test report"},
                "schedule": {"at": run_at, "kind": "once"},
            },
        )
        assert "proposal_id" in res
        assert "preview" in res

        # Confirm via tool
        conf = await tools.execute(
            "schedule_task_confirm",
            {"proposal_id": res["proposal_id"]},
        )
        assert conf["success"] is True
        assert conf["data"]["task_id"].startswith("task_")
    finally:
        current_actor.reset(token)
