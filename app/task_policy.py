"""Live policy checks shared by scheduling, execution, and delivery."""

import hashlib
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

from app.domain import User


class TaskPolicy:
    def creator(self, task):
        with self.ws.db.connect() as c:
            row = c.execute(
                "SELECT id,name FROM users WHERE id=?", (task["creator_user_id"],)
            ).fetchone()
        if not row:
            raise PermissionError("Creator account is missing")
        chat = task["origin_chat"]
        channel = (
            "group"
            if chat.endswith("@g.us")
            else "dm"
            if chat.endswith("@s.whatsapp.net")
            else "web"
        )
        return self.ws.actor(User(row["id"], row["name"]), chat, channel)

    def authorize(self, actor, kind, destination, approval=None):
        from app.actions import Actions

        policy = Actions(SimpleNamespace(ws=self.ws))
        actor = policy.fresh(actor)
        if kind not in {"reminder", "message", "summary"}:
            raise ValueError("Scheduled calendar and Drive report execution is not implemented yet")
        caps = ["reminders"] + (["send_message"] if kind in {"message", "summary"} else [])
        candidates = None
        for cap in caps:
            required = policy.check(actor, cap)
            if required and actor.phone not in required:
                candidates = set(required) if candidates is None else candidates & set(required)
        if candidates is not None and not candidates:
            raise PermissionError(
                "These capability rules need different approvers; adjust the rules first"
            )
        if destination.endswith("@g.us"):
            if destination not in self.ws.policy()["groups"]:
                raise PermissionError("Destination group is disabled or unregistered")
        else:
            match = re.fullmatch(r"([1-9][0-9]{7,14})@s\.whatsapp\.net", destination)
            if not match or self.ws.role(match[1]) == "guest" or not self.ws.can_access(match[1]):
                raise PermissionError("Destination must be an approved member or registered group")
        effective = []
        with self.ws.db.connect() as c:
            for cap in caps:
                row = c.execute(
                    "SELECT * FROM capability_rules WHERE capability=? AND subject IN (?, '*') "
                    "ORDER BY CASE WHEN subject=? THEN 0 ELSE 1 END LIMIT 1",
                    (cap, actor.phone, actor.phone),
                ).fetchone()
                effective.append(dict(row) if row else {"capability": cap, "default": True})
        signature = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
        if approval is not None and candidates:
            try:
                saved = json.loads(approval)
            except (ValueError, TypeError):
                saved = {}
            if saved.get("approver") not in candidates or saved.get("policy") != signature:
                raise PermissionError("Current permission rules require a fresh approval")
            approver = saved["approver"]
            if not self.ws.can_access(approver):
                raise PermissionError("Approver access was revoked")
        return caps, sorted(candidates or []), signature

    def approve(self, task_id, requester, approver):
        task = self.get_task(task_id)
        if (
            not task
            or task["creator_user_id"] != requester.user.id
            or task["status"] != "awaiting_approval"
        ):
            raise ValueError("Task is no longer awaiting this approval")
        if datetime.fromisoformat(task["next_run_at"]) <= datetime.now(UTC):
            raise ValueError(
                "The scheduled time has passed; request a new preview and confirmation"
            )
        _, people, signature = self.authorize(requester, task["task_type"], task["destination"])
        if people and approver not in people:
            raise PermissionError("Not a current approver for all required capabilities")
        from app.workspace import now

        with self.ws.db.connect() as c:
            c.execute(
                "UPDATE scheduled_tasks SET approval_state=?,status='scheduled',updated_at=? "
                "WHERE task_id=? AND status='awaiting_approval'",
                (json.dumps({"approver": approver, "policy": signature}), now(), task_id),
            )
        return "Scheduled task approved: " + task_id

    def delivery_allowed(self, task_id):
        task = self.get_task(task_id)
        if not task or task["status"] not in {"scheduled", "completed"}:
            return False
        try:
            self.authorize(
                self.creator(task), task["task_type"], task["destination"], task["approval_state"]
            )
            return True
        except (ValueError, PermissionError):
            return False
