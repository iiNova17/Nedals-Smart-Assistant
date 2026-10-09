"""Durable scheduled-task service backed by persistent records and atomic execution claims."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.scheduling import instant
from app.task_policy import TaskPolicy
from app.workspace import Actor, Workspace, current_request, now

SUPPORTED_TASK_TYPES = {"reminder", "message", "summary"}


def format_proposal_preview(
    task_type: str,
    destination_name: str,
    due_dt: datetime,
    timezone_name: str,
    arguments: dict[str, Any],
    recurrence_desc: str = "Once",
) -> str:
    """Format the natural-language preview for user confirmation."""
    local_dt = due_dt.astimezone(ZoneInfo(timezone_name))
    formatted_date = local_dt.strftime("%A, %d %B %Y, at %I:%M %p").replace(" 0", " ")
    msg = arguments.get("message") or arguments.get("content") or arguments.get("title") or ""
    label = "Message" if task_type in {"reminder", "message"} else "Details"
    type_header = "reminder" if task_type == "reminder" else f"scheduled {task_type} task"
    return (
        f"📅 Please confirm this {type_header}:\n"
        f"When: {formatted_date} — {timezone_name}\n"
        f"Where: {destination_name}\n"
        f"{label}: {msg}\n"
        f"Repeats: {recurrence_desc}\n\n"
        'Reply "confirm," or tell me what to change.'
    )


def compute_next_run(
    due: datetime, rule: dict[str, Any], timezone_str: str, clock: datetime | None = None
) -> datetime | None:
    """Calculate the next wall-clock recurrence across daylight saving boundaries."""
    clock = clock or datetime.now(UTC)
    zone = ZoneInfo(timezone_str)
    local = due.astimezone(zone)
    kind = rule.get("kind", "once")

    if kind == "once":
        return None

    step = rule.get("days", 1) if kind == "daily" else 1
    elapsed_days = max(0, (clock.astimezone(zone).date() - local.date()).days)
    first_offset = max(step, (elapsed_days // step) * step)
    for offset in range(first_offset, first_offset + 370, step):
        candidate = local + timedelta(days=offset)
        if kind == "weekly" and candidate.weekday() not in rule.get("weekdays", []):
            continue
        try:
            nxt = instant(candidate.replace(tzinfo=None).isoformat(), timezone_str)
            if nxt > clock:
                return nxt
        except ValueError:
            continue
    return None


class TaskScheduler(TaskPolicy):
    """Durable scheduler for confirmed reminders and background tasks."""

    def __init__(self, workspace: Workspace, settings: Any, drive: Any = None):
        self.ws = workspace
        self.settings = settings
        self.drive = drive

    def recover(self):
        """On single-worker startup, never replay an interrupted occurrence."""
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE notifications SET state='cancelled' WHERE state IN ('pending','queued') "
                "AND id IN (SELECT 'notif_' || occurrence_id "
                "FROM task_executions WHERE status='running')"
            )
            c.execute(
                "UPDATE scheduled_tasks SET status='uncertain',last_result=? "
                "WHERE task_id IN (SELECT task_id FROM task_executions WHERE status='running')",
                ("Interrupted execution; inspect before retrying",),
            )
            c.execute(
                "UPDATE task_executions SET status='uncertain',completed_at=?,result=? "
                "WHERE status='running'",
                (now(), "Interrupted execution; no automatic retry"),
            )

    def propose_task(
        self,
        actor: Actor,
        task_type: str,
        destination: str,
        validated_arguments: dict[str, Any],
        schedule: dict[str, Any],
        timezone_str: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """Create a pending proposal requiring user confirmation."""
        if task_type not in SUPPORTED_TASK_TYPES:
            raise ValueError(
                f"Unsupported task type: {task_type}. Supported: {sorted(SUPPORTED_TASK_TYPES)}"
            )

        self.authorize(actor, task_type, destination)
        schedule = dict(schedule)
        kind = schedule.get("kind", "once")
        if kind not in {"once", "daily", "weekly"}:
            raise ValueError("Use once, daily or weekly recurrence")
        if kind == "daily" and (
            type(schedule.get("days", 1)) is not int or not 1 <= schedule.get("days", 1) <= 365
        ):
            raise ValueError("Daily interval must be 1 to 365 days")
        if kind == "weekly":
            weekdays = schedule.get("weekdays", [])
            if not weekdays or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays):
                raise ValueError("Weekly recurrence needs valid weekdays")
        if task_type in {"reminder", "message"}:
            message = (
                validated_arguments.get("message")
                or validated_arguments.get("content")
                or validated_arguments.get("text")
            )
            if not isinstance(message, str) or not 1 <= len(message.strip()) <= 6000:
                raise ValueError("Supply a message of 1 to 6000 characters")
            validated_arguments = {"message": message}
        else:
            validated_arguments = {}
        schedule["_proposal_request"] = current_request.get()
        # Check authorization to schedule tasks
        if not self.ws.can_access(actor.phone, actor.chat, actor.channel):
            raise PermissionError("You are not authorized to schedule tasks")

        # Destination check
        if destination.endswith("@g.us"):
            with self.ws.db.connect() as c:
                row = c.execute(
                    "SELECT name, enabled FROM registered_groups WHERE chat=?", (destination,)
                ).fetchone()
            if not row or not row["enabled"]:
                raise ValueError("Target group is not registered or disabled")
            dest_name = row["name"]
        else:
            dest_name = "Direct Message"

        # Resolve due time
        at_val = schedule.get("at")
        if not at_val:
            raise ValueError("Schedule must include an 'at' timestamp")
        due_utc = instant(at_val, timezone_str)
        if due_utc <= datetime.now(UTC):
            raise ValueError("Scheduled time must be in the future")

        recurrence_desc = schedule.get("recurrence_desc", "Once")
        if schedule.get("kind") == "daily":
            recurrence_desc = f"Every {schedule.get('days', 1)} day(s)"
        elif schedule.get("kind") == "weekly":
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            wd_str = ",".join(day_names[d] for d in schedule.get("weekdays", []))
            recurrence_desc = f"Weekly on {wd_str}"

        proposal_id = "prop_" + uuid4().hex[:10]
        preview_text = format_proposal_preview(
            task_type, dest_name, due_utc, timezone_str, validated_arguments, recurrence_desc
        )

        expires_at = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()

        # Invalidate any older pending proposals by this user in this chat for the same task_type
        with self.ws.db.connect() as c:
            c.execute(
                "UPDATE task_proposals SET status='superseded' "
                "WHERE creator_user_id=? AND origin_chat=? AND task_type=? AND status='pending'",
                (actor.user.id, actor.chat, task_type),
            )
            c.execute(
                "INSERT INTO task_proposals("
                "proposal_id, creator_user_id, origin_chat, task_type, destination, "
                "validated_arguments, schedule, timezone, summary, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    proposal_id,
                    actor.user.id,
                    actor.chat,
                    task_type,
                    destination,
                    json.dumps(validated_arguments),
                    json.dumps(schedule),
                    timezone_str,
                    summary or preview_text,
                    now(),
                    expires_at,
                ),
            )

        return {
            "proposal_id": proposal_id,
            "preview": preview_text,
            "due": due_utc.isoformat(),
            "expires_at": expires_at,
        }

    def confirm_proposal(
        self,
        actor: Actor,
        proposal_id: str | None = None,
        chat: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Confirm a pending task proposal and activate it."""
        clock = datetime.now(UTC).isoformat()
        chat_filter = chat or actor.chat

        with self.ws.db.connect() as c:
            query = (
                "SELECT * FROM task_proposals WHERE creator_user_id=? AND origin_chat=? "
                "AND status='pending' AND expires_at>?"
            )
            params = [actor.user.id, chat_filter, clock]
            if proposal_id:
                query += " AND proposal_id=?"
                params.append(proposal_id)
            query += " ORDER BY created_at DESC"
            rows = [dict(r) for r in c.execute(query, params).fetchall()]

        if not rows:
            return (
                False,
                "No pending proposals found to confirm. (Proposals expire after 15 minutes).",
                None,
            )

        if len(rows) > 1 and not proposal_id:
            summaries = "\n".join(
                f"- ID: {r['proposal_id']} | Type: {r['task_type']} | {r['summary'][:80]}"
                for r in rows
            )
            return (
                False,
                f"Multiple proposals found. Please select one:\n{summaries}",
                None,
            )

        prop = rows[0]
        # Recheck creator permissions at confirmation time
        if not self.ws.db.is_active(actor.user.id):
            return False, "Your account is deactivated.", None
        if not self.ws.can_access(actor.phone, actor.chat, actor.channel):
            return False, "Your scheduling permission has been disabled.", None

        schedule_data = json.loads(prop["schedule"])
        args_data = json.loads(prop["validated_arguments"])
        due_utc = instant(schedule_data["at"], prop["timezone"])

        if due_utc <= datetime.now(UTC):
            return False, "That scheduled time has passed; choose a new time.", None
        if (
            current_request.get()
            and schedule_data.get("_proposal_request") == current_request.get()
        ):
            return False, "Present the preview and wait for a separate user confirmation.", None
        caps, approvers, signature = self.authorize(actor, prop["task_type"], prop["destination"])
        task_id = "task_" + uuid4().hex[:10]
        created_time = now()
        state = "awaiting_approval" if approvers else "scheduled"
        approval = json.dumps(
            {"approver": actor.phone if not approvers else "", "policy": signature}
        )
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            changed = c.execute(
                "UPDATE task_proposals SET status='confirmed' WHERE proposal_id=? "
                "AND status='pending' AND expires_at>?",
                (prop["proposal_id"], now()),
            ).rowcount
            if not changed:
                return False, "Proposal was already consumed or expired.", None
            c.execute(
                "INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    actor.user.id,
                    prop["origin_chat"],
                    prop["destination"],
                    prop["task_type"],
                    json.dumps(args_data),
                    prop["timezone"],
                    json.dumps(schedule_data),
                    due_utc.isoformat(),
                    json.dumps(caps),
                    approval,
                    state,
                    created_time,
                    created_time,
                    "",
                ),
            )
            replacement = schedule_data.get("_replaces")
            if replacement:
                c.execute(
                    "UPDATE scheduled_tasks SET status='cancelled',updated_at=? WHERE task_id=? "
                    "AND creator_user_id=?",
                    (now(), replacement, actor.user.id),
                )
        if approvers:
            actions = getattr(self, "actions", None)
            if actions:
                actions.request(actor, "@scheduled " + task_id, "reminders", approvers)
            return (
                True,
                "Confirmed; waiting for permission approval before scheduling. Task: " + task_id,
                {"task_id": task_id},
            )

        local_dt = due_utc.astimezone(ZoneInfo(prop["timezone"]))
        formatted_date = local_dt.strftime("%A, %d %B %Y, at %I:%M %p").replace(" 0", " ")
        msg = (
            f"✅ *{prop['task_type'].title()} confirmed and scheduled!*\n"
            f"When: {formatted_date}\n"
            f"Task ID: {task_id}"
        )
        return True, msg, {"task_id": task_id, "due": due_utc.isoformat()}

    def cancel_proposal(self, actor: Actor, proposal_id: str) -> bool:
        """Cancel an unconfirmed proposal."""
        with self.ws.db.connect() as c:
            row = c.execute(
                "SELECT creator_user_id FROM task_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if not row:
                return False
            if row[0] != actor.user.id and not actor.admin:
                raise PermissionError("Only creator or admin can cancel proposal")
            c.execute(
                "UPDATE task_proposals SET status='cancelled' WHERE proposal_id=?", (proposal_id,)
            )
            return True

    def list_tasks(
        self, actor: Actor, destination: str = "", include_completed: bool = False
    ) -> list[dict[str, Any]]:
        """List tasks respecting privacy (personal vs shared group tasks)."""
        with self.ws.db.connect() as c:
            query = "SELECT * FROM scheduled_tasks WHERE 1=1"
            params: list[Any] = []

            if actor.channel == "group":
                query += " AND destination=?"
                params.append(actor.chat)
            elif not actor.admin:
                query += " AND creator_user_id=?"
                params.append(actor.user.id)

            if destination:
                query += " AND destination=?"
                params.append(destination)

            if not include_completed:
                query += " AND status IN ('scheduled', 'paused', 'awaiting_approval')"

            query += " ORDER BY next_run_at ASC LIMIT 50"
            rows = [dict(r) for r in c.execute(query, params).fetchall()]

        return rows

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a single task record."""
        with self.ws.db.connect() as c:
            row = c.execute("SELECT * FROM scheduled_tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def manage_task(self, actor: Actor, action: str, task_id: str, **kwargs) -> tuple[bool, str]:
        """Perform conversational management on scheduled tasks (pause, resume, cancel, edit)."""
        task = self.get_task(task_id)
        if not task:
            return False, f"Task {task_id} not found."

        # Check permission: must be creator or admin
        if task["creator_user_id"] != actor.user.id and not actor.admin:
            raise PermissionError("You can only manage your own scheduled tasks.")

        if actor.channel == "group" and task["destination"] != actor.chat:
            raise PermissionError("Manage private tasks in a direct conversation")
        if action in {"resume", "reschedule"}:
            self.authorize(
                self.creator(task), task["task_type"], task["destination"], task["approval_state"]
            )
        if action == "reschedule":
            if task["creator_user_id"] != actor.user.id:
                raise PermissionError("The task creator must confirm a changed schedule")
            rule = json.loads(task["schedule"])
            rule["at"] = kwargs.get("new_due")
            rule["_replaces"] = task_id
            proposal = self.propose_task(
                actor,
                task["task_type"],
                task["destination"],
                json.loads(task["validated_arguments"]),
                rule,
                task["timezone"],
            )
            with self.ws.db.connect() as c:
                c.execute(
                    "UPDATE scheduled_tasks SET status='paused',updated_at=? WHERE task_id=?",
                    (now(), task_id),
                )
            return True, proposal["preview"]
        with self.ws.db.connect() as c:
            if action == "pause":
                if task["status"] != "scheduled":
                    return False, f"Task is {task['status']}, cannot pause."
                c.execute(
                    "UPDATE scheduled_tasks SET status='paused', updated_at=? WHERE task_id=?",
                    (now(), task_id),
                )
                return True, f"Task {task_id} paused."

            elif action == "resume":
                if task["status"] != "paused":
                    return False, f"Task is {task['status']}, not paused."
                c.execute(
                    "UPDATE scheduled_tasks SET status='scheduled', updated_at=? WHERE task_id=?",
                    (now(), task_id),
                )
                return True, f"Task {task_id} resumed."

            elif action == "cancel":
                c.execute(
                    "UPDATE scheduled_tasks SET status='cancelled', updated_at=? WHERE task_id=?",
                    (now(), task_id),
                )
                return True, f"Task {task_id} cancelled."

        return False, f"Unknown action: {action}"

    def execution_history(
        self, task_id: str | None = None, limit: int = 50, actor: Actor | None = None
    ) -> list[dict[str, Any]]:
        """Query execution history for inspectability."""
        with self.ws.db.connect() as c:
            if actor is None:
                raise PermissionError("Authenticated task history requester required")
            query = (
                "SELECT e.*,n.state AS delivery_state FROM task_executions e "
                "JOIN scheduled_tasks t ON t.task_id=e.task_id LEFT JOIN notifications n "
                "ON n.id=e.delivery_id WHERE 1=1"
            )
            params: list[Any] = []
            if actor.channel == "group":
                query += " AND t.destination=?"
                params.append(actor.chat)
            elif not actor.admin:
                query += " AND t.creator_user_id=?"
                params.append(actor.user.id)
            if task_id:
                query += " AND e.task_id=?"
                params.append(task_id)
            query += " ORDER BY started_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 100)))
            return [dict(r) for r in c.execute(query, params).fetchall()]

    def tick(self, clock: datetime | None = None) -> list[str]:
        """Atomic claim and execution of due scheduled tasks."""
        if self.ws.get("paused", False):
            return []
        clock = clock or datetime.now(UTC)
        clock_iso = clock.isoformat()
        executed_occurrence_ids: list[str] = []

        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            due_tasks = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM scheduled_tasks WHERE status='scheduled' AND next_run_at<=?",
                    (clock_iso,),
                ).fetchall()
            ]

        for task in due_tasks:
            task_id = task["task_id"]
            next_run_iso = task["next_run_at"]
            occurrence_id = f"{task_id}:{next_run_iso}"
            due_dt = datetime.fromisoformat(next_run_iso)

            # Atomic occurrence claim
            with self.ws.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                live = c.execute(
                    "SELECT status,next_run_at FROM scheduled_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if not live or live["status"] != "scheduled" or live["next_run_at"] != next_run_iso:
                    continue
                claimed = (
                    c.execute(
                        "INSERT INTO task_executions(occurrence_id, task_id, attempt,"
                        " scheduled_for, started_at, status) "
                        "VALUES (?, ?, 1, ?, ?, 'running') "
                        "ON CONFLICT(occurrence_id) DO NOTHING",
                        (occurrence_id, task_id, next_run_iso, now()),
                    ).rowcount
                    > 0
                )
                if not claimed:
                    continue  # Already claimed by another worker

            # Missed run check: if more than 15 minutes overdue, computer was likely offline
            if clock - due_dt > timedelta(minutes=15):
                rule = json.loads(task["schedule"])
                following = compute_next_run(due_dt, rule, task["timezone"], clock=clock)
                with self.ws.db.connect() as c:
                    c.execute("BEGIN IMMEDIATE")
                    c.execute(
                        "UPDATE task_executions SET status='missed', completed_at=?, "
                        "result='Missed run due to system downtime. Overdue send supp"
                        "ressed.' WHERE occurrence_id=?",
                        (now(), occurrence_id),
                    )
                    c.execute(
                        "UPDATE scheduled_tasks SET next_run_at=?, status=?, updated_at=?, "
                        "last_result='Missed run suppressed' WHERE task_id=?",
                        (
                            (following or due_dt).isoformat(),
                            "scheduled" if following else "completed",
                            now(),
                            task_id,
                        ),
                    )
                continue

            # Re-check permissions at execution time using creator identity
            creator_id = task["creator_user_id"]
            with self.ws.db.connect() as c:
                user_row = c.execute(
                    "SELECT active FROM users WHERE id=?", (creator_id,)
                ).fetchone()
                member_row = c.execute(
                    "SELECT phone FROM whatsapp_members WHERE user_id=?", (creator_id,)
                ).fetchone()
                dest_enabled = True
                if task["destination"].endswith("@g.us"):
                    group_row = c.execute(
                        "SELECT enabled FROM registered_groups WHERE chat=?", (task["destination"],)
                    ).fetchone()
                    dest_enabled = bool(group_row and group_row["enabled"])

            try:
                self.authorize(
                    self.creator(task),
                    task["task_type"],
                    task["destination"],
                    task["approval_state"],
                )
                policy_allowed = True
            except (ValueError, PermissionError):
                policy_allowed = False
            creator_active = bool(user_row and user_row["active"])
            creator_phone = member_row[0] if member_row else ""
            creator_allowed = bool(creator_phone and self.ws.can_access(creator_phone))

            if not creator_active or not creator_allowed or not dest_enabled or not policy_allowed:
                reason = "Current permissions or destination no longer permit this task"
                with self.ws.db.connect() as c:
                    c.execute("BEGIN IMMEDIATE")
                    c.execute(
                        "UPDATE task_executions SET status='blocked', completed_at=?,"
                        " result=? WHERE occurrence_id=?",
                        (now(), reason, occurrence_id),
                    )
                    c.execute(
                        "UPDATE scheduled_tasks SET status='blocked', updated_at=?, l"
                        "ast_result=? WHERE task_id=?",
                        (now(), reason, task_id),
                    )
                continue

            # Execute task handler
            try:
                status, result_text, delivery_id = self._execute_task_action(task, occurrence_id)
            except Exception:
                status, result_text, delivery_id = (
                    "uncertain",
                    "Execution could not be confirmed; inspect before retrying",
                    "",
                )

            # Advance recurrence
            rule = json.loads(task["schedule"])
            following = compute_next_run(due_dt, rule, task["timezone"], clock=clock)

            with self.ws.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "UPDATE task_executions SET status=?, completed_at=?, result="
                    "?, delivery_id=? WHERE occurrence_id=?",
                    (status, now(), result_text, delivery_id, occurrence_id),
                )
                c.execute(
                    "UPDATE scheduled_tasks SET next_run_at=?, status=?, updated_"
                    "at=?, last_result=? WHERE task_id=?",
                    (
                        (following or due_dt).isoformat(),
                        "scheduled"
                        if following and status == "queued"
                        else "completed"
                        if status == "queued"
                        else status,
                        now(),
                        result_text,
                        task_id,
                    ),
                )

            executed_occurrence_ids.append(occurrence_id)

        return executed_occurrence_ids

    def _execute_task_action(
        self, task: dict[str, Any], occurrence_id: str
    ) -> tuple[str, str, str]:
        """Execute the specific registered task handler."""
        task_type = task["task_type"]
        args = json.loads(task["validated_arguments"])
        destination = task["destination"]
        creator_id = task["creator_user_id"]

        if task_type in {"reminder", "message"}:
            body_text = args.get("message") or args.get("content") or args.get("text") or ""
            prefix = "🔔 *Reminder:* " if task_type == "reminder" else ""
            full_text = f"{prefix}{body_text}".strip()
            delivery_id = f"notif_{occurrence_id}"

            with self.ws.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT OR IGNORE INTO notifications(id, reminder_id, chat, b"
                    "ody, created, state) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (delivery_id, task["task_id"], destination, full_text, now()),
                )
                c.execute(
                    "INSERT OR IGNORE INTO outbound_metadata(notification_id, sen"
                    "der, kind, approval_id) "
                    "VALUES (?, ?, 'reminder', '')",
                    (delivery_id, creator_id),
                )
            return "queued", f"Enqueued to notifications: {delivery_id}", delivery_id

        elif task_type == "summary":
            # Generate project summary and deliver to destination
            with self.ws.db.connect() as c:
                wps = c.execute("SELECT name, status, notes FROM work_packages").fetchall()
                mems = c.execute(
                    "SELECT subject, body FROM project_memories WHERE status='act"
                    "ive' ORDER BY created DESC LIMIT 5"
                ).fetchall()
            lines = ["📋 *Project Status Summary:*\n*Work Packages:*"]
            for wp in wps:
                lines.append(f"- *{wp['name']}*: {wp['status']} ({wp['notes']})")
            if mems:
                lines.append("\n*Recent Decisions:*")
                for m in mems:
                    lines.append(f"- {m['subject']}: {m['body']}")
            full_text = "\n".join(lines)
            delivery_id = f"notif_{occurrence_id}"

            with self.ws.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT OR IGNORE INTO notifications(id, reminder_id, chat, b"
                    "ody, created, state) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (delivery_id, task["task_id"], destination, full_text, now()),
                )
                c.execute(
                    "INSERT OR IGNORE INTO outbound_metadata(notification_id, sen"
                    "der, kind, approval_id) "
                    "VALUES (?, ?, 'summary', '')",
                    (delivery_id, creator_id),
                )
            return "queued", "Summary generated and enqueued", delivery_id

        return "failed", "This scheduled operation has no implemented handler", ""
