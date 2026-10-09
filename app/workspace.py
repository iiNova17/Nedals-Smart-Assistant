"""Persistent policy and project records. The LLM cannot grant itself permissions."""

import json
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.domain import User


@dataclass(frozen=True)
class Actor:
    user: User
    phone: str = ""
    chat: str = ""
    channel: str = "web"
    admin: bool = False
    trusted: bool = True


current_actor: ContextVar[Actor | None] = ContextVar("actor", default=None)
current_attachment: ContextVar[str] = ContextVar("attachment", default="")
current_request: ContextVar[str] = ContextVar("request", default="")
current_image_hash: ContextVar[str] = ContextVar("image_hash", default="")


def now():
    return datetime.now(UTC).isoformat()


class Workspace:
    def __init__(self, db):
        self.db = db

    def initialize(self):
        with self.db.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS capability_rules(
                    capability TEXT NOT NULL,subject TEXT NOT NULL,effect TEXT NOT NULL,
                    approvers TEXT NOT NULL,updated TEXT NOT NULL,
                    PRIMARY KEY(capability,subject));
                CREATE TABLE IF NOT EXISTS action_requests(
                    id TEXT PRIMARY KEY,requester TEXT NOT NULL,chat TEXT NOT NULL,
                    channel TEXT NOT NULL,command TEXT NOT NULL,capability TEXT NOT NULL,
                    approvers TEXT NOT NULL,created TEXT NOT NULL,state TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS outbound_metadata(
                    notification_id TEXT PRIMARY KEY,sender TEXT NOT NULL,
                    kind TEXT NOT NULL,approval_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS outbound_stickers(
                    notification_id TEXT PRIMARY KEY,sticker_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbound_mentions(
                    notification_id TEXT PRIMARY KEY,jids TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_sources(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                    owner TEXT NOT NULL,created TEXT NOT NULL,status TEXT NOT NULL,
                    replaces TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS schedule_source_proposals(
                    id TEXT PRIMARY KEY,requester TEXT NOT NULL,chat TEXT NOT NULL,
                    payload TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,
                    expires TEXT NOT NULL,request_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_source_checks(
                    source_id TEXT PRIMARY KEY,checked_at TEXT NOT NULL,status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_sessions(
                    token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS access_members(
                    phone TEXT PRIMARY KEY,allowed INTEGER NOT NULL DEFAULT 1,
                    blocked INTEGER NOT NULL DEFAULT 0,admin INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS registered_groups(
                    chat TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS member_profiles(
                    user_id TEXT PRIMARY KEY,name TEXT NOT NULL,project_role TEXT NOT NULL
                DEFAULT '',
                    duties TEXT NOT NULL DEFAULT '',timezone TEXT NOT NULL DEFAULT 'Africa/Cairo',
                    complete_until TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS project_memories(
                    id TEXT PRIMARY KEY,subject TEXT NOT NULL,body TEXT NOT NULL,author TEXT
                NOT NULL,
                    created TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',
                    supersedes TEXT NOT NULL DEFAULT '',source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS busy_slots(
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
                    weekdays TEXT NOT NULL DEFAULT '',timezone TEXT NOT NULL,label TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calendar_links(
                    user_id TEXT PRIMARY KEY,calendar_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reminders(
                    id TEXT PRIMARY KEY,chat TEXT NOT NULL,body TEXT NOT NULL,next_due TEXT
                NOT NULL,
                    recurrence TEXT NOT NULL,timezone TEXT NOT NULL,creator TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY,reminder_id TEXT NOT NULL,chat TEXT NOT NULL,
                    body TEXT NOT NULL,created TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS admin_audit(
                    id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,created
                TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending_actions(
                    id TEXT PRIMARY KEY,actor TEXT NOT NULL,chat TEXT NOT NULL,command TEXT
                NOT NULL,
                    created TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS command_receipts(
                    id TEXT PRIMARY KEY,response TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_packages(
                    name TEXT PRIMARY KEY,status TEXT NOT NULL,notes TEXT NOT NULL,
                    updated TEXT NOT NULL,author TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_package_history(
                    id INTEGER PRIMARY KEY,name TEXT NOT NULL,old_status TEXT,new_status TEXT
                NOT NULL,
                    notes TEXT NOT NULL,updated TEXT NOT NULL,author TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scheduled_tasks(
                    task_id TEXT PRIMARY KEY,creator_user_id TEXT NOT NULL,
                    origin_chat TEXT NOT NULL,destination TEXT NOT NULL,
                    task_type TEXT NOT NULL,validated_arguments TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Africa/Cairo',
                    schedule TEXT NOT NULL,next_run_at TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    approval_state TEXT NOT NULL DEFAULT 'approved',
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    last_result TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS task_proposals(
                    proposal_id TEXT PRIMARY KEY,creator_user_id TEXT NOT NULL,
                    origin_chat TEXT NOT NULL,task_type TEXT NOT NULL,
                    destination TEXT NOT NULL,validated_arguments TEXT NOT NULL,
                    schedule TEXT NOT NULL,timezone TEXT NOT NULL,
                    summary TEXT NOT NULL,created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS task_executions(
                    occurrence_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',
                    delivery_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS stickers(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
                    usage_guidance TEXT NOT NULL DEFAULT '',creator_user_id TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'shared',sha256 TEXT NOT NULL UNIQUE,
                    storage_path TEXT NOT NULL,drive_file_id TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT 'image/webp',
                    is_animated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL);
                INSERT OR IGNORE INTO access_members(phone,allowed)
                    SELECT phone,1 FROM whatsapp_members;
            """)

    def get(self, key, default=None):
        with self.db.connect() as c:
            r = c.execute("SELECT value FROM runtime_config WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO runtime_config VALUES (?,?)", (key, json.dumps(value))
            )

    def owner_phone(self):
        with self.db.connect() as c:
            r = c.execute("SELECT phone FROM whatsapp_members WHERE role='owner'").fetchone()
        return r[0] if r else ""

    def role(self, phone):
        if phone and phone == self.owner_phone():
            return "owner"
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM access_members WHERE phone=?", (phone,)).fetchone()
        if not r or r["blocked"] or not r["allowed"]:
            return "guest"
        return "admin" if r["admin"] else "member"

    def is_name_known(self, actor: Actor | None) -> bool:
        if not actor or not actor.phone:
            return bool(
                actor
                and actor.user
                and not actor.user.name.startswith("Visitor ")
                and actor.user.name != "User"
            )
        role = self.role(actor.phone)
        if role == "guest":
            return False
        with self.db.connect() as c:
            r = c.execute(
                "SELECT u.name FROM users u JOIN whatsapp_members m ON u.id=m.user_id "
                "WHERE m.phone=? AND u.active=1",
                (actor.phone,),
            ).fetchone()
            if r and not r[0].startswith("Visitor ") and r[0] != "User":
                return True
        return False

    def is_creator(self, actor: Actor | None) -> bool:
        return bool(actor and actor.phone and actor.phone == self.owner_phone())

    def speaker_record(self, actor: Actor | None, timezone: str = "Africa/Cairo") -> dict:
        is_creator = self.is_creator(actor)
        name_known = self.is_name_known(actor)
        speaker_name = actor.user.name if (actor and name_known) else None
        role = (
            "owner"
            if is_creator
            else (
                "admin"
                if (actor and actor.admin)
                else ("member" if (actor and actor.trusted) else "guest")
            )
        )
        return {
            "name": speaker_name,
            "name_known": name_known,
            "role": role,
            "is_creator": is_creator,
            "is_admin": bool(actor and actor.admin),
            "is_trusted_member": bool(actor and actor.trusted),
            "phone": actor.phone if actor else "",
            "chat": actor.chat if actor else "",
            "channel": actor.channel if actor else "web",
            "timezone": timezone,
        }

    def policy(self):
        with self.db.connect() as c:
            members = [
                {**dict(r), "blocked": int(bool(r["blocked"] or not r["active"]))}
                for r in c.execute(
                    "SELECT a.*,coalesce(u.active,1) AS active FROM access_members a "
                    "LEFT JOIN whatsapp_members "
                    "m ON m.phone=a.phone "
                    "LEFT JOIN users u ON u.id=m.user_id"
                )
            ]
            groups = [r[0] for r in c.execute("SELECT chat FROM registered_groups WHERE enabled=1")]
        return {
            "mode": self.get("mode", "whitelist"),
            "owner": self.owner_phone(),
            "paused": self.get("paused", False),
            "members": members,
            "groups": groups,
            "group_trigger": self.get("group_trigger", "mention"),
        }

    def can_access(self, phone, chat="", channel="dm"):
        if phone == self.owner_phone() and phone:
            return True
        policy = self.policy()
        if policy["paused"]:
            return False
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM access_members WHERE phone=?", (phone,)).fetchone()
            inactive = c.execute(
                "SELECT 1 FROM whatsapp_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.phone=? AND u.active=0",
                (phone,),
            ).fetchone()
        if (r and r["blocked"]) or inactive or policy["mode"] == "owner":
            return False
        if policy["mode"] == "public":
            return True
        return bool(r and r["allowed"] and (channel != "group" or chat in policy["groups"]))

    def actor(self, user, chat="", channel="web"):
        with self.db.connect() as c:
            r = c.execute(
                "SELECT phone FROM whatsapp_members WHERE user_id=?", (user.id,)
            ).fetchone()
        phone = r[0] if r else ""
        role = self.role(phone)
        return Actor(
            user, phone, chat, channel, role in {"owner", "admin"}, role != "guest" or not phone
        )

    def authorize(self, phone, chat, channel):
        if not self.can_access(phone, chat, channel):
            return None
        user = self.db.whatsapp_user(phone)
        if not user:
            # Public visitors are recorded separately from the whitelist.
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                user = User(str(uuid4()), "Visitor " + phone[-4:])
                c.execute(
                    "INSERT INTO users(id,name,token_hash) VALUES (?,?,?)",
                    (user.id, user.name, self.db.token_hash(secrets.token_urlsafe(32))),
                )
                c.execute(
                    "INSERT OR IGNORE INTO access_members(phone,allowed) VALUES (?,0)", (phone,)
                )
                c.execute("INSERT INTO whatsapp_members VALUES (?,?,'member')", (phone, user.id))
        return self.actor(user, chat, channel)

    def audit(self, actor, action):
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO admin_audit(actor,action,created) VALUES (?,?,?)",
                (actor.user.id, action[:1000], now()),
            )

    def member_change(self, actor, phone, action, name="Member"):
        if not actor.admin:
            raise PermissionError("Admin only")
        phone = phone.lstrip("+")
        if not re.fullmatch(r"[1-9][0-9]{7,14}", phone):
            raise ValueError("Use a full phone number with country code")
        if phone == self.owner_phone() and action != "approve":
            raise PermissionError("The permanent owner cannot be blocked, demoted or removed")
        if action in {"promote", "demote"} and actor.phone != self.owner_phone():
            raise PermissionError("Only the permanent owner can appoint or remove admins")
        if action == "approve":
            self.db.approve_whatsapp_member(phone, name)
        with self.db.connect() as c:
            c.execute("INSERT OR IGNORE INTO access_members(phone,allowed) VALUES (?,0)", (phone,))
            updates = {
                "approve": "allowed=1,blocked=0",
                "remove": "allowed=0,admin=0",
                "block": "blocked=1,admin=0",
                "unblock": "blocked=0",
                "promote": "admin=1,allowed=1",
                "demote": "admin=0",
            }
            if action not in updates:
                raise ValueError("Unknown member action")
            if action == "promote" and not self.db.whatsapp_user(phone):
                raise ValueError("Approve this person first")
            c.execute(f"UPDATE access_members SET {updates[action]} WHERE phone=?", (phone,))
        self.audit(actor, f"member {action} ...{phone[-4:]}")

    def memories(self, query="", status="active"):
        with self.db.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM project_memories WHERE status=? AND "
                    "(instr(lower(subject),lower(?))>0 OR instr(lower(body),lower(?))>0) "
                    "ORDER BY created DESC LIMIT 50",
                    (status, query, query),
                )
            ]

    def project_status(self):
        with self.db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM work_packages ORDER BY name")]

    def update_work_package(self, actor, name, status, notes):
        if not actor.admin:
            raise PermissionError("Admin only")
        if status not in {
            "active",
            "planned",
            "awaiting planning",
            "proposed",
            "completed",
            "blocked",
        }:
            raise ValueError("Unknown work-package status")
        if not 1 <= len(name) <= 100 or len(notes) > 2000:
            raise ValueError("Invalid work package name/notes")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT status FROM work_packages WHERE name=?", (name,)).fetchone()
            c.execute(
                "INSERT OR REPLACE INTO work_packages VALUES (?,?,?,?,?)",
                (name, status, notes, now(), actor.user.id),
            )
            c.execute(
                "INSERT INTO "
                "work_package_history(name,old_status,new_status,notes,updated,author) "
                "VALUES (?,?,?,?,?,?)",
                (name, old[0] if old else None, status, notes, now(), actor.user.id),
            )

    def remember(self, actor, subject, body, supersedes=""):
        if not actor.trusted:
            raise PermissionError("Only approved members can save shared project memory")
        if not subject.strip() or not body.strip() or len(body) > 6000:
            raise ValueError("Memory needs a subject and 1–6000 characters")
        key = uuid4().hex[:12]
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if supersedes:
                old = c.execute(
                    "SELECT * FROM project_memories WHERE id=?", (supersedes,)
                ).fetchone()
                if not old or (old["author"] != actor.user.id and not actor.admin):
                    raise PermissionError("Only its author or an admin can replace a decision")
                c.execute(
                    "UPDATE project_memories SET status='superseded' WHERE id=?", (supersedes,)
                )
            c.execute(
                "INSERT INTO project_memories VALUES (?,?,?,?,?,'active',?,?)",
                (
                    key,
                    subject[:200],
                    body,
                    actor.user.id,
                    now(),
                    supersedes,
                    actor.channel + ":" + actor.chat,
                ),
            )
        return key
