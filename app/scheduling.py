"""Timezone-aware manual availability, optional Calendar free/busy, durable reminders."""

import hashlib
import json
import threading
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from app.drive_auth import save_credentials
from app.workspace import now

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
]
DAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


def instant(value, timezone):
    dt = datetime.fromisoformat(value)
    zone = ZoneInfo(timezone)
    if dt.tzinfo is None:
        candidate = dt.replace(tzinfo=zone)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != dt:
            raise ValueError("That local time does not exist because of daylight saving")
        if (
            dt.replace(tzinfo=zone, fold=0).utcoffset()
            != dt.replace(tzinfo=zone, fold=1).utcoffset()
        ):
            raise ValueError("Ambiguous local time; supply an explicit UTC offset")
        dt = candidate
    return dt.astimezone(UTC)


def days(value):
    result = sorted({DAYS[d.strip().lower()[:3]] for d in value.split(",")})
    if not result:
        raise ValueError("Specify weekdays such as Mon,Wed")
    return result


class Calendar:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    @property
    def ready(self):
        return self.path.is_file()

    def busy(self, calendar_id, start, end):
        if not self.ready:
            return None
        with self.lock:
            service = None
            try:
                credentials = Credentials.from_authorized_user_file(str(self.path), SCOPES)
                if not credentials.valid:
                    credentials.refresh(Request())
                    save_credentials(credentials, self.path)
                service = build(
                    "calendar",
                    "v3",
                    cache_discovery=False,
                    http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=20)),
                )
                result = (
                    service.freebusy()
                    .query(
                        body={
                            "timeMin": start.isoformat(),
                            "timeMax": end.isoformat(),
                            "items": [{"id": calendar_id}],
                            "timeZone": "UTC",
                        }
                    )
                    .execute(num_retries=0)
                )
                item = result.get("calendars", {}).get(calendar_id)
                if not item or item.get("errors"):
                    return None
                return item.get("busy", [])
            except Exception:
                return None  # Missing permissions/outages mean unknown, never free.
            finally:
                if service:
                    service.close()

    def event(self, calendar_id, args, body, timezone):
        if not self.ready:
            raise ValueError(
                "Google Calendar is not authorized yet; complete the local Calendar setup"
            )
        with self.lock:
            service = None
            try:
                credentials = Credentials.from_authorized_user_file(str(self.path), SCOPES)
                if not credentials.valid:
                    credentials.refresh(Request())
                    save_credentials(credentials, self.path)
                service = build(
                    "calendar",
                    "v3",
                    cache_discovery=False,
                    http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=25)),
                )
                operation = args[0] if args else ""
                if operation in {"create", "reschedule", "list"}:
                    expected = 4 if operation == "reschedule" else 3
                    if len(args) != expected:
                        raise ValueError(
                            "Supply START END, with EVENT_ID before them for rescheduling"
                        )
                    start, end = instant(args[-2], timezone), instant(args[-1], timezone)
                    if not timedelta(0) < end - start <= timedelta(days=31):
                        raise ValueError(
                            "Event/query interval must be positive and at most 31 days"
                        )
                if operation == "list":
                    result = (
                        service.events()
                        .list(
                            calendarId=calendar_id,
                            timeMin=start.isoformat(),
                            timeMax=end.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=100,
                        )
                        .execute(num_retries=0)
                    )
                    return {
                        "events": [
                            {k: e.get(k) for k in ("id", "summary", "start", "end", "htmlLink")}
                            for e in result.get("items", [])
                        ],
                        "more_results": bool(result.get("nextPageToken")),
                    }
                if operation == "create":
                    if not body or not body[0]:
                        raise ValueError("Event creation needs a title after |")
                    payload = {
                        "summary": body[0][:200],
                        "description": body[1][:4000] if len(body) > 1 else "",
                        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
                        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
                        "reminders": {"useDefault": False},
                    }
                    # Stable ID makes retrying this exact event creation idempotent.
                    key = hashlib.sha256(
                        (calendar_id + json.dumps(payload, sort_keys=True)).encode()
                    ).hexdigest()[:32]
                    from googleapiclient.errors import HttpError

                    try:
                        result = (
                            service.events()
                            .get(calendarId=calendar_id, eventId=key)
                            .execute(num_retries=0)
                        )
                    except HttpError as error:
                        if error.resp.status != 404:
                            raise
                        result = (
                            service.events()
                            .insert(
                                calendarId=calendar_id,
                                body={**payload, "id": key},
                                sendUpdates="none",
                            )
                            .execute(num_retries=0)
                        )
                    return {
                        "status": "created or existing",
                        "id": result["id"],
                        "link": result.get("htmlLink"),
                    }
                if operation in {"reschedule", "cancel"}:
                    if operation == "cancel" and len(args) != 2:
                        raise ValueError("Supply one EVENT_ID")
                    event = (
                        service.events()
                        .get(calendarId=calendar_id, eventId=args[1])
                        .execute(num_retries=0)
                    )
                    if event.get("recurrence") or event.get("recurringEventId"):
                        raise ValueError(
                            "Recurring calendar series/instances must be edited in Google "
                            "Calendar for now"
                        )
                    if operation == "reschedule":
                        request = service.events().patch(
                            calendarId=calendar_id,
                            eventId=args[1],
                            body={
                                "start": {"dateTime": start.isoformat(), "timeZone": timezone},
                                "end": {"dateTime": end.isoformat(), "timeZone": timezone},
                            },
                            sendUpdates="none",
                        )
                    else:
                        request = service.events().delete(
                            calendarId=calendar_id, eventId=args[1], sendUpdates="none"
                        )
                    if event.get("etag"):
                        request.headers["If-Match"] = event["etag"]
                    result = request.execute(num_retries=0)
                    return {
                        "status": operation + " completed",
                        "event_id": args[1],
                        "link": result.get("htmlLink") if isinstance(result, dict) else None,
                    }
                raise ValueError("Use event create, list, reschedule or cancel")
            except ValueError:
                raise
            except Exception:
                raise ValueError(
                    "Calendar operation could not be confirmed. Check Calendar API "
                    "authorization and edit access; inspect Calendar before repeating a write."
                ) from None
            finally:
                if service:
                    service.close()


