"""Explicit, authenticated chat commands. No model-produced text executes by itself."""

import json
import re
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.context import ContextFiles
from app.scheduling import Scheduling, days, instant
from app.workspace import now

HELP = """Chat: ask normally; use /assistant or mention/reply in groups.
/about — identity, project and capabilities
/status — current editable work-package status
/remember subject | decision — explicitly save a shared decision
/memories [query] — search active decisions
/replace ID | subject | new decision — retain the old decision in history
/forget ID — retire your decision (admins can retire any)
/history [query] — superseded decisions
/busy START END | label — add your busy interval (ISO date/time)
/busy weekly Mon,Wed 09:00 11:00 | label — weekly timetable
/schedule — list your timetable; /schedule remove ID
/schedule complete YYYY-MM-DD — declare manual timetable complete through this date
/timezone Africa/Cairo — set your timezone
/availability START END — check team conflicts and unknown availability
/calendar ID — link a calendar shared with the connected Google account
/calendar unlink — disconnect your calendar mapping
/confirm ID — execute your reviewed command proposal
/help admin — admin controls
Dates use your saved timezone unless an explicit offset is included."""

ADMIN_HELP = """Admin commands:
/admin status | members | groups | reminders | audit
/admin mode public|whitelist|owner
/admin whitelist +PHONE | Name — approve a regular member
/admin remove +PHONE — remove whitelist access
/admin block +PHONE | unblock +PHONE — block takes precedence over public mode
/admin promote +PHONE | demote +PHONE — permanent owner only
/admin member +PHONE | Name | Project role | Duties
/admin group remember Name — register this group and select it for reminders
/admin group target GROUP_ID — select a registered reminder target
/admin group forget GROUP_ID — disable group and cancel pending reminders
/admin trigger mention|all — group trigger behavior
/admin pause | resume — suspend ordinary replies and reminder delivery
/admin context identity|project|team | JSON_OBJECT
/admin work-package Name | active|planned|awaiting planning|proposed|completed|blocked | Notes
/admin calendar shared CALENDAR_ID
/admin event create START END | Title | Description
/admin event reschedule EVENT_ID START END
/admin event list START END
/admin event cancel EVENT_ID
/admin remind once YYYY-MM-DDTHH:MM | Message
/admin remind daily N HH:MM | Message — every N days, starting at next occurrence
/admin remind weekly Mon,Wed HH:MM | Message
/admin cancel REMINDER_ID
/admin limit user|team NUMBER — daily request caps
/admin reset-context — clear this conversation history
/admin report Title | Content — create a Markdown report in Drive
/admin folder Name | PARENT_ID — optional parent defaults to project root
/admin rename FILE_ID | New name
/admin move FILE_ID | DESTINATION_FOLDER_ID
Use /help for member commands. Admin roles are granted only by the permanent owner.
Public mode exposes shared project knowledge to anyone who contacts the bot.
Reminders run only while this computer and the bridge are online; old occurrences are skipped."""


