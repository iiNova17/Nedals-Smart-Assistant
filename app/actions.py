"""Conversational actions with live scoped permissions and durable human approvals."""

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.scheduling import instant
from app.workspace import current_request, now

MEMBER_CAPABILITIES = {"memory", "personal_schedule", "calendar_write", "stickers"}
CAPABILITIES = MEMBER_CAPABILITIES | {
    "members",
    "settings",
    "groups",
    "reminders",
    "drive_write",
    "project_status",
    "context",
    "send_message",
    "schedule_manage_others",
}


class Actions:
    def __init__(self, commands):
        self.cmd, self.ws = commands, commands.ws

    def fresh(self, actor):
        if not self.ws.db.is_active(actor.user.id):
            raise PermissionError("Your account is inactive")
        fresh = self.ws.actor(actor.user, actor.chat, actor.channel)
        if fresh.phone and not self.ws.can_access(fresh.phone, fresh.chat, fresh.channel):
            raise PermissionError("Your access is currently disabled")
        return fresh

    def rules(self):
        with self.ws.db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM capability_rules")]

    def describe(self, command, actor):
        if command.startswith("@schedule_source "):
            _, payload = self.cmd.schedule_sources._proposal(actor, command.split()[1])
            return "Confirm these schedule source changes: " + json.dumps(
                payload, ensure_ascii=False
            )
        if command.startswith("@scheduled "):
            task = self.cmd.schedule.scheduler.get_task(command.split()[1])
            return "Approve scheduled task, including its stated recurrence: " + json.dumps(
                task, ensure_ascii=False
            )
        if command.startswith("@message "):
            payload = json.loads(command[9:])
            return f"Send a message to {payload['target']}:\n{payload['text']}"
        head, *body = [p.strip() for p in command.split("|")]
        args = head.split()
        if args[:2] == ["/admin", "event"] and len(args) >= 4:
            operation = args[2]
            if operation in {"create", "reschedule"}:
                zone = self.cmd.schedule.timezone(actor.user.id)
                start, end = [instant(v, zone).astimezone(ZoneInfo(zone)) for v in args[-2:]]
                title = body[0] if body else "event " + args[3]
                return (
                    f"{operation.title()} {title}: "
                    f"{start:%d %b %Y, %H:%M} to {end:%d %b %Y, %H:%M} ({zone})"
                )
            return "Cancel calendar event " + args[3]
        return command.removeprefix("/admin ").removeprefix("/").replace(" | ", " — ")

    def check(self, actor, capability):
        if not actor.trusted:
            raise PermissionError("Only approved team members can change shared records")
        with self.ws.db.connect() as c:
            rule = c.execute(
                "SELECT * FROM capability_rules WHERE capability=? AND subject IN (?, '*') "
                "ORDER BY CASE WHEN subject=? THEN 0 ELSE 1 END LIMIT 1",
                (capability, actor.phone, actor.phone),
            ).fetchone()
        if rule:
            if rule["effect"] == "deny":
                raise PermissionError("The current permission rule does not allow this action")
            if rule["effect"] == "approval":
                return json.loads(rule["approvers"])
            return []
        if actor.admin or capability in MEMBER_CAPABILITIES:
            return []
        raise PermissionError("This action requires a permission grant from the owner")

    def capability(self, text):
        head = text.split("|", 1)[0].split()
        if not head:
            return None
        if head[0] in {"/remember", "/replace", "/forget"}:
            return "memory"
        if head[0] in {"/busy", "/timezone", "/calendar"} or (
            head[0] == "/schedule" and len(head) > 1
        ):
            return "personal_schedule"
        if head[0] != "/admin" or len(head) < 2:
            return None
        action = head[1]
        if action == "event":
            return None if len(head) > 2 and head[2] == "list" else "calendar_write"
        return {
            "whitelist": "members",
            "remove": "members",
            "block": "members",
            "unblock": "members",
            "member": "members",
            "promote": "members",
            "demote": "members",
            "mode": "settings",
            "trigger": "settings",
            "pause": "settings",
            "resume": "settings",
            "limit": "settings",
            "calendar": "settings",
            "context": "context",
            "group": "groups",
            "remind": "reminders",
            "cancel": "reminders",
            "folder": "drive_write",
            "rename": "drive_write",
            "move": "drive_write",
            "report": "drive_write",
            "work-package": "project_status",
        }.get(action)

    def command(self, actor, text, cid="", approved_by="", bypass_critical=False):
        actor = self.fresh(actor)
        # Legacy shortcuts pass through the same live policy as conversational tools.
        capability = self.capability(text)
        if capability is None:
            return self.cmd._execute(actor, text, cid)
        approvers = self.check(actor, capability)
        critical = text.startswith("/admin promote ") or text == "/admin mode public"
        if critical and not bypass_critical:
            if text.startswith("/admin promote ") and actor.phone != self.ws.owner_phone():
                raise PermissionError("Only the owner can appoint admins")
            approvers = [self.ws.owner_phone()]
        if (
            approvers
            and approved_by not in approvers
            and (actor.phone not in approvers or critical and not bypass_critical)
        ):
            return json.dumps(self.request(actor, text, capability, approvers), ensure_ascii=False)
        # This elevation applies only to the single operation whose capability was checked.
        # Permanent-owner checks inside member_change still apply to admin appointment.
        permitted = replace(actor, admin=True) if text.startswith("/admin ") else actor
        return self.cmd._execute(permitted, text, cid)

    def queue_message(self, actor, target, text, kind="message", approval_id=""):
        if not isinstance(text, str) or not 1 <= len(text) <= 6000:
            raise ValueError("Message must contain 1–6000 characters")
        if target.endswith("@g.us"):
            if target not in self.ws.policy()["groups"]:
                raise ValueError("Select a registered group")
        else:
            phone = target.removesuffix("@s.whatsapp.net").lstrip("+")
            if not re.fullmatch(r"[1-9][0-9]{7,14}", phone):
                raise ValueError("Resolve the recipient to an approved phone number first")
            if self.ws.role(phone) == "guest" or not self.ws.can_access(phone):
                raise ValueError("The recipient must be an active approved member")
            target = phone + "@s.whatsapp.net"
        key = uuid4().hex
        with self.ws.db.connect() as c:
            c.execute(
                "INSERT INTO notifications VALUES (?,?,?,?,?,'pending')",
                (key, "", target, text, now()),
            )
            c.execute(
                "INSERT INTO outbound_metadata VALUES (?,?,?,?)",
                (key, actor.user.id, kind, approval_id),
            )
        return {"status": "queued", "delivery_id": key, "target": target}

    def request(self, actor, command, capability, approvers):
        for phone in approvers:
            if self.ws.role(phone) == "guest" or not self.ws.can_access(phone):
                raise ValueError("An approver is unavailable; ask the owner to update the rule")
        with self.ws.db.connect() as c:
            existing = c.execute(
                "SELECT id FROM action_requests WHERE requester=? AND command=? "
                "AND state='pending' AND created>?",
                (actor.user.id, command, (datetime.now(UTC) - timedelta(hours=24)).isoformat()),
            ).fetchone()
            if existing:
                return {"status": "awaiting_approval", "request_id": existing[0]}
            key = uuid4().hex[:10]
            c.execute(
                "INSERT INTO action_requests VALUES (?,?,?,?,?,?,?,?,'pending','')",
                (
                    key,
                    actor.user.id,
                    actor.chat,
                    actor.channel,
                    command,
                    capability,
                    json.dumps(approvers),
                    now(),
                ),
            )
        for phone in approvers:
            self.queue_message(
                actor,
                phone,
                f"Approval requested by {actor.user.name} ({key}).\n"
                f"{self.describe(command, actor)}\n"
                "Reply naturally to approve or reject this request. "
                f"Reference: {key}. Nothing has been executed yet.",
                "approval",
                key,
            )
        return {
            "status": "awaiting_approval",
            "request_id": key,
            "approvers_notified": True,
            "expires_in_hours": 24,
        }

    def pending(self, actor):
        with self.ws.db.connect() as c:
            rows = c.execute(
                "SELECT * FROM action_requests WHERE state='pending' AND created>?",
                ((datetime.now(UTC) - timedelta(hours=24)).isoformat(),),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "requester": r["requester"],
                "action": r["command"],
                "created": r["created"],
            }
            for r in rows
            if actor.phone in json.loads(r["approvers"]) or r["requester"] == actor.user.id
        ]

    def resolve(self, actor, key, approve):
        actor = self.fresh(actor)
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM action_requests WHERE id=?", (key,)).fetchone()
            if not row or actor.phone not in json.loads(row["approvers"]):
                raise PermissionError("You are not a designated approver for this request")
            if row["state"] != "pending":
                return {"status": row["state"], "result": row["result"]}
            if datetime.now(UTC) - datetime.fromisoformat(row["created"]) > timedelta(hours=24):
                raise ValueError("That approval request expired")
            c.execute(
                "UPDATE action_requests SET state=? WHERE id=?",
                ("executing" if approve else "rejected", key),
            )
            c.execute(
                "UPDATE notifications SET state='cancelled' WHERE id IN "
                "(SELECT notification_id FROM outbound_metadata WHERE approval_id=?) "
                "AND state IN ('pending','queued')",
                (key,),
            )
        if not approve:
            return {"status": "rejected"}
        with self.ws.db.connect() as c:
            user = c.execute("SELECT id,name FROM users WHERE id=?", (row["requester"],)).fetchone()
        from app.domain import User

        requester = self.ws.actor(User(user["id"], user["name"]), row["chat"], row["channel"])
        try:
            requester = self.fresh(requester)
            allowed = self.check(requester, row["capability"])
            if allowed and actor.phone not in allowed:
                raise PermissionError("Approval rules changed; a current approver is required")
            if row["command"].startswith("@schedule_source "):
                result = json.dumps(
                    self.cmd.schedule_sources.confirm(
                        requester, row["command"].split()[1], approved_by=actor.phone
                    )
                )
            elif row["command"].startswith("@scheduled "):
                result = self.cmd.schedule.scheduler.approve(
                    row["command"].split()[1], requester, actor.phone
                )
            elif row["command"].startswith("@message "):
                data = json.loads(row["command"][9:])
                result = json.dumps(self.queue_message(requester, **data))
            else:
                result = self.command(
                    requester, row["command"], approved_by=actor.phone, bypass_critical=True
                )
            state = "completed"
        except (ValueError, PermissionError) as error:
            state, result = "failed", str(error)
        except Exception:
            state, result = "uncertain", "Result could not be confirmed; inspect before retrying"
        with self.ws.db.connect() as c:
            c.execute(
                "UPDATE action_requests SET state=?,result=? WHERE id=?", (state, result, key)
            )
        self.ws.audit(actor, f"approval {key} {state}")
        if requester.phone and requester.phone != actor.phone:
            try:
                self.queue_message(actor, requester.phone, f"Request {key}: {state}.\n{result}")
            except ValueError:
                pass
        return {"status": state, "result": result}

    def read_calendar(self, actor, period="this_week", start="", end="", calendar="shared"):
        actor = self.fresh(actor)
        if not actor.trusted:
            raise PermissionError("Calendar access requires approved membership")
        zone = self.cmd.schedule.timezone(actor.user.id)
        clock = datetime.now(ZoneInfo(zone))
        today = clock.replace(hour=0, minute=0, second=0, microsecond=0)
        if not start or not end:
            if period in {"this_week", "next_week"}:
                # Cairo team convention; returned dates make this interpretation explicit.
                begin = today - timedelta(days=(today.weekday() - self.cmd.settings.week_start) % 7)
                if period == "next_week":
                    begin += timedelta(days=7)
                finish = begin + timedelta(days=7)
            elif period in {"today", "tomorrow"}:
                begin = today + timedelta(days=int(period == "tomorrow"))
                finish = begin + timedelta(days=1)
            elif period == "upcoming":
                begin, finish = clock, clock + timedelta(days=7)
            else:
                raise ValueError("Provide a period or date range")
            start, end = begin.isoformat(), finish.isoformat()
        calendar_id = self.ws.get("shared_calendar", "")
        if calendar == "mine":
            with self.ws.db.connect() as c:
                row = c.execute(
                    "SELECT calendar_id FROM calendar_links WHERE user_id=?", (actor.user.id,)
                ).fetchone()
            calendar_id = row[0] if row else ""
        if not calendar_id:
            raise ValueError("That calendar is not linked yet")
        result = self.cmd.schedule.calendar.event(calendar_id, ["list", start, end], [], zone)
        return {
            **result,
            "start": start,
            "end_exclusive": end,
            "timezone": zone,
            "source": "Google Calendar",
            "calendar": calendar,
        }

    def run(self, actor, data, cid=""):
        actor = self.fresh(actor)
        if not isinstance(data, dict) or len(json.dumps(data)) > 16000:
            raise ValueError("Invalid action payload")
        action = data.get("action", "")

        def value(key, default=None):
            item = data.get(key, default)
            if not isinstance(item, str) or (not item and default is None):
                raise ValueError(f"The request needs {key}")
            return item

        def field(key, default=None):
            item = value(key, default)
            if "|" in item or "\n" in item:
                raise ValueError(f"Use a single line for {key}")
            return item

        target = data.get("target", "")
        if action == "permission_set":
            if actor.phone != self.ws.owner_phone():
                raise PermissionError("Only the permanent owner can change capability permissions")
            cap, effect = value("capability"), value("effect")
            if cap not in CAPABILITIES or effect not in {"allow", "deny", "approval", "reset"}:
                raise ValueError("Choose a supported capability and permission effect")
            subject = (target or "*").lstrip("+")
            if subject != "*" and self.ws.role(subject) == "guest":
                raise ValueError("Resolve the subject to an approved member first")
            approvers = [p.lstrip("+") for p in data.get("approvers", [])]
            if effect == "approval" and (
                not approvers
                or any(self.ws.role(p) == "guest" or not self.ws.can_access(p) for p in approvers)
            ):
                raise ValueError("Choose one or more active approved people as approvers")
            with self.ws.db.connect() as c:
                if effect == "reset":
                    c.execute(
                        "DELETE FROM capability_rules WHERE capability=? AND subject=?",
                        (cap, subject),
                    )
                else:
                    c.execute(
                        "INSERT OR REPLACE INTO capability_rules VALUES (?,?,?,?,?)",
                        (cap, subject, effect, json.dumps(approvers), now()),
                    )
            self.ws.audit(actor, f"permission {cap} {subject} {effect}")
            return {
                "status": "updated",
                "capability": cap,
                "subject": subject,
                "effect": effect,
                "approvers": approvers,
                "approval_mode": "any listed approver",
            }
        if action == "approval_resolve":
            if data.get("decision") not in {"approve", "reject"}:
                raise ValueError("Choose approve or reject")
            return self.resolve(actor, value("id"), data["decision"] == "approve")
        if action == "message_send":
            approvers = self.check(actor, "send_message")
            payload = {"target": value("target"), "text": value("text")}
            if approvers and actor.phone not in approvers:
                return self.request(
                    actor, "@message " + json.dumps(payload), "send_message", approvers
                )
            return self.queue_message(actor, **payload)
        if action == "memory_save":
            command = f"/remember {field('title')} | {field('text')}"
        elif action == "memory_replace":
            command = f"/replace {field('id')} | {field('title')} | {field('text')}"
        elif action == "memory_forget":
            command = "/forget " + field("id")
        elif action == "schedule_add":
            prefix = "weekly " + field("weekdays") + " " if data.get("weekdays") else ""
            command = f"/busy {prefix}{field('start')} {field('end')} | {field('title', 'Busy')}"
        elif action in {"schedule_remove", "schedule_complete"}:
            command = "/schedule " + (
                "remove " + field("id") if action.endswith("remove") else "complete " + field("end")
            )
        elif action == "timezone_set":
            command = "/timezone " + field("timezone")
        elif action == "calendar_link":
            command = "/calendar " + field("target")
        elif action in {"event_create", "event_reschedule", "event_cancel"}:
            op = action[6:]
            command = "/admin event " + op
            if op != "create":
                command += " " + field("id")
            if op != "cancel":
                # Validate dates before requesting any approval.
                for key in ("start", "end"):
                    instant(value(key), self.cmd.schedule.timezone(actor.user.id))
                    command += " " + field(key)
            if op == "create":
                command += f" | {field('title')} | {field('text', '')}"
        elif action == "member_update":
            op = field("operation")
            if op not in {"whitelist", "block", "unblock", "remove", "promote", "demote", "member"}:
                raise ValueError("Unknown member operation")
            command = f"/admin {op} {field('target')}"
            if op == "whitelist":
                command += " | " + field("title", "Member")
            if op == "member":
                command += f" | {field('title')} | {field('role')} | {field('text', '')}"
        elif action == "settings_update":
            op = field("operation")
            if op not in {"mode", "trigger", "pause", "resume", "limit", "calendar"}:
                raise ValueError("Unknown setting")
            command = f"/admin {op} {field('target', '')}".strip()
        elif action == "group_manage":
            op = field("operation")
            if op not in {"remember", "forget", "target"}:
                raise ValueError("Unknown group operation")
            command = f"/admin group {op} {field('target')}"
        elif action == "reminder_create":
            command = f"/admin remind {field('recurrence')} | {field('text')}"
        elif action == "reminder_cancel":
            command = "/admin cancel " + field("id")
        elif action == "work_package_update":
            command = (
                f"/admin work-package {field('title')} | {field('status')} | {field('text', '')}"
            )
        elif action == "context_update":
            command = f"/admin context {field('target')} | {field('text')}"
        elif action in {"drive_folder", "drive_rename", "drive_move", "drive_report"}:
            op = action[5:]
            command = f"/admin {op} {field('title') if op in {'folder', 'report'} else field('id')}"
            if op == "report":
                command += " | " + value("text")
            elif op != "folder" or target:
                command += " | " + field("target")
        else:
            raise ValueError("Unknown action")
        return {"result": self.command(actor, command, cid)}

    def execute(self, actor, data, cid=""):
        # A retried WhatsApp delivery/tool call cannot repeat a committed mutation.
        request_id = current_request.get() or uuid4().hex
        key = (
            "action:"
            + hashlib.sha256(
                (request_id + actor.user.id + json.dumps(data, sort_keys=True)).encode()
            ).hexdigest()
        )
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT response FROM command_receipts WHERE id=?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
            c.execute(
                "INSERT INTO command_receipts VALUES (?,?,?)",
                (
                    key,
                    json.dumps(
                        {
                            "status": "uncertain",
                            "message": "Inspect state before deliberately retrying",
                        }
                    ),
                    now(),
                ),
            )
        try:
            result = self.run(actor, data, cid)
        except (ValueError, PermissionError, KeyError) as error:
            result = {"error": str(error), "executed": False}
        except Exception:
            result = {"error": "Action result could not be confirmed; inspect before retrying"}
        with self.ws.db.connect() as c:
            c.execute(
                "UPDATE command_receipts SET response=? WHERE id=?", (json.dumps(result), key)
            )
        return result
