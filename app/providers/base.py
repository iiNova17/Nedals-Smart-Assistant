from typing import Protocol

from app.domain import Completion, Message


class ChatProvider(Protocol):
    @property
    def ready(self) -> bool: ...

    async def complete(self, messages: list[Message]) -> Completion: ...

    async def close(self) -> None: ...
