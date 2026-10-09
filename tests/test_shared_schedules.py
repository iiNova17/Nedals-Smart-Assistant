import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from test_scheduler_and_features import test_env as test_env

from app.project_tools import ProjectTools
from app.schedule_sources import ScheduleSources, calendar_id_from_link
from app.team_availability import TeamAvailability, timetable_intervals
from app.workspace import current_actor, current_image_hash, current_request


def monday():
    day = datetime.now(UTC).date() + timedelta(days=10)
    return day + timedelta(days=(-day.weekday()) % 7)


def payload(env, **changes):
    start = monday()
    return {
        "operation": "add",
        "kind": "timetable",
        "label": "Semester timetable",
        "members": [env["owner"].user.id],
        "timezone": "UTC",
        "valid_from": str(start),
        "valid_until": str(start + timedelta(days=60)),
        "complete": True,
        "slots": [
            {"weekdays": [0], "start": "09:00", "end": "11:00", "label": "PRIVATE COURSE NAME"}
        ],
        **changes,
    }


def save(env, **changes):
    service = env["cmd"].schedule_sources
    proposal = service.propose(env["owner"], payload(env, **changes))
    return service.confirm(env["owner"], proposal["proposal_id"])


def mock_calendar(env, monkeypatch, busy=None):
    calendar = env["cmd"].schedule.calendar
    monkeypatch.setattr(calendar, "busy", MagicMock(return_value=[]))
    monkeypatch.setattr(
        calendar,
        "busy_many",
        MagicMock(side_effect=lambda ids, a, b: {key: [] if busy is None else busy for key in ids}),
    )
    return calendar


def test_google_calendar_link_formats():
    cid = "lecture.group@group.calendar.google.com"
    encoded = base64.urlsafe_b64encode(cid.encode()).decode().rstrip("=")
    assert calendar_id_from_link(cid) == cid
    assert calendar_id_from_link("https://calendar.google.com/calendar/u/3?cid=" + encoded) == cid
    assert (
        calendar_id_from_link(
            "https://calendar.google.com/calendar/embed?src="
            + cid.replace("@", "%40")
            + "&ctz=Africa%2FCairo"
        )
        == cid
    )
    for invalid in [
        "https://evil.example/calendar?src=" + cid,
        "primary",
        "https://calendar.google.com/calendar/event?eid=secret",
        "https://calendar.google.com/calendar/embed?src=a@b&src=c@d",
    ]:
        with pytest.raises(ValueError):
            calendar_id_from_link(invalid)


