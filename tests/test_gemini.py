import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors, types

from app.config import Settings
from app.domain import Message, ProviderUnavailable
from app.providers.gemini import GeminiProvider


def build_provider(monkeypatch, result):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=result)
    client.aio.aclose = AsyncMock()
    monkeypatch.setattr("app.providers.gemini.genai.Client", lambda **kwargs: client)
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key="test-not-real"))
    return provider, client


def test_sdk_mapping_filters_thoughts_and_counts_usage(monkeypatch):
    result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    parts=[
                        types.Part(text="hidden thought", thought=True),
                        types.Part(text="Visible answer"),
                    ]
                ),
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=20,
            candidates_token_count=8,
            thoughts_token_count=5,
        ),
    )
    provider, client = build_provider(monkeypatch, result)
    response = asyncio.run(
        provider.complete([Message("user", "Question"), Message("assistant", "Earlier answer")])
    )
    assert response.text == "Visible answer"
    assert response.output_tokens == 13
    args = client.aio.models.generate_content.call_args.kwargs
    assert [c.role for c in args["contents"]] == ["user", "model"]
    assert args["config"].automatic_function_calling.disable
    assert args["config"].tools is None
    asyncio.run(provider.close())
    client.aio.aclose.assert_awaited_once()
    client.close.assert_called_once()


def test_quota_fallback_preserves_tool_context_and_backoff(monkeypatch):
    result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP", content=types.Content(parts=[types.Part(text="Ready")])
            )
        ]
    )
    provider, client = build_provider(monkeypatch, result)
    client.aio.models.generate_content.side_effect = [
        errors.ClientError(
            429,
            {
                "error": {
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}
                    ]
                }
            },
        ),
        result,
        result,
    ]
    first = asyncio.run(provider.complete([Message("user", "Hello")]))
    second = asyncio.run(provider.complete([Message("user", "Hello again")]))
    assert first.model == second.model == provider.settings.gemini_fallback_model
    assert [c.kwargs["model"] for c in client.aio.models.generate_content.call_args_list] == [
        provider.settings.gemini_model,
        provider.settings.gemini_fallback_model,
        provider.settings.gemini_fallback_model,
    ]


@pytest.mark.parametrize("reason", ["SAFETY", "MAX_TOKENS"])
def test_incomplete_answers_not_saved(monkeypatch, reason):
    result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason=reason,
                content=types.Content(parts=[types.Part(text="Partial")]),
            )
        ]
    )
    provider, _ = build_provider(monkeypatch, result)
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.complete([Message("user", "Question")]))


def test_empty_response_and_exception_are_safe(monkeypatch):
    provider, client = build_provider(monkeypatch, SimpleNamespace(candidates=[]))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.complete([Message("user", "Question")]))
    client.aio.models.generate_content.side_effect = RuntimeError("secret")
    with pytest.raises(ProviderUnavailable) as error:
        asyncio.run(provider.complete([Message("user", "Question")]))
    assert "secret" not in str(error.value)


def test_manual_drive_tool_round_trip_and_verified_links(monkeypatch):
    tool_result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="project_drive",
                                id="call1",
                                args={"operation": "search", "query": "manual"},
                            ),
                            thought_signature=b"preserve-this-signature",
                        )
                    ],
                ),
            )
        ]
    )
    final_result = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(role="model", parts=[types.Part(text="Found it.")]),
            )
        ]
    )
    provider, client = build_provider(monkeypatch, final_result)
    client.aio.models.generate_content.side_effect = [tool_result, final_result]
    provider.tools = MagicMock(ready=True)
    provider.tools.declarations.return_value = []
    provider.tools.execute = AsyncMock(
        return_value={
            "files": [
                {
                    "id": "file123",
                    "name": "Manual.pdf",
                    "drive_url": "https://drive.google.com/file/d/file123",
                }
            ]
        }
    )
    result = asyncio.run(provider.complete([Message("user", "Find manual")]))
    assert result.evidence == "project_drive_metadata_and_reasoning"
    assert "https://drive.google.com/file/d/file123" in result.text
    provider.tools.execute.assert_awaited_once_with(
        "project_drive", {"operation": "search", "query": "manual"}
    )
    contents = client.aio.models.generate_content.call_args.kwargs["contents"]
    assert contents[1].parts[0].thought_signature == b"preserve-this-signature"
    assert contents[2].parts[0].function_response.id == "call1"


def test_repeated_tool_calls_are_bounded(monkeypatch):
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="project_drive", args={"operation": "search"}
                            ),
                        )
                    ],
                ),
            )
        ]
    )
    provider, client = build_provider(monkeypatch, response)
    provider.tools = MagicMock(ready=True)
    provider.tools.declarations.return_value = []
    provider.tools.execute = AsyncMock(return_value={"files": []})
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.complete([Message("user", "Find manual")]))
    assert client.aio.models.generate_content.await_count == 6
    assert provider.tools.execute.await_count == 5


def test_document_answer_delivered_directly_with_backend_citations(monkeypatch):
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="project_documents",
                                args={"question": "What is RS06 rated torque?"},
                            )
                        )
                    ],
                ),
            )
        ]
    )
    provider, client = build_provider(monkeypatch, response)
    provider.tools = MagicMock(ready=True)
    provider.tools.declarations.return_value = []
    provider.tools.execute = AsyncMock(
        return_value={
            "document_answer": True,
            "grounded": True,
            "answer": "Rated torque: 11 Nm [S1]",
            "input_tokens": 123,
            "output_tokens": 20,
            "citations": [
                {
                    "label": "S1",
                    "name": "Manual.pdf",
                    "page": 8,
                    "drive_url": "https://drive.google.com/file/d/source",
                }
            ],
        }
    )
    result = asyncio.run(
        provider.complete(
            [
                Message("user", "Let's discuss RS06"),
                Message("assistant", "Okay"),
                Message("user", "What is its torque?"),
            ]
        )
    )
    assert result.evidence == "project_pdf_evidence"
    assert result.text.startswith("Rated torque: 11 Nm [S1]")
    assert "PDF page 8" in result.text
    assert result.text.endswith("https://drive.google.com/file/d/source")
    assert result.input_tokens == 123
    assert result.output_tokens == 20
    client.aio.models.generate_content.assert_awaited_once()
    contents = client.aio.models.generate_content.call_args.kwargs["contents"]
    assert contents[0].parts[0].text == "Let's discuss RS06"
    assert contents[2].parts[0].text == "What is its torque?"


def test_eight_member_actions_run_and_return_all_tool_results(monkeypatch):
    calls = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="assistant_action",
                                id=f"member-{i}",
                                args={
                                    "action": "member_update",
                                    "operation": "whitelist",
                                    "target": f"20100000000{i}",
                                    "title": f"Member {i}",
                                },
                            )
                        )
                        for i in range(8)
                    ],
                ),
            )
        ]
    )
    final = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    role="model", parts=[types.Part(text="Added all eight teammates.")]
                ),
            )
        ]
    )
    provider, client = build_provider(monkeypatch, final)
    client.aio.models.generate_content.side_effect = [calls, final]
    provider.tools = MagicMock(ready=True)
    provider.tools.declarations.return_value = []
    provider.tools.execute = AsyncMock(return_value={"result": "Approved regular member"})
    result = asyncio.run(provider.complete([Message("user", "Add these eight teammates")]))
    assert result.text == "Added all eight teammates."
    assert provider.tools.execute.await_count == 8
    responses = client.aio.models.generate_content.call_args.kwargs["contents"][2].parts
    assert len(responses) == 8
    assert [p.function_response.id for p in responses] == [f"member-{i}" for i in range(8)]
