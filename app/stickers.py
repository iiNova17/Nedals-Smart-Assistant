"""Local reusable sticker storage with deduplication and visibility controls."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.domain import User
from app.workspace import Actor, Workspace, current_request, now

MAX_STICKER_SIZE_BYTES = 1024 * 1024  # 1 MiB


class StickerManager:
    """Manage reusable stickers with hashing, metadata, and visibility controls."""

    def __init__(self, workspace: Workspace, storage_dir: Path | str, drive: Any = None):
        self.ws = workspace
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.drive = drive

    def _authorize(self, actor):
        from app.actions import Actions

        policy = Actions(SimpleNamespace(ws=self.ws))
        actor = policy.fresh(actor)
        approvers = policy.check(actor, "stickers")
        if approvers and actor.phone not in approvers:
            raise PermissionError("Sticker approval workflows are not implemented; action blocked")
        return actor

    def save_sticker(
        self,
        actor: Actor,
        file_path_or_bytes: Path | str | bytes,
        name: str,
        description: str = "",
        usage_guidance: str = "",
        visibility: str = "shared",
        mime_type: str = "image/webp",
        is_animated: bool = False,
    ) -> dict[str, Any]:
        """Validate, hash, deduplicate and store a sticker."""
        actor = self._authorize(actor)
        if not self.ws.can_access(actor.phone, actor.chat, actor.channel):
            raise PermissionError("Access disabled")

        if visibility == "shared" and not actor.trusted:
            raise PermissionError("Only trusted members and admins can save shared stickers")

        if visibility not in {"shared", "personal"}:
            raise ValueError("Visibility must be shared or personal")
        # Read bytes
        if isinstance(file_path_or_bytes, bytes):
            data = file_path_or_bytes
        else:
            p = Path(file_path_or_bytes)
            if not p.is_file():
                raise FileNotFoundError(f"Sticker file not found: {p}")
            if p.stat().st_size > MAX_STICKER_SIZE_BYTES:
                raise ValueError("Sticker exceeds 1 MiB")
            data = p.read_bytes()

        if len(data) > MAX_STICKER_SIZE_BYTES:
            raise ValueError(f"Sticker exceeds 1 MiB limit ({len(data)} bytes)")

        if (
            len(data) < 20
            or data[:4] != b"RIFF"
            or data[8:12] != b"WEBP"
            or int.from_bytes(data[4:8], "little") + 8 != len(data)
        ):
            raise ValueError("Sticker must be a valid WebP container")
        mime_type = "image/webp"
        sha256 = hashlib.sha256(data).hexdigest()
        dest_filename = f"{sha256}.webp"
        local_dest = self.storage_dir / dest_filename
        if not local_dest.exists():
            local_dest.write_bytes(data)

        clean_name = name.strip() or "Sticker"
        slug = re.sub(r"[^a-z0-9]+", "_", clean_name.lower()).strip("_")[:20]
        sticker_id = f"stk_{slug}_{sha256[:6]}" if slug else f"stk_{sha256[:8]}"

        drive_file_id = ""

        with self.ws.db.connect() as c:
            # Check deduplication
            existing = c.execute("SELECT * FROM stickers WHERE sha256=?", (sha256,)).fetchone()
            if existing:
                if (
                    existing["creator_user_id"] != actor.user.id
                    and existing["visibility"] != "shared"
                ):
                    raise PermissionError(
                        "An identical asset is private; its metadata cannot be reused"
                    )
                return {
                    "id": existing["id"],
                    "name": existing["name"],
                    "sha256": sha256,
                    "usage_guidance": existing["usage_guidance"],
                    "deduplicated": True,
                }

            c.execute(
                "INSERT INTO stickers("
                "id, name, description, usage_guidance, creator_user_id, visibility, "
                "sha256, storage_path, drive_file_id, mime_type, is_animated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sticker_id,
                    clean_name,
                    description,
                    usage_guidance,
                    actor.user.id,
                    visibility,
                    sha256,
                    str(local_dest),
                    drive_file_id,
                    mime_type,
                    int(is_animated),
                    now(),
                ),
            )

        return {
            "id": sticker_id,
            "name": clean_name,
            "sha256": sha256,
            "usage_guidance": usage_guidance,
            "deduplicated": False,
        }

    def list_stickers(self, actor: Actor, query: str = "") -> list[dict[str, Any]]:
        """List stickers accessible to this actor."""
        actor = self._authorize(actor)
        if not self.ws.can_access(actor.phone, actor.chat, actor.channel):
            raise PermissionError("Access disabled")
        with self.ws.db.connect() as c:
            sql = (
                "SELECT id,name,description,usage_guidance,creator_user_id,vi"
                "sibility FROM stickers WHERE (visibility='shared' OR creator"
                "_user_id=?)"
            )
            params: list[Any] = [actor.user.id]
            if actor.channel == "group":
                sql += " AND visibility='shared'"
            if query:
                sql += (
                    " AND (instr(lower(name), lower(?))>0 OR instr(lower(usage_gu"
                    "idance), lower(?))>0)"
                )
                params.extend([query, query])
            sql += " ORDER BY created_at DESC LIMIT 50"
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def get_sticker(self, sticker_id: str) -> dict[str, Any] | None:
        """Fetch sticker by ID."""
        with self.ws.db.connect() as c:
            row = c.execute("SELECT * FROM stickers WHERE id=?", (sticker_id,)).fetchone()
            return dict(row) if row else None

    def _send_bytes(self, actor, sticker_id):
        actor = self._authorize(actor)
        if actor.channel not in {"group", "dm"}:
            raise ValueError("Send stickers from a WhatsApp chat")
        if actor.channel == "group":
            if actor.chat not in self.ws.policy()["groups"]:
                raise PermissionError("Destination group is disabled or unregistered")
        elif actor.chat != actor.phone + "@s.whatsapp.net" and not re.fullmatch(
            r"[0-9]{5,25}@lid", actor.chat
        ):
            raise PermissionError("Sticker replies must stay in the current conversation")
        sticker = self.get_sticker(sticker_id)
        if not sticker or (
            sticker["visibility"] != "shared"
            and (sticker["creator_user_id"] != actor.user.id or actor.channel == "group")
        ):
            raise PermissionError("Sticker is unavailable in this conversation")
        # Resolve from the content hash, never a model-supplied or persisted arbitrary path.
        digest = sticker["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid sticker reference")
        root = self.storage_dir.resolve()
        path = (root / (digest + ".webp")).resolve()
        if path.parent != root or not path.is_file():
            raise ValueError("Sticker media is no longer available")
        if path.stat().st_size > MAX_STICKER_SIZE_BYTES:
            raise ValueError("Sticker exceeds 1 MiB")
        data = path.read_bytes()
        if (
            len(data) < 20
            or len(data) > MAX_STICKER_SIZE_BYTES
            or data[:4] != b"RIFF"
            or data[8:12] != b"WEBP"
            or int.from_bytes(data[4:8], "little") + 8 != len(data)
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("Sticker media is corrupt or changed")
        return data

    def queue_send(self, actor, sticker_id):
        self._send_bytes(actor, sticker_id)
        request = current_request.get() or uuid4().hex
        key = (
            "sticker_"
            + hashlib.sha256(
                f"{actor.user.id}:{actor.chat}:{request}:{sticker_id}".encode()
            ).hexdigest()
        )
        with self.ws.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,'pending')",
                (key, "", actor.chat, "[Sticker]", now()),
            )
            c.execute(
                "INSERT OR IGNORE INTO outbound_metadata VALUES (?,?,'sticker','')",
                (key, actor.user.id),
            )
            c.execute("INSERT OR IGNORE INTO outbound_stickers VALUES (?,?)", (key, sticker_id))
            state = c.execute("SELECT state FROM notifications WHERE id=?", (key,)).fetchone()[0]
        return {"status": state, "delivery_id": key, "media_type": "sticker"}

    def delivery_bytes(self, notification_id):
        if self.ws.get("paused", False):
            raise PermissionError("Assistant is paused")
        with self.ws.db.connect() as c:
            row = c.execute(
                "SELECT n.chat,m.sender,s.sticker_id,u.name FROM notifications n "
                "JOIN outbound_metadata m ON m.notification_id=n.id "
                "JOIN outbound_stickers s ON s.notification_id=n.id "
                "JOIN users u ON u.id=m.sender WHERE n.id=? "
                "AND n.state IN ('pending','queued') AND m.kind='sticker'",
                (notification_id,),
            ).fetchone()
        if not row:
            raise PermissionError("Sticker delivery is unavailable")
        actor = self.ws.actor(
            User(row["sender"], row["name"]),
            row["chat"],
            "group" if row["chat"].endswith("@g.us") else "dm",
        )
        return self._send_bytes(actor, row["sticker_id"])

    def manage_sticker(
        self, actor: Actor, action: str, sticker_id: str, **kwargs
    ) -> tuple[bool, str]:
        """Update or remove a sticker."""
        actor = self._authorize(actor)
        if not self.ws.can_access(actor.phone, actor.chat, actor.channel):
            raise PermissionError("Access disabled")
        stk = self.get_sticker(sticker_id)
        if not stk:
            return False, f"Sticker {sticker_id} not found"

        if stk["creator_user_id"] != actor.user.id and not actor.admin:
            raise PermissionError("Only creator or admin can manage this sticker")

        with self.ws.db.connect() as c:
            if action == "delete":
                c.execute("DELETE FROM stickers WHERE id=?", (sticker_id,))
                p = Path(stk["storage_path"])
                if p.is_file():
                    p.unlink(missing_ok=True)
                return True, f"Sticker {sticker_id} deleted"
            elif action == "update":
                name = kwargs.get("name") or stk["name"]
                guidance = (
                    kwargs.get("usage_guidance")
                    if kwargs.get("usage_guidance") is not None
                    else stk["usage_guidance"]
                )
                visibility = kwargs.get("visibility") or stk["visibility"]
                if visibility not in {"shared", "personal"}:
                    raise ValueError("Invalid visibility")
                c.execute(
                    "UPDATE stickers SET name=?, usage_guidance=?, visibility=? WHERE id=?",
                    (name, guidance, visibility, sticker_id),
                )
                return True, f"Sticker {sticker_id} updated"

        return False, f"Unknown action: {action}"
