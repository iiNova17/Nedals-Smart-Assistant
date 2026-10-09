"""Trusted local bridge ingress. All user and group access is checked again here."""

import asyncio
import json
import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import Assistant
from app.audio import GeminiAudioTranscriber, process_audio_file
from app.config import Settings, load_team
from app.db import Database
from app.domain import Completion, ProviderUnavailable, QuotaExceeded
from app.drive import DriveError
from app.images import load_image
from app.ingestion import Ingestion
from app.workspace import Workspace, current_actor, current_attachment


class ReplyContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: str = Field(default="", max_length=250)
    chat_id: str = Field(default="", max_length=150)
    author_identifier: str = Field(default="", max_length=150)
    resolved_author_name: str = Field(default="", max_length=150)
    content_type: str = Field(default="text", max_length=50)
    text_or_caption: str = Field(default="", max_length=8000)
    attachment_reference: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    unavailable: bool = False
    media_error: str = Field(default="", max_length=50)


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=250)
    sender_phone: str = Field(pattern=r"^[1-9][0-9]{7,14}$")
    chat_id: str = Field(min_length=1, max_length=150)
    channel: Literal["group", "dm"]
    text: str = Field(default="", max_length=8000)
    kind: Literal["text", "document", "audio", "sticker", "image"] = "text"
    file_token: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    filename: str = Field(default="attachment", min_length=1, max_length=200)
    media_error: Literal["", "too_large", "download_failed", "spool_full", "expired"] = ""
    reply_context: ReplyContext | None = None


class GroupInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chat: str = Field(pattern=r"^[0-9-]+@g\.us$", max_length=150)
    name: str = Field(default="", max_length=150)


class GroupSyncRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    groups: list[GroupInfo] = Field(max_length=1000)


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

    @routes.post("/groups/sync", dependencies=[Depends(bridge_auth)])
    def sync_groups(body: GroupSyncRequest):
        synced = 0
        with db.connect() as connection:
            for g in body.groups:
                name = g.name.strip()
                if not name or name.lower() in {"current group name", "this group"}:
                    continue
                connection.execute(
                    "INSERT INTO registered_groups(chat, name, enabled) VALUES (?, ?, 0) "
                    "ON CONFLICT(chat) DO UPDATE SET name = excluded.name",
                    (g.chat, name[:100]),
                )
                synced += 1
        return {"status": "ok", "synced": synced}

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
                    "SELECT n.*,COALESCE(m.kind,'text') AS media_kind FROM notifications n "
                    "LEFT JOIN outbound_metadata m ON m.notification_id=n.id "
                    "WHERE n.state IN ('pending','queued') ORDER BY n.created LIMIT 10"
                )
            ]
            for row in rows:
                mentions = c.execute(
                    "SELECT jids FROM outbound_mentions WHERE notification_id=?", (row["id"],)
                ).fetchone()
                row["mentions"] = json.loads(mentions[0]) if mentions else []
        return {"notifications": rows}

    @routes.get("/notifications/{key}/media", dependencies=[Depends(bridge_auth)])
    def notification_media(key: str):
        if not assistant.commands:
            raise HTTPException(404, "Sticker unavailable")
        try:
            data = assistant.commands.stickers.delivery_bytes(key)
        except (ValueError, PermissionError, OSError):
            raise HTTPException(403, "Sticker delivery unavailable") from None
        return Response(data, media_type="image/webp", headers={"Cache-Control": "no-store"})

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
        if body.reply_context and body.reply_context.chat_id != body.chat_id:
            raise HTTPException(422, "Quoted context must belong to the current chat")
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
                started = time.monotonic()
                image_reference = (
                    body.reply_context
                    if (body.reply_context and body.reply_context.content_type == "image")
                    else None
                )
                images = ()
                if body.kind == "image" or image_reference:
                    image_token = (
                        body.file_token
                        if body.kind == "image"
                        else (image_reference.attachment_reference)
                    )
                    media_error = (
                        body.media_error
                        if body.kind == "image"
                        else (image_reference.media_error or image_reference.unavailable)
                    )
                    if media_error or not image_token:
                        return {
                            "text": "I couldn't download that image. Please resend it "
                            "with your question, or reply to it and mention me."
                        }
                    try:
                        image = await asyncio.to_thread(
                            load_image, settings.incoming_path / (image_token + ".bin")
                        )
                    except ValueError as error:
                        return {"text": str(error)}
                    images = (image,)
                    body.text = body.text.strip() or "Describe this image."
                audio_request = body.kind == "audio" or bool(
                    body.reply_context and body.reply_context.content_type == "audio"
                )
                if audio_request:
                    try:
                        db.reserve_request(
                            user.id,
                            workspace.get("daily_team_requests", settings.daily_team_requests),
                            workspace.get("daily_user_requests", settings.daily_user_requests),
                        )
                    except QuotaExceeded:
                        raise HTTPException(429, "Daily usage limit reached") from None
                if body.kind == "audio":
                    if body.media_error:
                        text = {
                            "too_large": "Voice note exceeds 16 MiB limit.",
                            "download_failed": "Voice note download failed. Please resend it.",
                            "spool_full": "Temporary storage is full. Please try again later.",
                            "expired": "Voice note is expired or unavailable. Please resend it.",
                        }.get(
                            body.media_error, "Voice note could not be retrieved. Please resend it."
                        )
                        return {"text": text}
                    audio_path = settings.incoming_path / (body.file_token + ".bin")
                    transcriber = (
                        GeminiAudioTranscriber(assistant.provider)
                        if hasattr(assistant.provider, "_generate_with_rotation")
                        else None
                    )
                    if transcriber and audio_path.is_file():
                        try:
                            transcription = await asyncio.wait_for(
                                process_audio_file(transcriber, audio_path), 30
                            )
                            body.text = transcription or "[Voice note was inaudible]"
                        except Exception:
                            return {
                                "text": (
                                    "I could not transcribe the voice note. Please resend it or t"
                                    "ype your message."
                                )
                            }
                    else:
                        return {"text": "Voice note understanding is currently unavailable."}
                elif body.kind == "sticker":
                    body.text = body.text or "[Sticker received]"

                if body.reply_context:
                    body.reply_context.resolved_author_name = ""
                if body.reply_context and body.reply_context.author_identifier:
                    author_id = body.reply_context.author_identifier
                    if author_id == team.owner_phone:
                        body.reply_context.resolved_author_name = "Owner"
                    else:
                        author_user = db.whatsapp_user(author_id)
                        if author_user:
                            body.reply_context.resolved_author_name = author_user.name
                        else:
                            body.reply_context.resolved_author_name = ""

                if (
                    body.reply_context
                    and body.reply_context.content_type == "audio"
                    and body.reply_context.attachment_reference
                ):
                    quoted_audio_path = settings.incoming_path / (
                        body.reply_context.attachment_reference + ".bin"
                    )
                    transcriber = (
                        GeminiAudioTranscriber(assistant.provider)
                        if hasattr(assistant.provider, "_generate_with_rotation")
                        else None
                    )
                    if transcriber and quoted_audio_path.is_file():
                        try:
                            quoted_transcription = await asyncio.wait_for(
                                process_audio_file(transcriber, quoted_audio_path), 30
                            )
                            body.reply_context.text_or_caption = (
                                f"[Voice note transcription]: {quoted_transcription}"
                            )
                        except Exception:
                            body.reply_context.unavailable = True
                            body.reply_context.media_error = "download_failed"

                try:
                    token = current_actor.set(actor)
                    attachment = (
                        body.file_token if body.kind == "sticker" and not body.media_error else ""
                    )
                    if (
                        body.reply_context
                        and body.reply_context.content_type == "sticker"
                        and not body.reply_context.unavailable
                    ):
                        attachment = body.reply_context.attachment_reference
                    attachment_token = current_attachment.set(
                        str(settings.incoming_path / (attachment + ".bin")) if attachment else ""
                    )
                    try:
                        result = await asyncio.wait_for(
                            assistant.reply(
                                user,
                                cid,
                                body.text,
                                event_key,
                                reply_context=body.reply_context,
                                usage_reserved=audio_request,
                                images=images,
                            ),
                            max(1, 125 - (time.monotonic() - started)),
                        )
                    finally:
                        current_actor.reset(token)
                        current_attachment.reset(attachment_token)
                except QuotaExceeded:
                    raise HTTPException(429, "Daily usage limit reached") from None
                except PermissionError:
                    raise HTTPException(403, "Access revoked") from None
                except (ProviderUnavailable, TimeoutError):
                    raise HTTPException(503, "Assistant unavailable") from None
            return {"text": result.text}

    return routes
