"""Confirmed member-bound calendars and timetables, separate from chat and project memory."""

import base64
import binascii
import html
import json
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.workspace import current_image_hash, current_request, now


def calendar_id_from_link(value):
    value = html.unescape(str(value).strip())
    if not value or len(value) > 3000:
        raise ValueError("Supply a Google Calendar link or explicit calendar ID")
    if "://" in value:
        url = urlparse(value)
        if url.scheme != "https" or url.hostname not in {"calendar.google.com", "www.google.com"}:
            raise ValueError("Use a Google Calendar HTTPS link; ICS feeds are not supported")
        if not url.path.startswith("/calendar"):
            raise ValueError("Use a calendar link, not another Google page")
        query = parse_qs(url.query)
        if "eid" in query or "event" in url.path.lower():
            raise ValueError("That is an event link; send the whole calendar's sharing link")
        values = query.get("src") or query.get("cid") or []
        if len(values) != 1:
            raise ValueError("Send one calendar at a time, with a src or cid parameter")
        value = values[0]
        if "cid" in query and "@" not in value:
            try:
                value = base64.b64decode(
                    value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeError, binascii.Error):
                raise ValueError("Could not decode the calendar link") from None
    if not re.fullmatch(r"[A-Za-z0-9_.+%#=-]+@[A-Za-z0-9_.-]+", value) or len(value) > 500:
        raise ValueError("Use the explicit calendar ID under Google Calendar → Integrate calendar")
    return value


