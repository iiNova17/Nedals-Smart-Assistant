"""Live calendar availability plus confirmed semester timetables and bounded slot search."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.scheduling import instant
from app.workspace import now


def timetable_intervals(payload, start, end):
    zone = ZoneInfo(payload["timezone"])
    first, last = (
        date.fromisoformat(payload["valid_from"]),
        date.fromisoformat(payload["valid_until"]),
    )
    excluded = set(payload.get("excluded_dates", []))
    day = max(first, start.astimezone(zone).date() - timedelta(days=1))
    until = min(last, end.astimezone(zone).date())
    intervals = []
    uncertain = False
    while day <= until:
        if day.isoformat() not in excluded:
            for slot in payload["slots"]:
                anchor = date.fromisoformat(slot["anchor_date"])
                week = day - timedelta(days=day.weekday())
                anchor_week = anchor - timedelta(days=anchor.weekday())
                if day.weekday() not in slot["weekdays"] or (
                    ((week - anchor_week).days // 7) % slot["every_weeks"]
                ):
                    continue
                finish_day = day + timedelta(days=int(slot["end"] < slot["start"]))
                try:
                    a = instant(f"{day}T{slot['start']}", payload["timezone"])
                    b = instant(f"{finish_day}T{slot['end']}", payload["timezone"])
                except ValueError:
                    uncertain = True
                    continue
                if a < end and b > start:
                    intervals.append(
                        {
                            "start": max(a, start).isoformat(),
                            "end": min(b, end).isoformat(),
                            "source": "confirmed timetable",
                        }
                    )
        day += timedelta(days=1)
    return intervals, uncertain


class TeamAvailability:
    def __init__(self, schedule):
        self.schedule, self.ws = schedule, schedule.ws

    def check(self, start, end, timezone, member_ids=None, *, _buffer_minutes=0):
        a, b = instant(start, timezone), instant(end, timezone)
        if not timedelta(0) < b - a <= timedelta(days=31):
            raise ValueError("Use a positive query window of at most 31 days")
        # Include adjacent events before applying meeting buffers at window edges.
        a -= timedelta(minutes=_buffer_minutes)
        b += timedelta(minutes=_buffer_minutes)
        selected = {
            m["id"] for m in self.ws.db.whatsapp_members() if self.ws.role(m["phone"]) != "guest"
        }
        if member_ids is not None:
            if not member_ids or not set(member_ids) <= selected:
                raise ValueError("Resolve the requested members to active team member IDs")
            selected = set(member_ids)
        with self.ws.db.connect() as c:
            sources = [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in c.execute("SELECT * FROM schedule_sources WHERE status='active'")
            ]
            calendar_ids = {
                r["calendar_id"]
                for r in c.execute("SELECT * FROM calendar_links")
                if r["user_id"] in selected
            }
        sources = [s for s in sources if set(s["payload"]["member_ids"]) & selected]
        shared = self.ws.get("shared_calendar", "")
        if shared:
            calendar_ids.add(shared)
        calendar_ids.update(s["payload"]["calendar_id"] for s in sources if s["kind"] == "calendar")
        # One live batch per request: recurrence and exceptions are expanded by Google.
        external = self.schedule.calendar.busy_many(calendar_ids, a, b)
        result = self.schedule._legacy_availability(
            a.isoformat(), b.isoformat(), timezone, external
        )
        checked = now()
        for member in result["members"]:
            if member["member_id"] not in selected:
                continue
            # Legacy busy status alone does not certify complete coverage.
            with self.ws.db.connect() as c:
                profile = c.execute(
                    "SELECT complete_until,timezone FROM member_profiles WHERE user_id=?",
                    (member["member_id"],),
                ).fetchone()
                legacy = c.execute(
                    "SELECT calendar_id FROM calendar_links WHERE user_id=?", (member["member_id"],)
                ).fetchone()
            coverage = bool(legacy and external.get(legacy[0]) is not None) or bool(
                profile
                and profile[0]
                and (b - timedelta(microseconds=1))
                .astimezone(ZoneInfo(profile[1]))
                .date()
                .isoformat()
                <= profile[0]
            )
            missing = bool(legacy and external.get(legacy[0]) is None)
            used = []
            for source in sources:
                payload = source["payload"]
                if member["member_id"] not in payload["member_ids"]:
                    continue
                zone = ZoneInfo(payload["timezone"])
                first = (
                    datetime.fromisoformat(payload["valid_from"])
                    .replace(tzinfo=zone)
                    .astimezone(UTC)
                )
                next_day = date.fromisoformat(payload["valid_until"]) + timedelta(days=1)
                last = datetime.combine(next_day, datetime.min.time(), zone).astimezone(UTC)
                covers_window = first <= a and b <= last
                current = first < b and last > a
                state = "ok" if covers_window else "outside_validity"
                if not current:
                    used.append({"source_id": source["id"], "status": state})
                    with self.ws.db.connect() as c:
                        c.execute(
                            "INSERT OR REPLACE INTO schedule_source_checks VALUES (?,?,?)",
                            (source["id"], checked, state),
                        )
                    continue
                if source["kind"] == "calendar":
                    intervals = external.get(payload["calendar_id"])
                    if intervals is None:
                        missing = True
                        state = "unavailable"
                    else:
                        for interval in intervals:
                            x, y = (
                                instant(interval["start"], "UTC"),
                                instant(interval["end"], "UTC"),
                            )
                            if x < min(b, last) and y > max(a, first):
                                member["conflicts"].append(
                                    {
                                        "start": max(x, a, first).isoformat(),
                                        "end": min(y, b, last).isoformat(),
                                        "source": "subscribed Google Calendar",
                                    }
                                )
                else:
                    intervals, ambiguous = timetable_intervals(payload, a, b)
                    member["conflicts"].extend(intervals)
                    if ambiguous:
                        missing = True
                        state = "ambiguous_dst"
                if not covers_window:
                    missing = True
                if payload["complete"] and covers_window and state == "ok":
                    coverage = True
                used.append({"source_id": source["id"], "status": state})
                with self.ws.db.connect() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO schedule_source_checks VALUES (?,?,?)",
                        (source["id"], checked, state),
                    )
            member["coverage_complete"] = coverage and not missing
            member["sources"] = used
            member["status"] = (
                "busy"
                if member["conflicts"]
                else ("no recorded conflict" if member["coverage_complete"] else "unknown")
            )
        if member_ids is not None:
            available = {m["member_id"] for m in result["members"]}
            if not member_ids or not set(member_ids) <= available:
                raise ValueError("Resolve the requested members to active team member IDs")
            result["members"] = [m for m in result["members"] if m["member_id"] in member_ids]
        result["checked_at"] = checked
        result["timezone"] = timezone
        result["note"] = (
            "Calendar availability is checked live. No recorded conflict is not a guarantee; "
            "partial, expired or inaccessible schedules are explicitly unknown."
        )
        return result

    def suggest(
        self,
        start,
        end,
        timezone,
        duration_minutes=60,
        member_ids=None,
        day_start="09:00",
        day_end="21:00",
        weekdays=None,
        limit=10,
        buffer_minutes=0,
    ):
        from datetime import time

        if type(duration_minutes) is not int or not 5 <= duration_minutes <= 480:
            raise ValueError("Meeting duration must be 5 to 480 minutes")
        low, high = time.fromisoformat(day_start), time.fromisoformat(day_end)
        if low >= high:
            raise ValueError("Daily search hours must end later on the same day")
        weekdays = list(range(7)) if weekdays is None else weekdays
        if not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):
            raise ValueError("Weekdays use Monday=0 through Sunday=6")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Request one to twenty suggestions")
        if type(buffer_minutes) is not int or not 0 <= buffer_minutes <= 180:
            raise ValueError("Buffer must be zero to 180 minutes before and after busy intervals")
        data = self.check(start, end, timezone, member_ids, _buffer_minutes=buffer_minutes)
        if not data["members"]:
            raise ValueError("No active members selected")
        zone = ZoneInfo(timezone)
        a, b = instant(start, timezone), instant(end, timezone)
        duration = timedelta(minutes=duration_minutes)
        unknown = [m["name"] for m in data["members"] if not m["coverage_complete"]]
        shared_unknown = (
            data["shared_calendar"]["configured"] and data["shared_calendar"]["status"] == "unknown"
        )
        conflicts = [i for m in data["members"] for i in m["conflicts"]] + data["shared_calendar"][
            "conflicts"
        ]
        buffer = timedelta(minutes=buffer_minutes)
        intervals = [
            (instant(i["start"], "UTC") - buffer, instant(i["end"], "UTC") + buffer)
            for i in conflicts
        ]
        result = []
        day = a.astimezone(zone).date()
        while day <= b.astimezone(zone).date() and len(result) < limit:
            if day.weekday() in weekdays:
                local = datetime.combine(day, low)
                stop = datetime.combine(day, high)
                while local < stop and len(result) < limit:
                    try:
                        begin = instant(local.isoformat(), timezone)
                        finish = begin + duration
                        valid = (
                            begin >= max(a, datetime.now(UTC))
                            and finish <= b
                            and finish.astimezone(zone).date() == day
                            and finish.astimezone(zone).time().replace(tzinfo=None) <= high
                        )
                        if valid and not any(x < finish and y > begin for x, y in intervals):
                            result.append(
                                {
                                    "start": begin.astimezone(zone).isoformat(),
                                    "end": finish.astimezone(zone).isoformat(),
                                    "status": "tentative"
                                    if unknown or shared_unknown
                                    else "no_recorded_conflicts",
                                }
                            )
                            local += duration
                            continue
                    except ValueError:
                        pass  # Skip nonexistent/ambiguous wall-clock instants.
                    local += timedelta(minutes=15)
            day += timedelta(days=1)
        return {
            "slots": result,
            "timezone": timezone,
            "buffer_minutes": buffer_minutes,
            "checked_at": data["checked_at"],
            "unknown_members": unknown,
            "shared_calendar_unknown": shared_unknown,
            "members": [m["name"] for m in data["members"]],
            "note": "Tentative means a schedule is missing or incomplete. No meeting was created.",
        }
