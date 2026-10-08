import asyncio
import logging
from uuid import uuid4

from app.config import Settings
from app.db import Database
from app.domain import Completion, Message, ProviderUnavailable, User
from app.providers.base import ChatProvider
from app.workspace import Workspace, current_actor, current_request

logger = logging.getLogger("plume")


class Assistant:
    def __init__(self, settings: Settings, db: Database, provider: ChatProvider):
        self.settings = settings
        self.db = db
        self.provider = provider
        self.workspace = Workspace(db)
        self.commands = None
        # One worker and one in-flight generation for the first increment.
        # Reject contention instead of building an unbounded in-memory queue.
        self.lock = asyncio.Lock()

    async def reply(
        self, user: User, conversation_id: str, text: str, event_id: str | None = None
    ) -> Completion:
        actor = current_actor.get() or self.workspace.actor(user, conversation_id, "web")
        actor_token = current_actor.set(actor)
        request_token = current_request.set(event_id or uuid4().hex)
        try:
            return await self._reply(actor, conversation_id, text, event_id)
        finally:
            current_actor.reset(actor_token)
            current_request.reset(request_token)

    async def _reply(self, actor, conversation_id, text, event_id):
        user = actor.user
        stored_text = f"[{user.name}] {text}" if actor.channel == "group" else text
        if self.commands:
            result = await asyncio.to_thread(
                self.commands.dispatch, actor, text, conversation_id, event_id or ""
            )
            if result is not None:
                completion = Completion(result, "local-command", evidence="authenticated_command")
                if not self.db.is_active(user.id):
                    raise PermissionError()
                self.db.save_turn(
                    conversation_id, stored_text, completion, self.settings.context_turns, event_id
                )
                return completion
        if not self.provider.ready:
            raise ProviderUnavailable()
        num_keys = max(1, len(getattr(self.provider, "_keys", [])))
        self.db.reserve_request(
            user.id,
            self.workspace.get("daily_team_requests", self.settings.daily_team_requests) * num_keys,
            self.workspace.get("daily_user_requests", self.settings.daily_user_requests) * num_keys,
        )
        self.db.cleanup(self.settings.context_days)
        history = self.db.history(
            conversation_id, self.settings.context_turns, self.settings.context_characters
        )
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                result = await self.provider.complete([*history, Message("user", text)])
        except Exception as error:
            logger.warning("generation_failed type=%s", type(error).__name__)
            raise ProviderUnavailable() from None
        if not result.text.strip() or len(result.text) > 64000:
            raise ProviderUnavailable()
        # Revocation during generation must also prevent delivery/persistence.
        if not self.db.is_active(user.id):
            raise PermissionError()
        self.db.save_turn(
            conversation_id, stored_text, result, self.settings.context_turns, event_id
        )
        return result