class Scheduling:
    def __init__(self, workspace, settings):
        self.ws, self.settings = workspace, settings
        self.calendar = Calendar(settings.calendar_token_path)

    def timezone(self, user_id):
        with self.ws.db.connect() as c:
            r = c.execute(
                "SELECT timezone FROM member_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        return r[0] if r else self.settings.default_timezone

    def add_busy(self, actor, start, end, weekdays="", label="Busy"):
        if not actor.trusted:
            raise PermissionError("Only approved members can store schedules")
        zone = self.timezone(actor.user.id)
        if weekdays:
            days(weekdays)
            if time.fromisoformat(start) >= time.fromisoformat(end):
                raise ValueError(
                    "Weekly slots must end later on the same day; split overnight slots"
                )
        else:
            a, b = instant(start, zone), instant(end, zone)
            if not timedelta(0) < b - a <= timedelta(days=31):
                raise ValueError("Busy interval must be positive and at most 31 days")
            start, end = a.isoformat(), b.isoformat()
        key = uuid4().hex[:12]
        with self.ws.db.connect() as c:
            c.execute(
                "INSERT INTO busy_slots VALUES (?,?,?,?,?,?,?)",
                (key, actor.user.id, start, end, weekdays, zone, label[:200]),
            )
        return key

    def availability(self, start, end, timezone="Africa/Cairo"):
        a, b = instant(start, timezone), instant(end, timezone)
        if not timedelta(0) < b - a <= timedelta(days=31):
            raise ValueError("Check a positive interval of at most 31 days")
        output = []
        for member in self.ws.db.whatsapp_members():
            if self.ws.role(member["phone"]) == "guest":
                continue
            with self.ws.db.connect() as c:
                slots = c.execute(
                    "SELECT * FROM busy_slots WHERE user_id=?", (member["id"],)
                ).fetchall()
                profile = c.execute(
                    "SELECT * FROM member_profiles WHERE user_id=?", (member["id"],)
                ).fetchone()
                link = c.execute(
                    "SELECT calendar_id FROM calendar_links WHERE user_id=?", (member["id"],)
                ).fetchone()
            conflicts = []
            for slot in slots:
                intervals = []
                if slot["weekdays"]:
                    zone = ZoneInfo(slot["timezone"])
                    day = a.astimezone(zone).date() - timedelta(days=1)
                    while day <= b.astimezone(zone).date():
                        if day.weekday() in days(slot["weekdays"]):
                            try:
                                intervals.append(
                                    (
                                        instant(f"{day}T{slot['start']}", slot["timezone"]),
                                        instant(f"{day}T{slot['end']}", slot["timezone"]),
                                    )
                                )
                            except ValueError:
                                # Conservatively flag the day rather than report a DST gap as free.
                                intervals.append((a, b))
                        day += timedelta(days=1)
                else:
                    intervals.append(
                        (datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"]))
                    )
                for x, y in intervals:
                    if x < b and y > a:
                        conflicts.append(
                            {
                                "start": max(x, a).isoformat(),
                                "end": min(y, b).isoformat(),
                                "source": "manual schedule",
                            }
                        )
            external = self.calendar.busy(link[0], a, b) if link else None
            if external:
                conflicts.extend({**item, "source": "Google Calendar"} for item in external)
            complete = bool(
                profile
                and profile["complete_until"]
                and b.astimezone(ZoneInfo(profile["timezone"])).date().isoformat()
                <= profile["complete_until"]
            )
            known = (external is not None) if link else complete
            output.append(
                {
                    "name": profile["name"] if profile else member["name"],
                    "status": "busy"
                    if conflicts
                    else "no recorded conflict"
                    if known
                    else "unknown",
                    "conflicts": conflicts,
                    "coverage": "Calendar only plus declared manual times"
                    if link
                    else "Declared manual schedule",
                }
            )
        shared_id = self.ws.get("shared_calendar", "")
        shared_busy = self.calendar.busy(shared_id, a, b) if shared_id else None
        return {
            "start": a.isoformat(),
            "end": b.isoformat(),
            "members": output,
            "shared_calendar": {
                "status": "unknown"
                if shared_busy is None
                else "busy"
                if shared_busy
                else "no recorded conflict",
                "conflicts": shared_busy or [],
            },
            "note": (
                "No recorded conflict is not guaranteed availability; "
                "unknown means incomplete data."
            ),
        }

    def create_reminder(self, actor, chat, body, due, recurrence, timezone):
        if not actor.admin:
            raise PermissionError("Admin only")
        with self.ws.db.connect() as c:
            if not c.execute(
                "SELECT 1 FROM registered_groups WHERE chat=? AND enabled=1", (chat,)
            ).fetchone():
                raise ValueError("Register the target group first with /admin group remember Name")
            if c.execute("SELECT COUNT(*) FROM reminders WHERE active=1").fetchone()[0] >= 100:
                raise ValueError("Limit of 100 active reminders reached")
            key = uuid4().hex[:12]
            c.execute(
                "INSERT INTO reminders VALUES (?,?,?,?,?,?,?,1)",
                (
                    key,
                    chat,
                    body[:2000],
                    due.isoformat(),
                    json.dumps(recurrence),
                    timezone,
                    actor.user.id,
                ),
            )
        return key

    @staticmethod
    def next_time(due, rule, zone):
        local = due.astimezone(ZoneInfo(zone))
        if rule["kind"] == "once":
            return None
        step = rule.get("days", 1) if rule["kind"] == "daily" else 1
        for offset in range(step, 370, step):
            candidate = local + timedelta(days=offset)
            if rule["kind"] == "weekly" and candidate.weekday() not in rule["weekdays"]:
                continue
            try:
                return instant(candidate.replace(tzinfo=None).isoformat(), zone)
            except ValueError:
                continue
        raise ValueError("No valid recurrence within one year")

    def tick(self, clock=None):
        clock = clock or datetime.now(UTC)
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for r in c.execute(
                "SELECT * FROM reminders WHERE active=1 AND next_due<=?", (clock.isoformat(),)
            ).fetchall():
                due = datetime.fromisoformat(r["next_due"])
                enabled = c.execute(
                    "SELECT 1 FROM registered_groups WHERE chat=? AND enabled=1", (r["chat"],)
                ).fetchone()
                state = "pending" if clock - due <= timedelta(minutes=15) and enabled else "missed"
                key = r["id"] + ":" + r["next_due"]
                c.execute(
                    "INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,?)",
                    (key, r["id"], r["chat"], r["body"], now(), state),
                )
                rule = json.loads(r["recurrence"])
                following = self.next_time(due, rule, r["timezone"])
                while following and following <= clock:
                    following = self.next_time(following, rule, r["timezone"])
                c.execute(
                    "UPDATE reminders SET next_due=?,active=? WHERE id=?",
                    ((following or due).isoformat(), int(following is not None), r["id"]),
                )

    def notification_allowed(self, key):
        if self.ws.get("paused", False):
            return False
        with self.ws.db.connect() as c:
            item = c.execute(
                "SELECT n.*,m.sender,m.kind,m.approval_id FROM notifications n "
                "JOIN outbound_metadata m ON m.notification_id=n.id WHERE n.id=? "
                "AND n.state IN ('pending','queued')",
                (key,),
            ).fetchone()
            if item:
                sender = c.execute(
                    "SELECT m.phone FROM whatsapp_members m JOIN users u ON m.user_id=u.id "
                    "WHERE u.id=? AND u.active=1",
                    (item["sender"],),
                ).fetchone()
                if not sender or not self.ws.can_access(sender[0]):
                    return False
                if item["kind"] == "approval":
                    request = c.execute(
                        "SELECT state,created FROM action_requests WHERE id=?",
                        (item["approval_id"],),
                    ).fetchone()
                    if (
                        not request
                        or request[0] != "pending"
                        or (
                            datetime.now(UTC) - datetime.fromisoformat(request[1])
                            > timedelta(hours=24)
                        )
                    ):
                        return False
                if item["chat"].endswith("@s.whatsapp.net"):
                    phone = item["chat"].split("@")[0]
                    return self.ws.role(phone) != "guest" and self.ws.can_access(phone)
                return item["chat"] in self.ws.policy()["groups"]
            return bool(
                c.execute(
                    "SELECT 1 FROM notifications n JOIN registered_groups g ON n.chat=g.chat "
                    "WHERE n.id=? AND n.state IN ('pending','queued') AND g.enabled=1",
                    (key,),
                ).fetchone()
            )
