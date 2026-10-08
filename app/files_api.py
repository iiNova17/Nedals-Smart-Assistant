import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.assistant import Assistant
from app.domain import User
from app.drive import DriveClient, DriveError
from app.ingestion import Ingestion


def router(
    drive: DriveClient | None, ingestion: Ingestion | None, assistant: Assistant, current_user
):
    routes = APIRouter(prefix="/api/v1/drive", tags=["Google Drive"])

    def configured():
        if drive is None or not drive.ready:
            raise HTTPException(503, "Google Drive is not connected")

    @routes.get("/files")
    async def search(
        user: Annotated[User, Depends(current_user)],
        q: str = Query(default="", max_length=200),
        folders_only: bool = False,
    ):
        configured()
        try:
            return await asyncio.to_thread(drive.search, q, folders_only)
        except DriveError as error:
            raise HTTPException(400, str(error)) from None

    @routes.get("/files/{file_id}")
    async def metadata(file_id: str, user: Annotated[User, Depends(current_user)]):
        configured()
        try:
            return await asyncio.to_thread(drive.metadata, file_id)
        except DriveError as error:
            raise HTTPException(400, str(error)) from None

    @routes.post("/files", status_code=201)
    async def upload(
        request: Request,
        user: Annotated[User, Depends(current_user)],
        filename: str = Query(min_length=1, max_length=200),
        destination: str = Query(default="", max_length=150),
    ):
        """Send file bytes as the request body. destination is an exact folder name."""
        configured()
        if assistant.lock.locked():
            raise HTTPException(429, "Assistant busy", headers={"Retry-After": "3"})
        async with assistant.lock:
            root = ingestion.settings.incoming_path
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"web-{uuid4().hex}.part"
            try:
                size = 0
                with path.open("xb") as file:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > ingestion.settings.max_upload_bytes:
                            raise HTTPException(413, "Maximum upload is 25 MiB")
                        file.write(chunk)
                task = asyncio.create_task(
                    asyncio.to_thread(ingestion.ingest, path, filename, user, destination)
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A cancelled request doesn't cancel its worker thread. Keep its
                    # source alive until it finishes, including any resumable upload.
                    await task
                    raise
            except DriveError as error:
                raise HTTPException(400, str(error)) from None
            finally:
                path.unlink(missing_ok=True)

    return routes
