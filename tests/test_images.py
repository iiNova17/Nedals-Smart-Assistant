import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from PIL import Image
from test_chat import FakeProvider
from test_gemini import build_provider
from test_scheduler_and_features import test_env as test_env

from app.domain import ImageInput, Message
from app.images import load_image
from app.main import create_app


def photo():
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(output, format="PNG")
    return output.getvalue()


def test_image_decoding_and_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "image.bin"
    path.write_bytes(photo())
    image = load_image(path)
    assert image.mime_type == "image/png" and not path.exists()
    assert Image.open(io.BytesIO(image.data)).size == (40, 30)
    path.write_bytes(b"not a photo")
    with pytest.raises(ValueError):
        load_image(path)
    assert not path.exists()
    monkeypatch.setattr("app.images.MAX_IMAGE_PIXELS", 10)
    path.write_bytes(photo())
    with pytest.raises(ValueError, match="megapixels"):
        load_image(path)
    assert not path.exists()


@pytest.mark.parametrize("quoted", [False, True])
def test_whatsapp_image_reaches_provider_and_is_not_reused(test_env, quoted):
    incoming = test_env["tmp_path"] / "incoming"
    incoming.mkdir()
    settings = test_env["settings"].model_copy(update={"incoming_path": incoming})
    provider = FakeProvider()
    app = create_app(settings, provider)
    token = "c" * 64
    path = incoming / (token + ".bin")
    path.write_bytes(photo())
    body = {
        "event_id": "image-test",
        "sender_phone": "15555550100",
        "chat_id": "123@g.us",
        "channel": "group",
        "text": "Describe this image",
        "kind": "text" if quoted else "image",
    }
    if quoted:
        body["reply_context"] = {
            "chat_id": "123@g.us",
            "content_type": "image",
            "attachment_reference": token,
        }
    else:
        body["file_token"] = token
    headers = {"Authorization": "Bearer bridge-test-token"}
    with TestClient(app) as client:
        response = client.post("/internal/whatsapp/messages", json=body, headers=headers)
        assert response.status_code == 200 and response.json()["text"] == "Test reply"
        assert len(provider.calls[-1][-1].images) == 1
        assert not path.exists()
        body = {
            "event_id": "next-turn",
            "sender_phone": "15555550100",
            "chat_id": "123@g.us",
            "channel": "group",
            "text": "Hello",
        }
        assert (
            client.post("/internal/whatsapp/messages", json=body, headers=headers).status_code
            == 200
        )
        assert all(not message.images for message in provider.calls[-1])


def test_missing_image_gets_a_useful_reply(test_env):
    provider = FakeProvider()
    with TestClient(create_app(test_env["settings"], provider)) as client:
        response = client.post(
            "/internal/whatsapp/messages",
            headers={"Authorization": "Bearer bridge-test-token"},
            json={
                "event_id": "missing",
                "sender_phone": "15555550100",
                "chat_id": "123@g.us",
                "channel": "group",
                "kind": "image",
                "media_error": "download_failed",
            },
        )
        assert response.status_code == 200 and "resend" in response.json()["text"]
        assert not provider.calls


def test_gemini_receives_inline_image_bytes(monkeypatch):
    result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(parts=[types.Part(text="A red rectangle")]),
            )
        ]
    )
    provider, client = build_provider(monkeypatch, result)
    data = photo()
    answer = asyncio.run(provider.complete([Message("user", "Describe", (ImageInput(data),))]))
    assert answer.text == "A red rectangle"
    sent = client.aio.models.generate_content.call_args.kwargs["contents"][0]
    assert sent.parts[1].inline_data.data == data
    assert sent.parts[1].inline_data.mime_type == "image/png"
