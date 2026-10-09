from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class User:
    id: str
    name: str


@dataclass(frozen=True)
class ImageInput:
    data: bytes = field(repr=False)
    mime_type: str = "image/png"


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    text: str
    images: tuple[ImageInput, ...] = ()


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    evidence: str = "conversation_and_general_reasoning"


class ProviderUnavailable(Exception):
    """Safe boundary: never expose provider exception text to clients."""


class QuotaExceeded(Exception):
    pass