class Commands:
    def __init__(self, workspace, settings, drive=None, ingestion=None):
        self.ws, self.settings = workspace, settings
        self.context = ContextFiles(settings.context_path)
        self.schedule = Scheduling(workspace, settings)
        self.drive, self.ingestion = drive, ingestion
        from app.actions import Actions

        self.actions = Actions(self)

    def about(self, actor):
        identity, project = self.context.read("identity"), self.context.read("project")
        calendar = (
            "authorization configured; live access checked on use"
            if self.schedule.calendar.ready
            else "not connected yet"
        )
        purpose = identity.get("purpose", "Help the team").rstrip(". ")
        project_name = project.get("name", "not configured").rstrip(". ")
        return (
            f"I'm {identity.get('name', 'your project assistant')}, created and owned by "
            f"{identity.get('creator', 'the project owner')}. "
            f"My purpose: {purpose}.\n\n"
            f"Project: {project_name}. "
            f"{project.get('objective', 'The project objective has not been supplied.')}\n\n"
            "I can chat, reason, find Drive files, answer indexed PDF questions with sources, "
            "search the web, store explicit decisions, compare recorded availability, "
            "and run admin-configured reminders. Admins can manage members and settings, "
            "create reports, and schedule or reschedule shared-calendar events in chat. "
            f"Google Calendar availability: {calendar}. "
            "Missing evidence or missing schedules mean unknown, not a guess. "
            "I run on the owner's computer and cannot send reminders while it is offline.\n\n"
            f"You are {actor.user.name}; your authenticated access is "
            f"{'admin' if actor.admin else 'member' if actor.trusted else 'public visitor'}. "
            "Use /help for commands."
        )

    def dispatch(self, actor, text, conversation_id="", receipt=""):
        text = re.sub(r"^/(?:assistant|nedal|plume)\s+", "", text.strip(), flags=re.I)
        if not text.startswith("/"):
            return None
        if receipt:
            with self.ws.db.connect() as c:
                row = c.execute(
                    "SELECT response FROM command_receipts WHERE id=?", (receipt,)
                ).fetchone()
            if row:
                return row[0]
            with self.ws.db.connect() as c:
                c.execute(
                    "INSERT OR IGNORE INTO command_receipts VALUES (?,?,?)",
                    (
                        receipt,
                        "Command result is unconfirmed. Inspect status before "
                        "deliberately repeating it.",
                        now(),
                    ),
                )
        try:
            result = self.execute(actor, text, conversation_id)
        except (ValueError, KeyError, PermissionError) as error:
            result = str(error) + "\nUse /help or /help admin for the exact syntax."
        except Exception:
            result = "Command could not be confirmed. Check status before repeating a write."
        if receipt and result is not None:
            with self.ws.db.connect() as c:
                c.execute(
                    "UPDATE command_receipts SET response=? WHERE id=?",
                    (result, receipt),
                )
        return result

    def execute(self, actor, text, cid=""):
        return self.actions.command(actor, text, cid)

    def _execute(self, actor, text, cid=""):
        command, _, argument = text.partition(" ")
        command, argument = command.lower(), argument.strip()
        parts = [p.strip() for p in argument.split("|")]
        if command == "/help":
            return ADMIN_HELP if argument == "admin" and actor.admin else HELP
        if command == "/about":
            return self.about(actor)
        if command == "/status":
            return json.dumps(self.ws.project_status(), ensure_ascii=False, indent=2)
        if command == "/admin":
            if not actor.admin:
                raise PermissionError("Only an authenticated admin can execute this command")
            result = self.admin(actor, argument, cid)
            self.ws.audit(actor, argument.split("|")[0][:200])
            return result
        if command == "/confirm":
            with self.ws.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM pending_actions WHERE id=?", (argument,)).fetchone()
                if (
                    not row
                    or row["actor"] != actor.user.id
                    or row["chat"] != actor.chat
                    or row["used"]
                    or datetime.now(UTC) - datetime.fromisoformat(row["created"])
                    > timedelta(minutes=15)
                ):
                    raise ValueError(
                        "Proposal is missing, expired, used, or belongs to another conversation"
                    )
                c.execute("UPDATE pending_actions SET used=1 WHERE id=?", (argument,))
            return self.execute(actor, row["command"], cid)
        if command in {"/remember", "/replace"}:
            needed = 3 if command == "/replace" else 2
            if len(parts) != needed:
                raise ValueError(
                    "Use /remember subject | decision or /replace ID | subject | decision"
                )
            key = self.ws.remember(actor, parts[-2], parts[-1], parts[0] if needed == 3 else "")
            return f"Saved project decision {key}: {parts[-1]}"
        if command in {"/memories", "/history"}:
            return json.dumps(
                self.ws.memories(argument, "active" if command == "/memories" else "superseded"),
                ensure_ascii=False,
                indent=2,
            )
        if command == "/forget":
            with self.ws.db.connect() as c:
                row = c.execute(
                    "SELECT author FROM project_memories WHERE id=?", (argument,)
                ).fetchone()
                if not row or (row[0] != actor.user.id and not actor.admin):
                    raise PermissionError("Only the author or an admin can retire this decision")
                c.execute("UPDATE project_memories SET status='retired' WHERE id=?", (argument,))
            return "Decision retired; its audit history is preserved."
        if (
            command in {"/busy", "/schedule", "/timezone", "/calendar", "/availability"}
            and not actor.trusted
        ):
            raise PermissionError("Schedule storage is available to approved team members")
        if command == "/busy":
            args = parts[0].split()
            weekly = args[0] == "weekly" if args else False
            if len(args) != (4 if weekly else 2):
                raise ValueError(
                    "Use /busy START END | label, or /busy weekly Mon,Wed 09:00 11:00 | label"
                )
            key = self.schedule.add_busy(
                actor,
                args[-2],
                args[-1],
                args[1] if weekly else "",
                parts[1] if len(parts) > 1 else "Busy",
            )
            return f"Busy time saved: {key}. Only busy intervals are shared, not the private label."
        if command == "/availability":
            args = argument.split()
            if len(args) != 2:
                raise ValueError("Use /availability START END with ISO date/time")
            return json.dumps(
                self.schedule.availability(*args, self.schedule.timezone(actor.user.id)),
                ensure_ascii=False,
                indent=2,
            )
        if command == "/timezone":
            ZoneInfo(argument)
            self.profile(actor)
            with self.ws.db.connect() as c:
                c.execute(
                    "UPDATE member_profiles SET timezone=? WHERE user_id=?",
                    (argument, actor.user.id),
                )
            return "Your timezone is now " + argument
        if command == "/schedule":
            if argument.startswith("remove "):
                with self.ws.db.connect() as c:
                    changed = c.execute(
                        "DELETE FROM busy_slots WHERE id=? AND user_id=?",
                        (argument[7:], actor.user.id),
                    ).rowcount
                return "Removed." if changed else "No matching busy time belongs to you."
            if argument.startswith("complete "):
                until = datetime.fromisoformat(argument[9:]).date().isoformat()
                self.profile(actor)
                with self.ws.db.connect() as c:
                    c.execute(
                        "UPDATE member_profiles SET complete_until=? WHERE user_id=?",
                        (until, actor.user.id),
                    )
                return "Your manual timetable is marked complete through " + until
            with self.ws.db.connect() as c:
                return json.dumps(
                    [
                        dict(r)
                        for r in c.execute(
                            "SELECT * FROM busy_slots WHERE user_id=?", (actor.user.id,)
                        )
                    ],
                    ensure_ascii=False,
                )
        if command == "/calendar":
            with self.ws.db.connect() as c:
                if argument == "unlink":
                    c.execute("DELETE FROM calendar_links WHERE user_id=?", (actor.user.id,))
                    return "Calendar mapping removed."
                if not argument or len(argument) > 300 or " " in argument:
                    raise ValueError("Supply the Calendar ID, not a browser URL")
                c.execute(
                    "INSERT OR REPLACE INTO calendar_links VALUES (?,?)", (actor.user.id, argument)
                )
            return (
                "Calendar ID saved. Share it with the connected Google account. "
                "Unavailable access is reported as unknown."
            )
        return "Unknown command. Use /help or /help admin."

    def profile(self, actor):
        with self.ws.db.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO member_profiles(user_id,name,timezone) VALUES (?,?,?)",
                (actor.user.id, actor.user.name, self.settings.default_timezone),
            )

    def propose(self, actor, command):
        if (
            not command.startswith("/")
            or command.startswith(("/confirm", "/help"))
            or len(command) > 6000
        ):
            raise ValueError("Propose one supported command")
        if command.startswith("/admin") and not actor.admin:
            raise PermissionError("Admin only")
        key = uuid4().hex[:10]
        with self.ws.db.connect() as c:
            c.execute(
                "INSERT INTO pending_actions VALUES (?,?,?,?,?,0)",
                (key, actor.user.id, actor.chat, command, now()),
            )
        return {
            "proposal": command,
            "confirmation": f"/confirm {key}",
            "expires_in_minutes": 15,
            "status": "Not executed. Awaiting the authenticated requester's confirmation.",
        }

    def admin(self, actor, argument, cid):
        head, *body = [p.strip() for p in argument.split("|")]
        args = head.split()
        if not args:
            return ADMIN_HELP
        action = args[0]
        if action in {"status", "members", "groups", "reminders", "audit"}:
            if action == "status":
                return json.dumps(
                    {
                        "policy": self.ws.policy(),
                        "calendar_connected": self.schedule.calendar.ready,
                        "running": "local computer; reminders stop when offline",
                    },
                    ensure_ascii=False,
                )
            if action == "members":
                return json.dumps(self.ws.db.whatsapp_members(), ensure_ascii=False)
            table = {
                "groups": "registered_groups",
                "reminders": "reminders",
                "audit": "admin_audit",
            }[action]
            with self.ws.db.connect() as c:
                return json.dumps(
                    [
                        dict(r)
                        for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 50")
                    ],
                    ensure_ascii=False,
                )
        if action in {"mode", "trigger"}:
            choices = {"mode": {"public", "whitelist", "owner"}, "trigger": {"mention", "all"}}
            if len(args) != 2 or args[1] not in choices[action]:
                raise ValueError("Choose " + ", ".join(sorted(choices[action])))
            self.ws.set("mode" if action == "mode" else "group_trigger", args[1])
            return f"{action} set to {args[1]}." + (
                " Anyone can now access shared project information." if args[1] == "public" else ""
            )
        if action in {"pause", "resume"}:
            self.ws.set("paused", action == "pause")
            return "Paused; owner commands remain available." if action == "pause" else "Resumed."
        if action in {"whitelist", "remove", "block", "unblock", "promote", "demote"}:
            if len(args) != 2:
                raise ValueError("Supply a full phone number")
            self.ws.member_change(
                actor,
                args[1],
                "approve" if action == "whitelist" else action,
                body[0] if body else "Member",
            )
            return f"{action} applied to account ending {args[1][-4:]}."
        if action == "member":
            if len(args) != 2 or len(body) != 3:
                raise ValueError("Use /admin member +PHONE | Name | Project role | Duties")
            user = self.ws.db.whatsapp_user(args[1].lstrip("+"))
            if not user:
                raise ValueError("Approve that member first")
            with self.ws.db.connect() as c:
                c.execute("UPDATE users SET name=? WHERE id=?", (body[0][:100], user.id))
                c.execute(
                    "INSERT INTO "
                    "member_profiles(user_id,name,project_role,duties,timezone) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "name=excluded.name,project_role=excluded.project_role,duties=excluded.duties",
                    (
                        user.id,
                        body[0][:100],
                        body[1][:200],
                        body[2][:2000],
                        self.settings.default_timezone,
                    ),
                )
                people = [
                    dict(r)
                    for r in c.execute("SELECT name,project_role,duties FROM member_profiles")
                ]
            self.context.write("team", {"members": people})
            return "Team profile saved. Project roles do not change access privileges."
        if action == "work-package":
            if len(body) != 2:
                raise ValueError("Use work-package Name | status | Notes")
            self.ws.update_work_package(actor, " ".join(args[1:]), body[0], body[1])
            return "Work-package status updated with revision history."
        if action == "calendar":
            if len(args) != 3 or args[1] != "shared" or len(args[2]) > 300:
                raise ValueError("Use calendar shared CALENDAR_ID")
            self.ws.set("shared_calendar", args[2])
            return (
                "Shared calendar ID saved. Google authorization and calendar "
                "permissions are still required."
            )
        if action == "event":
            calendar_id = self.ws.get("shared_calendar", "")
            if not calendar_id:
                raise ValueError("Configure a shared Calendar ID first")
            result = self.schedule.calendar.event(
                calendar_id, args[1:], body, self.schedule.timezone(actor.user.id)
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        if action == "context":
            if len(args) != 2 or len(body) != 1:
                raise ValueError("Use /admin context identity|project|team | JSON_OBJECT")
            value = json.loads(body[0])
            if not isinstance(value, dict):
                raise ValueError("Context must be an object")
            self.context.write(args[1], value)
            return "Context saved; it will be loaded on the next message."
        if action == "group":
            if len(args) < 3:
                raise ValueError("Use group remember Name, target ID, or forget ID")
            if args[1] == "remember":
                if actor.channel != "group":
                    raise ValueError("Run this command inside the group to remember")
                with self.ws.db.connect() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO registered_groups VALUES (?,?,1)",
                        (actor.chat, " ".join(args[2:])[:100]),
                    )
                self.ws.set("reminder_target", actor.chat)
                return "This group is registered and selected for reminders."
            chat = args[2]
            with self.ws.db.connect() as c:
                if not c.execute(
                    "SELECT 1 FROM registered_groups WHERE chat=?", (chat,)
                ).fetchone():
                    raise ValueError("Unknown group")
                if args[1] == "forget":
                    c.execute("UPDATE registered_groups SET enabled=0 WHERE chat=?", (chat,))
                    c.execute("UPDATE reminders SET active=0 WHERE chat=?", (chat,))
                    c.execute(
                        "UPDATE notifications SET state='cancelled' WHERE chat=? AND "
                        "state IN ('pending','queued')",
                        (chat,),
                    )
                    return "Group disabled and pending reminders cancelled."
                if args[1] != "target":
                    raise ValueError("Unknown group command")
            self.ws.set("reminder_target", chat)
            return "Reminder target selected."
        if action == "remind":
            if len(body) != 1 or not body[0]:
                raise ValueError("Supply the schedule followed by | Reminder text")
            zone = self.schedule.timezone(actor.user.id)
            clock = datetime.now(UTC)
            if len(args) == 3 and args[1] == "once":
                due, rule = instant(args[2], zone), {"kind": "once"}
            elif len(args) == 4 and args[1] in {"daily", "weekly"}:
                time.fromisoformat(args[3])
                rule = {"kind": args[1]}
                if args[1] == "daily":
                    rule["days"] = int(args[2])
                    if not 1 <= rule["days"] <= 365:
                        raise ValueError("Day interval must be 1–365")
                else:
                    rule["weekdays"] = days(args[2])
                local = clock.astimezone(ZoneInfo(zone))
                due = instant(f"{local.date()}T{args[3]}", zone)
                if due <= clock or (
                    rule["kind"] == "weekly" and local.weekday() not in rule["weekdays"]
                ):
                    due = self.schedule.next_time(due, {**rule, "days": 1}, zone)
            else:
                raise ValueError("Use remind once DATETIME, daily N HH:MM, or weekly Mon,Wed HH:MM")
            if due <= clock:
                raise ValueError("Reminder time must be in the future")
            chat = actor.chat if actor.channel == "group" else self.ws.get("reminder_target", "")
            key = self.schedule.create_reminder(actor, chat, body[0], due, rule, zone)
            return (
                f"Reminder {key} saved for {due.astimezone(ZoneInfo(zone)).isoformat()} "
                f"({zone}). Runs while this computer is online."
            )
        if action == "cancel":
            if len(args) != 2:
                raise ValueError("Supply a reminder ID")
            with self.ws.db.connect() as c:
                changed = c.execute("UPDATE reminders SET active=0 WHERE id=?", (args[1],)).rowcount
                c.execute(
                    "UPDATE notifications SET state='cancelled' WHERE reminder_id=? "
                    "AND state IN ('pending','queued')",
                    (args[1],),
                )
            return "Reminder cancelled." if changed else "Reminder not found."
        if action == "limit":
            if len(args) != 3 or args[1] not in {"user", "team"} or not 1 <= int(args[2]) <= 10000:
                raise ValueError("Use limit user|team NUMBER (1–10000)")
            self.ws.set("daily_" + args[1] + "_requests", int(args[2]))
            return "Daily request limit updated."
        if action == "reset-context":
            with self.ws.db.connect() as c:
                c.execute("DELETE FROM turns WHERE conversation_id=?", (cid,))
            return "This conversation's recent context was cleared; project decisions remain."
        if action in {"folder", "rename", "move", "report"}:
            return self.drive_action(actor, action, " ".join(args[1:]), body)
        return ADMIN_HELP

    def drive_action(self, actor, action, target, body):
        if not self.drive or not self.drive.ready:
            raise ValueError("Drive is not connected")
        if action == "folder":
            result = self.drive.create_folder(target, body[0] if body else "")
        elif action in {"rename", "move"}:
            if len(body) != 1:
                raise ValueError("Supply FILE_ID | new name or destination ID")
            result = getattr(self.drive, action)(target, body[0])
        else:
            import tempfile
            from pathlib import Path

            if not self.ingestion or not body:
                raise ValueError("Use report Title | Report content")
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "report.md"
                path.write_text(" | ".join(body), encoding="utf-8")
                result = self.ingestion.ingest(path, target[:100] + ".md", actor.user)["file"]
        return "Drive operation completed: " + result["drive_url"]
