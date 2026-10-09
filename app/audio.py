"""Voice-note transcription and understanding with replaceable interface and privacy cleanup."""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from google.genai import types

logger = logging.getLogger("plume.audio")

MAX_AUDIO_SIZE_BYTES = 16 * 1024 * 1024  # 16 MiB


class AudioTranscriber(ABC):
    """Abstract base class for audio transcription and understanding."""

    @abstractmethod
    async def transcribe(
        self, audio_bytes: bytes, mime_type: str = "audio/ogg", prompt: str = ""
    ) -> str:
        """Transcribe or summarize audio bytes, returning text in original language."""
        pass


class GeminiAudioTranscriber(AudioTranscriber):
    """Audio transcription powered by Gemini multimodal API."""

    def __init__(self, provider: Any):
        self.provider = provider

    async def transcribe(
        self, audio_bytes: bytes, mime_type: str = "audio/ogg", prompt: str = ""
    ) -> str:
        if not audio_bytes:
            raise ValueError("Audio data is empty")
        if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
            raise ValueError(f"Audio file exceeds 16 MiB limit ({len(audio_bytes)} bytes)")

        # Normalize WhatsApp audio mime types
        clean_mime = mime_type.split(";")[0].strip() or "audio/ogg"
        if clean_mime not in {
            "audio/ogg",
            "audio/mp4",
            "audio/mpeg",
            "audio/wav",
            "audio/aac",
            "audio/x-m4a",
        }:
            raise ValueError("Unsupported audio MIME type")

        if clean_mime == "audio/ogg":
            if not audio_bytes.startswith(b"OggS") or b"OpusHead" not in audio_bytes[:65536]:
                raise ValueError("WhatsApp voice notes must be Ogg Opus audio")
            offset, samples = 0, 0
            while offset < len(audio_bytes):
                page = audio_bytes[offset : offset + 27]
                if len(page) < 27 or page[:4] != b"OggS":
                    raise ValueError("Malformed Ogg audio")
                count = page[26]
                segments = audio_bytes[offset + 27 : offset + 27 + count]
                length = 27 + count + sum(segments)
                if len(segments) != count or offset + length > len(audio_bytes):
                    raise ValueError("Truncated Ogg audio")
                granule = int.from_bytes(page[6:14], "little")
                if granule != (1 << 64) - 1:
                    samples = max(samples, granule)
                offset += length
            if samples > 48000 * 600:
                raise ValueError("Voice note exceeds ten minutes")
        system_instruction = (
            "You are an expert audio transcriber. Transcribe the spoken audio verbatim in its "
            "original spoken language, supporting both Arabic (modern sta"
            "ndard or dialects like Egyptian) "
            "and English. If there is background noise, transcribe only the speaker. "
            "If parts are unintelligible, write [inaudible]. Output ONLY "
            "the transcription text without commentary."
        )

        part = types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime)
        user_prompt = prompt or "Transcribe this audio verbatim in its original language."

        model = getattr(self.provider.settings, "gemini_model", "gemini-2.5-flash")

        try:
            # Leverage provider's multi-key rotated generation
            response = await self.provider._generate_with_rotation(
                model=model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[part, types.Part.from_text(text=user_prompt)],
                    )
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1,
                ),
            )
            candidate = (response.candidates or [None])[0]
            text = (
                candidate.content.parts[0].text
                if (candidate and candidate.content and candidate.content.parts)
                else ""
            )
            if candidate is None or candidate.finish_reason != types.FinishReason.STOP:
                raise ValueError("Audio transcription incomplete")
            text = "".join(
                part.text for part in candidate.content.parts if part.text and not part.thought
            )
            if not text.strip() or len(text) > 8000:
                raise ValueError("Audio transcription empty or too long")
            return text.strip()
        except Exception as error:
            logger.warning("audio_transcription_failed type=%s", type(error).__name__)
            raise


async def process_audio_file(
    transcriber: AudioTranscriber,
    file_path: Path | str,
    mime_type: str = "audio/ogg",
    prompt: str = "",
) -> str:
    """Read audio, transcribe it, and guarantee immediate deletion of temporary file."""
    p = Path(file_path)
    try:
        if not p.is_file():
            raise FileNotFoundError(f"Audio file missing: {p}")
        if p.stat().st_size > MAX_AUDIO_SIZE_BYTES:
            raise ValueError("Audio exceeds 16 MiB")
        data = p.read_bytes()
        return await transcriber.transcribe(data, mime_type=mime_type, prompt=prompt)
    finally:
        # Guarantee immediate cleanup of temporary audio
        p.unlink(missing_ok=True)