class ScheduleSources:
    def __init__(self, commands):
        self.cmd, self.ws = commands, commands.ws

    def members(self, references=None):
        members = [m for m in self.ws.db.whatsapp_members() if self.ws.role(m["phone"]) != "guest"]
        if references is None:
            return members
        if not isinstance(references, list) or not 1 <= len(references) <= 20:
            raise ValueError("Select one to twenty members by member ID or unambiguous name")
        selected = {}
        for reference in references:
            matches = [
                m
                for m in members
                if str(reference).casefold().lstrip("+")
                in {m["id"].casefold(), m["phone"], m["name"].casefold()}
            ]
            if len(matches) != 1:
                raise ValueError(
                    "A member is missing, inactive or ambiguous; use team_directory IDs"
                )
            selected[matches[0]["id"]] = matches[0]
        return list(selected.values())

    def permission(self, actor, member_ids, approved_by=""):
        actor = self.cmd.actions.fresh(actor)
        self.members(member_ids)
        caps = ["personal_schedule"]
        if any(uid != actor.user.id for uid in member_ids):
            caps.append("schedule_manage_others")
        approvers = None
        for capability in caps:
            required = self.cmd.actions.check(actor, capability)
            if required and actor.phone not in required:
                approvers = set(required) if approvers is None else approvers & set(required)
        if approvers is not None and not approvers:
            raise PermissionError(
                "These rules require different approvers; ask the owner to adjust them"
            )
        if approved_by and approvers and approved_by not in approvers:
            raise PermissionError("Current rules require a different approver")
        return caps[-1], sorted(approvers or [])

    def get(self, source_id):
        with self.ws.db.connect() as c:
            row = c.execute("SELECT * FROM schedule_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("Schedule source not found")
        return {**dict(row), "payload": json.loads(row["payload"])}

    def validate(self, actor, data):
        if not isinstance(data, dict) or len(json.dumps(data)) > 50000:
            raise ValueError("Schedule input is too large")
        operation = data.get("operation", "add")
        if operation not in {"add", "replace", "remove"}:
            raise ValueError("Use add, replace or remove")
        old = None
        if operation in {"replace", "remove"}:
            old = self.get(data.get("source_id", ""))
            if old["status"] != "active":
                raise ValueError("That source is no longer active")
            self.permission(actor, old["payload"]["member_ids"])
        if operation == "remove":
            return {"operation": operation, "source_id": old["id"], **old["payload"]}
        members = self.members(data.get("members") or [actor.user.id])
        ids = sorted(m["id"] for m in members)
        self.permission(actor, ids)
        kind = data.get("kind")
        if kind not in {"calendar", "timetable"}:
            raise ValueError("Choose calendar or timetable")
        zone = data.get("timezone") or self.cmd.schedule.timezone(actor.user.id)
        ZoneInfo(zone)
        default_start = (
            datetime.now(ZoneInfo(zone)).date().isoformat() if kind == "calendar" else ""
        )
        start = date.fromisoformat(data.get("valid_from") or default_start)
        end = date.fromisoformat(
            data.get("valid_until") or ("9999-12-30" if kind == "calendar" else "")
        )
        if end < start or (kind == "timetable" and (end - start).days > 366):
            raise ValueError(
                "Provide a validity period of at most one year, including semester end"
            )
        if end < datetime.now(ZoneInfo(zone)).date():
            raise ValueError("That timetable period has expired")
        label = str(data.get("label", "Schedule")).strip()
        if not 1 <= len(label) <= 150 or type(data.get("complete", False)) is not bool:
            raise ValueError("Supply a short label and an explicit true/false completeness flag")
        payload = {
            "operation": operation,
            "source_id": old["id"] if old else "",
            "kind": kind,
            "label": label,
            "member_ids": ids,
            "timezone": zone,
            "valid_from": start.isoformat(),
            "valid_until": end.isoformat(),
            "complete": data.get("complete", False),
            "image_hash": current_image_hash.get(),
        }
        if kind == "calendar":
            payload["calendar_id"] = calendar_id_from_link(data.get("calendar_link", ""))
            clock = datetime.now(UTC)
            status = self.cmd.schedule.calendar.busy(
                payload["calendar_id"], clock, clock + timedelta(days=1)
            )
            if status is None:
                raise ValueError(
                    "The connected Google account cannot read that calendar's availability. "
                    "Share it with the connected account, then try again. No source was saved."
                )
        else:
            if data.get("from_image") and not current_image_hash.get():
                raise ValueError(
                    "Quote or resend the timetable image so its extraction can be verified"
                )
            if data.get("uncertainties"):
                raise ValueError(
                    "Resolve the unreadable or ambiguous timetable fields before proposing"
                )
            slots = data.get("slots", [])
            if not isinstance(slots, list) or not 1 <= len(slots) <= 100:
                raise ValueError("Provide one to one hundred timetable intervals")
            normalized = []
            for slot in slots:
                weekdays = slot.get("weekdays", [])
                if not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):
                    raise ValueError("Weekdays use Monday=0 through Sunday=6")
                begin, finish = slot.get("start", ""), slot.get("end", "")
                if (
                    any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in (begin, finish))
                    or begin == finish
                ):
                    raise ValueError(
                        "Use distinct HH:MM start/end times; an earlier end means next day"
                    )
                every = slot.get("every_weeks", 1)
                if type(every) is not int or not 1 <= every <= 4:
                    raise ValueError("Week recurrence must be every 1 to 4 weeks")
                anchor = date.fromisoformat(slot.get("anchor_date", start.isoformat()))
                normalized.append(
                    {
                        "weekdays": sorted(set(weekdays)),
                        "start": begin,
                        "end": finish,
                        "label": str(slot.get("label", "Busy"))[:150],
                        "every_weeks": every,
                        "anchor_date": anchor.isoformat(),
                    }
                )
            excluded = data.get("excluded_dates", [])
            if not isinstance(excluded, list) or len(excluded) > 100:
                raise ValueError("At most 100 excluded dates are supported")
            payload["slots"] = sorted(
                normalized, key=lambda s: (s["weekdays"], s["start"], s["end"])
            )
            payload["excluded_dates"] = sorted(
                {date.fromisoformat(d).isoformat() for d in excluded}
            )
        return payload

    def propose(self, actor, data):
        payload = self.validate(actor, data)
        key = "schedule_" + uuid4().hex[:12]
        with self.ws.db.connect() as c:
            c.execute(
                "INSERT INTO schedule_source_proposals VALUES (?,?,?,?, 'pending',?,?,?)",
                (
                    key,
                    actor.user.id,
                    actor.chat,
                    json.dumps(payload),
                    now(),
                    (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                    current_request.get(),
                ),
            )
        people = self.members(payload["member_ids"])
        preview = {k: v for k, v in payload.items() if k not in {"image_hash", "member_ids"}}
        if preview["valid_until"] == "9999-12-30":
            preview["valid_until"] = "Until unlinked (no expiry)"
        return {
            "status": "needs_confirmation",
            "proposal_id": key,
            "preview": preview,
            "members": [m["name"] for m in people],
            "sharing": "Team can check busy times; labels stay private",
            "calendar_mapping": "Every busy event applies to all selected members"
            if payload["kind"] == "calendar"
            else None,
            "instruction": "Show every interval, members, timezone, validity and completeness; "
            "wait for a separate confirmation. Nothing has been saved yet.",
        }

    def _proposal(self, actor, proposal_id):
        with self.ws.db.connect() as c:
            row = c.execute(
                "SELECT * FROM schedule_source_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if not row or row["requester"] != actor.user.id or row["chat"] != actor.chat:
            raise PermissionError("Confirm your own proposal in its original conversation")
        return dict(row), json.loads(row["payload"])

    def confirm(self, actor, proposal_id="", approved_by=""):
        if not proposal_id:
            with self.ws.db.connect() as c:
                rows = c.execute(
                    "SELECT id FROM schedule_source_proposals WHERE requester=? "
                    "AND chat=? AND state='pending' AND expires>?",
                    (actor.user.id, actor.chat, now()),
                ).fetchall()
            if len(rows) != 1:
                raise ValueError(
                    "Choose a pending source proposal using schedule_sources; "
                    "there may be none or more than one"
                )
            proposal_id = rows[0][0]
        row, payload = self._proposal(actor, proposal_id)
        expected = "awaiting_approval" if approved_by else "pending"
        if row["state"] != expected or row["expires"] <= now():
            raise ValueError("That proposal was consumed or expired; request a new preview")
        if current_request.get() and row["request_id"] == current_request.get():
            raise ValueError("Show the preview and wait for a separate user confirmation")
        ids = payload["member_ids"]
        old = self.get(payload["source_id"]) if payload["source_id"] else None
        if old:
            if old["status"] != "active":
                raise ValueError("The original source changed; request a new preview")
            ids = sorted(set(ids + old["payload"]["member_ids"]))
        capability, approvers = self.permission(actor, ids, approved_by)
        if approvers and not approved_by:
            result = self.cmd.actions.request(
                actor, "@schedule_source " + proposal_id, capability, approvers
            )
            with self.ws.db.connect() as c:
                c.execute(
                    "UPDATE schedule_source_proposals SET state='awaiting_approval',expires=? "
                    "WHERE id=? AND state='pending'",
                    ((datetime.now(UTC) + timedelta(hours=24)).isoformat(), proposal_id),
                )
            return result
        key = "src_" + uuid4().hex[:12]
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "UPDATE schedule_source_proposals SET state='confirmed' WHERE id=? "
                "AND state=? AND expires>?",
                (proposal_id, expected, now()),
            ).rowcount:
                raise ValueError("Proposal was already consumed")
            if old:
                if not c.execute(
                    "UPDATE schedule_sources SET status=? WHERE id=? AND status='active'",
                    ("removed" if payload["operation"] == "remove" else "superseded", old["id"]),
                ).rowcount:
                    raise ValueError("Original source changed; request a new preview")
            if payload["operation"] == "remove":
                return {"status": "removed", "source_id": old["id"]}
            stored = {k: v for k, v in payload.items() if k not in {"operation", "source_id"}}
            for existing in c.execute(
                "SELECT id,payload FROM schedule_sources WHERE status='active'"
            ):
                if json.loads(existing["payload"]) == stored:
                    return {"status": "already_saved", "source_id": existing["id"]}
            c.execute(
                "INSERT INTO schedule_sources VALUES (?,?,?,?,?,'active',?)",
                (
                    key,
                    payload["kind"],
                    json.dumps(stored),
                    actor.user.id,
                    now(),
                    old["id"] if old else "",
                ),
            )
        self.ws.audit(actor, "schedule source " + key + " " + payload["operation"])
        return {
            "status": "saved",
            "source_id": key,
            "members": len(payload["member_ids"]),
            "live_calendar_reads": payload["kind"] == "calendar",
        }

    def list(self, actor):
        self.cmd.actions.fresh(actor)
        if not actor.trusted:
            raise PermissionError("Approved membership required")
        with self.ws.db.connect() as c:
            records = c.execute(
                "SELECT s.*,k.checked_at,k.status AS check_status FROM schedule_sources s "
                "LEFT JOIN schedule_source_checks k ON k.source_id=s.id "
                "WHERE s.status='active' ORDER BY s.created"
            ).fetchall()
            pending = [
                {
                    "proposal_id": r["id"],
                    "expires": r["expires"],
                    "label": json.loads(r["payload"])["label"],
                }
                for r in c.execute(
                    "SELECT * FROM schedule_source_proposals WHERE requester=? "
                    "AND chat=? AND state='pending' AND expires>?",
                    (actor.user.id, actor.chat, now()),
                )
            ]
        result = []
        for record in records:
            payload = json.loads(record["payload"])
            if actor.user.id not in payload["member_ids"] and not actor.admin:
                continue
            item = {
                "id": record["id"],
                "kind": record["kind"],
                "members": [m["name"] for m in self.members() if m["id"] in payload["member_ids"]],
                "valid_from": payload["valid_from"],
                "valid_until": payload["valid_until"],
                "complete": payload["complete"],
                "checked_at": record["checked_at"],
                "check_status": record["check_status"],
            }
            if actor.channel != "group":
                item["details"] = payload
            result.append(item)
        return {"sources": result, "pending_proposals": pending}