def test_photo_import_is_confirmed_durable_and_private(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    service = test_env["cmd"].schedule_sources
    request = current_request.set("photo-message")
    image = current_image_hash.set("a" * 64)
    try:
        proposal = service.propose(test_env["owner"], payload(test_env, from_image=True))
        with pytest.raises(ValueError, match="separate"):
            service.confirm(test_env["owner"], proposal["proposal_id"])
        current_request.set("confirmation-message")
        saved = service.confirm(test_env["owner"], proposal["proposal_id"])
    finally:
        current_request.reset(request)
        current_image_hash.reset(image)
    with pytest.raises(ValueError):
        service.confirm(test_env["owner"], proposal["proposal_id"])
    restored = ScheduleSources(test_env["cmd"])
    assert restored.get(saved["source_id"])["payload"]["image_hash"] == "a" * 64
    info = test_env["cmd"].schedule.availability(f"{monday()}T10:00", f"{monday()}T10:30", "UTC")
    member = next(m for m in info["members"] if m["member_id"] == test_env["owner"].user.id)
    assert member["status"] == "busy" and member["coverage_complete"]
    assert "PRIVATE COURSE NAME" not in json.dumps(info)
    assert "PRIVATE COURSE NAME" not in json.dumps(restored.list(test_env["owner"]))


def test_live_subscription_is_bound_to_selected_members_and_updates(test_env, monkeypatch):
    calendar = mock_calendar(test_env, monkeypatch)
    recipient = test_env["db"].whatsapp_user("15555550200")
    save(
        test_env,
        kind="calendar",
        calendar_link="lectures@example.com",
        members=[recipient.id],
        slots=[],
    )
    engine = TeamAvailability(test_env["cmd"].schedule)
    first = engine.check(f"{monday()}T10:00", f"{monday()}T11:00", "UTC")
    assert (
        next(m for m in first["members"] if m["member_id"] == recipient.id)["status"]
        == "no recorded conflict"
    )
    assert (
        next(m for m in first["members"] if m["member_id"] == test_env["owner"].user.id)["status"]
        == "unknown"
    )
    calendar.busy_many.side_effect = lambda ids, a, b: {
        key: [{"start": a.isoformat(), "end": b.isoformat()}] for key in ids
    }
    second = engine.check(f"{monday()}T10:00", f"{monday()}T11:00", "UTC")
    assert next(m for m in second["members"] if m["member_id"] == recipient.id)["status"] == "busy"
    calendar.busy_many.side_effect = lambda ids, a, b: {key: None for key in ids}
    third = engine.check(f"{monday()}T10:00", f"{monday()}T11:00", "UTC")
    assert (
        next(m for m in third["members"] if m["member_id"] == recipient.id)["status"] == "unknown"
    )
    assert calendar.busy_many.call_count == 3


def test_suggestions_respect_all_members_bounds_and_partial_coverage(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    recipient = test_env["db"].whatsapp_user("15555550200")
    save(test_env)
    save(
        test_env,
        members=[recipient.id],
        slots=[{"weekdays": [0], "start": "11:00", "end": "12:00"}],
    )
    engine = TeamAvailability(test_env["cmd"].schedule)
    result = engine.suggest(
        f"{monday()}T08:00",
        f"{monday()}T17:00",
        "UTC",
        member_ids=[test_env["owner"].user.id, recipient.id],
        duration_minutes=60,
        day_start="09:00",
        day_end="14:00",
        limit=2,
    )
    assert [s["start"][11:16] for s in result["slots"]] == ["12:00", "13:00"]
    assert all(s["status"] == "no_recorded_conflicts" for s in result["slots"])
    assert not result["unknown_members"]
    all_team = engine.suggest(f"{monday()}T12:00", f"{monday()}T14:00", "UTC")
    assert all_team["unknown_members"]
    assert all(s["status"] == "tentative" for s in all_team["slots"])


def test_edits_keep_history_and_only_remove_the_target_source(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    service = test_env["cmd"].schedule_sources
    original = save(test_env)
    assert save(test_env)["status"] == "already_saved"
    replacement = save(
        test_env,
        operation="replace",
        source_id=original["source_id"],
        slots=[{"weekdays": [0], "start": "13:00", "end": "14:00"}],
    )
    assert service.get(original["source_id"])["status"] == "superseded"
    proposal = service.propose(
        test_env["owner"], {"operation": "remove", "source_id": replacement["source_id"]}
    )
    service.confirm(test_env["owner"], proposal["proposal_id"])
    assert service.get(replacement["source_id"])["status"] == "removed"
    assert service.list(test_env["owner"])["sources"] == []


def test_permission_approval_and_revocation_apply_to_source_import(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    service, cmd = test_env["cmd"].schedule_sources, test_env["cmd"]
    person = test_env["db"].whatsapp_user("15555550200")
    actor = test_env["ws"].actor(person, "123@g.us", "group")
    with pytest.raises(PermissionError):
        service.propose(actor, payload(test_env))
    with test_env["db"].connect() as c:
        c.execute(
            "INSERT INTO capability_rules VALUES ('personal_schedule',?,'approval',?,?)",
            (actor.phone, json.dumps([test_env["owner"].phone]), "test"),
        )
    proposal = service.propose(actor, payload(test_env, members=[person.id]))
    result = service.confirm(actor, proposal["proposal_id"])
    assert result["status"] == "awaiting_approval"
    approved = cmd.actions.resolve(test_env["owner"], result["request_id"], True)
    assert approved["status"] == "completed"
    second = service.propose(actor, payload(test_env, members=[person.id], label="Changed"))
    with test_env["db"].connect() as c:
        c.execute("UPDATE capability_rules SET effect='deny' WHERE subject=?", (actor.phone,))
    with pytest.raises(PermissionError):
        service.confirm(actor, second["proposal_id"])


def test_unavailable_calendar_and_uncertain_photo_never_saved(test_env, monkeypatch):
    calendar = mock_calendar(test_env, monkeypatch)
    service = test_env["cmd"].schedule_sources
    calendar.busy.return_value = None
    with pytest.raises(ValueError, match="cannot read"):
        service.propose(
            test_env["owner"], payload(test_env, kind="calendar", calendar_link="x@example.com")
        )
    with pytest.raises(ValueError, match="ambiguous"):
        service.propose(test_env["owner"], payload(test_env, uncertainties=["unreadable time"]))
    assert service.list(test_env["owner"])["sources"] == []


def test_alternate_week_overnight_holidays_and_expiry(test_env):
    data = payload(test_env)
    data["slots"] = [
        {
            "weekdays": [0],
            "start": "23:00",
            "end": "01:00",
            "every_weeks": 2,
            "anchor_date": str(monday()),
        }
    ]
    start = datetime.fromisoformat(str(monday()) + "T00:00+00:00")
    intervals, uncertain = timetable_intervals(data, start, start + timedelta(days=16))
    assert len(intervals) == 2 and not uncertain
    assert intervals[0]["end"][11:16] == "01:00"
    data["excluded_dates"] = [str(monday())]
    assert len(timetable_intervals(data, start, start + timedelta(days=16))[0]) == 1


def test_dm_reminder_tool_canonicalizes_lid_and_group_reminders_mention(test_env):
    tools = ProjectTools(None, None, test_env["cmd"])
    owner = test_env["owner"]
    actor = test_env["ws"].actor(owner.user, "123456789@lid", "dm")
    token = current_actor.set(actor)
    due = datetime.now(UTC) + timedelta(minutes=5)
    try:
        proposal = asyncio.run(
            tools.execute(
                "schedule_task_propose",
                {
                    "task_type": "reminder",
                    "destination": "dm",
                    "arguments": {"message": "Drink water"},
                    "schedule": {"at": due.isoformat()},
                },
            )
        )
    finally:
        current_actor.reset(token)
    scheduler = test_env["cmd"].schedule.scheduler
    assert "Private WhatsApp DM" in proposal["preview"]
    _, _, task = scheduler.confirm_proposal(actor, proposal["proposal_id"])
    assert scheduler.get_task(task["task_id"])["destination"] == owner.phone + "@s.whatsapp.net"
    group = scheduler.propose_task(
        owner, "reminder", "123@g.us", {"message": "Bring report"}, {"at": due.isoformat()}, "UTC"
    )
    scheduler.confirm_proposal(owner, group["proposal_id"])
    scheduler.tick(due + timedelta(seconds=1))
    with test_env["db"].connect() as c:
        rows = c.execute(
            "SELECT n.chat,n.body,m.jids FROM notifications n LEFT JOIN "
            "outbound_mentions m ON n.id=m.notification_id ORDER BY n.chat"
        ).fetchall()
    dm = next(r for r in rows if r["chat"].endswith("@s.whatsapp.net"))
    group = next(r for r in rows if r["chat"].endswith("@g.us"))
    assert "Drink water" in dm["body"] and dm["jids"] is None
    assert json.loads(group["jids"]) == [owner.phone + "@s.whatsapp.net"]
    assert "@" + owner.phone in group["body"]


def test_source_confirmation_without_id_and_wrong_member_or_expired(test_env):
    service = test_env["cmd"].schedule_sources
    proposal = service.propose(test_env["owner"], payload(test_env))
    person = test_env["db"].whatsapp_user("15555550200")
    actor = test_env["ws"].actor(person, "123@g.us", "group")
    with pytest.raises(PermissionError):
        service.confirm(actor, proposal["proposal_id"])
    assert service.confirm(test_env["owner"])["status"] == "saved"
    expired = service.propose(test_env["owner"], payload(test_env, label="Other"))
    with test_env["db"].connect() as c:
        c.execute(
            "UPDATE schedule_source_proposals SET expires='2000-01-01' WHERE id=?",
            (expired["proposal_id"],),
        )
    with pytest.raises(ValueError):
        service.confirm(test_env["owner"], expired["proposal_id"])


def test_calendar_api_batches_and_preserves_per_calendar_errors(test_env, monkeypatch):
    from app.scheduling import Calendar

    path = test_env["tmp_path"] / "token.json"
    path.write_text("{}")
    service = MagicMock()
    service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "a@example.com": {"busy": []},
            "b@example.com": {"errors": [{"reason": "notFound"}]},
        }
    }
    monkeypatch.setattr("app.scheduling.build", MagicMock(return_value=service))
    monkeypatch.setattr(
        "app.scheduling.Credentials.from_authorized_user_file",
        MagicMock(return_value=MagicMock(valid=True)),
    )
    monkeypatch.setattr("app.scheduling.AuthorizedHttp", MagicMock())
    clock = datetime.now(UTC)
    result = Calendar(path).busy_many(
        ["a@example.com", "b@example.com"], clock, clock + timedelta(days=1)
    )
    assert result == {"a@example.com": [], "b@example.com": None}
    assert len(service.freebusy.return_value.query.call_args.kwargs["body"]["items"]) == 2
    service.close.assert_called_once()


def test_partial_and_expired_sources_are_unknown_and_owner_mode_keeps_team(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    save(test_env, complete=False)
    engine = TeamAvailability(test_env["cmd"].schedule)
    result = engine.check(f"{monday()}T12:00", f"{monday()}T13:00", "UTC")
    assert result["members"][0]["coverage_complete"] is False
    test_env["ws"].set("mode", "owner")
    assert len(test_env["cmd"].schedule_sources.members()) == 3
    result = engine.check(
        f"{monday() + timedelta(days=70)}T12:00", f"{monday() + timedelta(days=70)}T13:00", "UTC"
    )
    assert all(m["status"] == "unknown" for m in result["members"])


def test_dst_gap_is_unknown_and_break_constraint_is_respected(test_env, monkeypatch):
    data = payload(
        test_env, timezone="America/New_York", valid_from="2027-03-01", valid_until="2027-03-31"
    )
    data["slots"] = [
        {
            "weekdays": [6],
            "start": "02:30",
            "end": "03:30",
            "every_weeks": 1,
            "anchor_date": "2027-03-01",
        }
    ]
    start = datetime(2027, 3, 14, tzinfo=UTC)
    assert timetable_intervals(data, start, start + timedelta(days=1))[1]
    mock_calendar(test_env, monkeypatch)
    save(test_env)
    engine = TeamAvailability(test_env["cmd"].schedule)
    result = engine.suggest(
        f"{monday()}T11:00",
        f"{monday()}T15:00",
        "UTC",
        member_ids=[test_env["owner"].user.id],
        buffer_minutes=30,
    )
    assert result["slots"][0]["start"][11:16] == "11:30"


def test_legacy_dm_delivery_and_calendar_default_validity(test_env, monkeypatch):
    mock_calendar(test_env, monkeypatch)
    service = test_env["cmd"].schedule_sources
    proposal = service.propose(
        test_env["owner"],
        {"operation": "add", "kind": "calendar", "calendar_link": "university@example.com"},
    )
    saved = service.confirm(test_env["owner"], proposal["proposal_id"])
    assert service.get(saved["source_id"])["payload"]["valid_until"] == "9999-12-30"
    scheduling = test_env["cmd"].schedule
    due = datetime.now(UTC) + timedelta(minutes=1)
    key = scheduling.create_reminder(
        test_env["owner"],
        test_env["owner"].phone + "@s.whatsapp.net",
        "Private",
        due,
        {"kind": "once"},
        "UTC",
    )
    scheduling.tick(due + timedelta(seconds=1))
    with test_env["db"].connect() as c:
        notification = c.execute(
            "SELECT * FROM notifications WHERE reminder_id=?", (key,)
        ).fetchone()
    assert notification["state"] == "pending"
    assert scheduling.notification_allowed(notification["id"])
