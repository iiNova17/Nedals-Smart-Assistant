"""Trusted local bridge ingress. All user and group access is checked again here."""

import asyncio
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import Assistant
from app.config import Settings, load_team
from app.db import Database
from app.domain import Completion, ProviderUnavailable, QuotaExceeded
from app.drive import DriveError
from app.ingestion import Ingestion
from app.workspace import Workspace, current_actor


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=250)
    sender_phone: str = Field(pattern=r"^[1-9][0-9]{7,14}$")
    chat_id: str = Field(min_length=1, max_length=150)
    channel: Literal["group", "dm"]
    text: str = Field(default="", max_length=8000)
    kind: Literal["text", "document"] = "text"
    file_token: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    filename: str = Field(default="attachment", min_length=1, max_length=200)
    media_error: Literal["", "too_large", "download_failed", "spool_full"] = ""


def router(
    settings: Settings, db: Database, assistant: Assistant, ingestion: Ingestion | None = None
) -> APIRouter:
    routes = APIRouter(prefix="/internal/whatsapp")
    bearer = HTTPBearer(auto_error=False)
    workspace = Workspace(db)

    def bridge_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = settings.bridge_token.get_secret_value()
        if (
            not expected
            or not credentials
            or not secrets.compare_digest(expected, credentials.credentials)
        ):
            raise HTTPException(401, "Bridge authentication required")

    @routes.get("/members", dependencies=[Depends(bridge_auth)])
    def members():
        return {"members": db.whatsapp_members()}

    @routes.get("/policy", dependencies=[Depends(bridge_auth)])
    def policy():
        return workspace.policy()

    @routes.get("/notifications", dependencies=[Depends(bridge_auth)])
    def notifications():
        if workspace.get("paused", False):
            return {"notifications": []}
        with db.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM notifications WHERE state IN ('pending','queued') "
                    "ORDER BY created LIMIT 10"
                )
            ]
        return {"notifications": rows}

    class DeliveryState(BaseModel):
        state: Literal["queued", "sent", "uncertain", "cancelled"]

    @routes.get("/notifications/{key}", dependencies=[Depends(bridge_auth)])
    def notification_state(key: str):
        return {
            "allowed": bool(
                assistant.commands and assistant.commands.schedule.notification_allowed(key)
            )
        }

    @routes.post("/notifications/{key}", dependencies=[Depends(bridge_auth)])
    def delivery_state(key: str, body: DeliveryState):
        with db.connect() as c:
            c.execute(
                "UPDATE notifications SET state=? WHERE id=? AND state IN ('pending','queued')",
                (body.state, key),
            )
        return {"ok": True}

    @routes.post("/messages", dependencies=[Depends(bridge_auth)])
    async def incoming(body: IncomingMessage):
        team = load_team(settings)
        if team is None:
            raise HTTPException(403, "Team is not configured")
        actor = workspace.authorize(body.sender_phone, body.chat_id, body.channel)
        if actor is None:
            raise HTTPException(403, "Sender is not approved")
        user = actor.user
        if body.channel == "group":
            if (
                workspace.get("mode", "whitelist") != "public"
                and body.chat_id not in workspace.policy()["groups"]
                and not actor.admin
            ):
                raise HTTPException(403, "Register this group with an admin command first")
            # The owner can register a new group via an authenticated chat command.
            # Other groups require public mode or explicit group registration.
            scope = "group:" + body.chat_id
        else:
            # The bridge resolves WhatsApp LIDs. Canonical phone scoping prevents aliases
            # creating parallel contexts, and the caller cannot choose another user's DM.
            scope = "dm:" + body.sender_phone
        event_key = scope + ":" + body.sender_phone + ":" + body.event_id
        if not body.text.strip() and body.kind == "text":
            raise HTTPException(422, "Text must not be blank")
        if assistant.lock.locked():
            raise HTTPException(429, "Assistant busy", headers={"Retry-After": "3"})
        async with assistant.lock:
            if not db.is_active(user.id):
                raise HTTPException(403, "Access revoked")
            result = db.whatsapp_result(event_key)
            if result:
                return {"text": result.text}
            cid = db.whatsapp_conversation(user, scope)
            if body.kind == "document":
                text = (
                    "I received your document message. Google Drive upload and document "
                    "search are not connected yet, so I have not uploaded or indexed it."
                )
                if body.media_error:
                    text = {
                        "too_large": "File exceeds 25 MiB. It was not uploaded.",
                        "download_failed": "Attachment download failed. Please resend it.",
                        "spool_full": "Temporary file storage is full. Please try again later.",
                    }[body.media_error]
                elif ingestion and ingestion.drive.ready and body.file_token:
                    try:
                        text = await asyncio.to_thread(
                            ingestion.receive_bridge,
                            body.file_token,
                            body.filename,
                            user,
                            body.text,
                        )
                    except DriveError as error:
                        text = f"Upload could not be completed: {error}"
                    except Exception:
                        text = (
                            "Upload status could not be confirmed. Resend the file to retry safely."
                        )
                if not db.is_active(user.id):
                    raise HTTPException(403, "Access revoked")
                result = Completion(text, "local-status")
                db.save_turn(cid, "[Document received]", result, settings.context_turns, event_key)
            else:
                try:
                    token = current_actor.set(actor)
                    try:
                        result = await assistant.reply(user, cid, body.text, event_key)
                    finally:
                        current_actor.reset(token)
                except QuotaExceeded:
                    raise HTTPException(429, "Daily usage limit reached") from None
                except PermissionError:
                    raise HTTPException(403, "Access revoked") from None
                except ProviderUnavailable:
                    raise HTTPException(503, "Assistant unavailable") from None
            return {"text": result.text}

    return routes
