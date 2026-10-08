import asyncio
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant import Assistant
from app.commands import Commands
from app.config import Settings, load_team
from app.db import Database
from app.domain import ProviderUnavailable, QuotaExceeded, User
from app.drive import DriveClient
from app.files_api import router as files_router
from app.ingestion import Ingestion
from app.knowledge import Knowledge
from app.project_tools import ProjectTools
from app.providers.base import ChatProvider
from app.providers.gemini import GeminiProvider
from app.whatsapp import router as whatsapp_router
from app.workspace import Workspace

bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger("plume")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Text must not be blank")
        return text


class ChatResponse(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    evidence: str = "conversation_and_general_reasoning"


class ConversationResponse(BaseModel):
    id: str


def create_app(settings: Settings | None = None, provider: ChatProvider | None = None) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.database_path)
    team = load_team(settings)
    drive = (
        DriveClient(team.drive_folder_id, settings.drive_token_path)
        if team and team.drive_folder_id
        else None
    )
    knowledge = (
        Knowledge(settings, db, drive)
        if (
            provider is None
            and drive
            and settings.document_search_enabled
            and settings.gemini_api_key.get_secret_value()
        )
        else None
    )
    workspace = Workspace(db)
    ingestion = Ingestion(settings, db, drive, knowledge) if drive else None
    commands = Commands(workspace, settings, drive, ingestion)
    project_tools = ProjectTools(drive, knowledge, commands)
    active_provider = provider if provider is not None else GeminiProvider(settings, project_tools)
    assistant = Assistant(settings, db, active_provider)
    assistant.commands = commands
    if isinstance(active_provider, GeminiProvider):
        project_tools.search_client = active_provider.client

    async def reminder_loop():
        while True:
            try:
                await asyncio.to_thread(commands.schedule.tick)
            except Exception:
                logger.warning("reminder_tick_failed")
            await asyncio.sleep(15)

    async def knowledge_loop():
        while True:
            await asyncio.to_thread(knowledge.sync)
            await asyncio.sleep(settings.document_sync_seconds)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            db.cleanup(settings.context_days)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
        # HTTP client debug output can contain headers; keep it off regardless of app level.
        for name in (
            "httpx",
            "httpcore",
            "google_genai",
            "googleapiclient",
            "google.auth",
            "google_auth_httplib2",
            "urllib3",
            "requests_oauthlib",
        ):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        db.initialize()
        team = load_team(settings) if settings.bridge_token.get_secret_value() else None
        if team:
            db.ensure_owner(
                team.owner_phone, commands.context.read("identity").get("creator", "Owner")
            )
            with db.connect() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO access_members(phone,allowed) VALUES (?,1)",
                    (team.owner_phone,),
                )
                if team.group_id and not workspace.get("groups_initialized", False):
                    connection.execute(
                        "INSERT OR IGNORE INTO registered_groups VALUES (?,?,1)",
                        (team.group_id, team.group_name),
                    )
            workspace.set("groups_initialized", True)
        db.cleanup(settings.context_days)
        cleanup_task = asyncio.create_task(cleanup_loop())
        knowledge_task = asyncio.create_task(knowledge_loop()) if knowledge else None
        reminder_task = asyncio.create_task(reminder_loop())
        try:
            yield
        finally:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task
            if knowledge_task:
                knowledge_task.cancel()
                with suppress(asyncio.CancelledError):
                    await knowledge_task
                # to_thread workers may still be inside a bounded request on shutdown.
                await asyncio.to_thread(knowledge.lock.acquire)
                knowledge.lock.release()
                knowledge.close()
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            await active_provider.close()

    app = FastAPI(title="Nedal’s Smart Assistant", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.assistant = assistant
    app.state.drive = drive
    app.state.knowledge = knowledge
    app.state.workspace = workspace
    app.include_router(whatsapp_router(settings, db, assistant, ingestion))

    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        # Exclude paths, query strings, bodies, names, phones and tokens.
        logger.info(
            "request id=%s method=%s status=%s", request_id, request.method, response.status_code
        )
        return response

    def current_user(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        user = db.authenticate(credentials.credentials) if credentials else None
        if not credentials and request.cookies.get("assistant_session"):
            if request.method not in {"GET", "HEAD"}:
                browser_origin(request)
            with db.connect() as connection:
                row = connection.execute(
                    "SELECT u.id,u.name FROM browser_sessions s JOIN users u ON s.user_id=u.id "
                    "WHERE s.token_hash=? AND s.expires>? AND u.active=1",
                    (
                        db.token_hash(request.cookies["assistant_session"]),
                        datetime.now(UTC).isoformat(),
                    ),
                ).fetchone()
            user = User(row["id"], row["name"]) if row else None
        if user:
            actor = workspace.actor(user)
            if actor.phone and not workspace.can_access(actor.phone):
                user = None
        if user is None:
            raise HTTPException(
                401, "Valid team credentials required", headers={"WWW-Authenticate": "Bearer"}
            )
        return user

    def browser_origin(request):
        host = request.headers.get("host", "")
        if (
            not request.client
            or request.client.host not in {"127.0.0.1", "::1", "testclient"}
            or host not in {"127.0.0.1:8000", "localhost:8000"}
            or request.headers.get("origin") != "http://" + host
        ):
            raise HTTPException(403, "Local same-origin browser access required")

    @app.post("/api/v1/browser-session")
    def browser_session(request: Request, response: Response):
        browser_origin(request)
        owner = db.whatsapp_user(workspace.owner_phone())
        if not owner:
            raise HTTPException(503, "Complete local owner setup first")
        token = secrets.token_urlsafe(32)
        with db.connect() as connection:
            connection.execute(
                "DELETE FROM browser_sessions WHERE expires<=?", (datetime.now(UTC).isoformat(),)
            )
            connection.execute(
                "INSERT INTO browser_sessions VALUES (?,?,?)",
                (
                    db.token_hash(token),
                    owner.id,
                    (datetime.now(UTC) + timedelta(hours=8)).isoformat(),
                ),
            )
        response.set_cookie(
            "assistant_session", token, httponly=True, samesite="strict", max_age=8 * 3600, path="/"
        )
        return {"ready": True}

    app.include_router(files_router(drive, ingestion, assistant, current_user))

    @app.get("/api/v1/me")
    def me(user: Annotated[User, Depends(current_user)]):
        actor = workspace.actor(user)
        return {
            "name": user.name,
            "admin": actor.admin,
            "assistant_name": commands.context.read("identity").get("name", "Project Assistant"),
        }

    from pathlib import Path

    web_path = Path(__file__).parent / "web"
    if web_path.is_dir():

        @app.get("/assets/{asset}", include_in_schema=False)
        def web_asset(asset: str):
            # Windows registry MIME mappings can label .js as text/plain.
            media = {"app.js": "text/javascript", "style.css": "text/css"}
            if asset not in media:
                raise HTTPException(404, "Asset not found")
            return FileResponse(web_path / asset, media_type=media[asset])

        @app.get("/", include_in_schema=False)
        def web_ui():
            return FileResponse(web_path / "index.html")

    @app.get("/api/v1/knowledge")
    def knowledge_status(user: Annotated[User, Depends(current_user)]):
        return knowledge.status() if knowledge else {"ready_count": 0, "disabled": True}

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness():
        # Configuration readiness only; never spends tokens as a health check.
        if not active_provider.ready:
            raise HTTPException(503, "Chat provider is not configured")
        return {"status": "configured", "live_provider_verified": False}

    @app.post("/api/v1/conversations", response_model=ConversationResponse, status_code=201)
    def create_conversation(user: Annotated[User, Depends(current_user)]):
        try:
            return ConversationResponse(id=db.create_conversation(user))
        except QuotaExceeded:
            raise HTTPException(429, "Conversation limit reached; delete an old session") from None

    @app.post("/api/v1/conversations/{conversation_id}/messages", response_model=ChatResponse)
    async def message(
        conversation_id: UUID, body: ChatRequest, user: Annotated[User, Depends(current_user)]
    ):
        cid = str(conversation_id)
        if not db.owns_conversation(user.id, cid):
            raise HTTPException(404, "Conversation not found")
        if assistant.lock.locked():
            raise HTTPException(
                429, "Assistant is busy; retry shortly", headers={"Retry-After": "3"}
            )
        async with assistant.lock:
            try:
                result = await assistant.reply(user, cid, body.text)
            except QuotaExceeded:
                raise HTTPException(
                    429, "Daily usage limit reached (resets at 00:00 UTC)"
                ) from None
            except PermissionError:
                raise HTTPException(401, "Team access has been revoked") from None
            except ProviderUnavailable:
                raise HTTPException(503, "Assistant unavailable; retry later") from None
        return ChatResponse(
            text=result.text,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            evidence=result.evidence,
        )

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    def history(conversation_id: UUID, user: Annotated[User, Depends(current_user)]):
        if not db.owns_conversation(user.id, str(conversation_id)):
            raise HTTPException(404, "Conversation not found")
        return {
            "messages": [
                {"role": m.role, "text": m.text}
                for m in db.history(
                    str(conversation_id), settings.context_turns, settings.context_characters
                )
            ]
        }

    @app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: UUID, user: Annotated[User, Depends(current_user)]
    ):
        if assistant.lock.locked():
            raise HTTPException(409, "Wait for the active reply before deleting a conversation")
        async with assistant.lock:
            if not db.delete_conversation(user.id, str(conversation_id)):
                raise HTTPException(404, "Conversation not found")
        return Response(status_code=204)

    return app
