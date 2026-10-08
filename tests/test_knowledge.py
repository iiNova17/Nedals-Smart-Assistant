from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import errors, types

from app.config import Settings
from app.db import Database
from app.knowledge import Knowledge, KnowledgeUnavailable, grounded_result, revision


@pytest.fixture
def knowledge(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "test.db")
    db = Database(settings.database_path)
    db.initialize()
    drive = MagicMock(root_id="root")
    file = {
        "id": "pdf",
        "name": "Manual.pdf",
        "sha256": "original",
        "modified_at": "2026-10-08",
        "size": 100,
    }
    drive.metadata.return_value = file
    drive.pdf_catalog.return_value = [file]
    client = MagicMock()
    knowledge = Knowledge(settings, db, drive, client)
    with db.connect() as connection:
        connection.execute("INSERT INTO knowledge_stores VALUES (?,?)", ("root", "stores/test"))
        connection.execute(
            "INSERT INTO knowledge_documents "
            "(root_id,drive_id,source_key,filename,modified_at,sha256,document_name,"
            "status,page_count,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "root",
                "pdf",
                revision(file),
                "Manual.pdf",
                file["modified_at"],
                "original",
                "stores/test/documents/pdf",
                "ready",
                8,
                "now",
            ),
        )
    return knowledge


def candidate(rows, *, page=8, known=True, indices=None):
    context = types.GroundingChunkRetrievedContext(
        document_name=rows[0]["document_name"] if known else "other/document",
        title="Manual.pdf",
        page_number=page,
        text="Rated 11 Nm; peak 36 Nm",
    )
    return types.Candidate(
        finish_reason="STOP",
        content=types.Content(parts=[types.Part(text="Unsupported invented opening")]),
        grounding_metadata=types.GroundingMetadata(
            grounding_chunks=[types.GroundingChunk(retrieved_context=context)],
            grounding_supports=[
                types.GroundingSupport(
                    grounding_chunk_indices=indices if indices is not None else [0],
                    segment=types.Segment(text="Rated torque is 11 Nm; peak torque is 36 Nm."),
                )
            ],
        ),
    )


def test_only_supported_segments_and_verified_drive_links_are_delivered(knowledge):
    rows = knowledge.records()
    result = grounded_result(candidate(rows), rows)
    assert result["grounded"]
    assert "Unsupported" not in result["answer"]
    assert "[S1]" in result["answer"]
    assert result["citations"][0]["page"] == 8
    assert result["citations"][0]["drive_url"] == "https://drive.google.com/file/d/pdf"


@pytest.mark.parametrize("options", [{"known": False}, {"indices": [0, 1]}, {"indices": []}])
def test_unknown_or_partially_mapped_sources_cannot_support_an_answer(knowledge, options):
    rows = knowledge.records()
    result = grounded_result(candidate(rows, **options), rows)
    assert not result["grounded"]
    assert not result["citations"]


@pytest.mark.parametrize("page", [None, 0, 9])
def test_missing_or_invalid_pages_are_not_invented(knowledge, page):
    rows = knowledge.records()
    assert grounded_result(candidate(rows, page=page), rows)["citations"][0]["page"] is None


def test_ungrounded_model_answer_is_not_used(knowledge):
    result = grounded_result(
        types.Candidate(content=types.Content(parts=[types.Part(text="Made-up specification")])),
        knowledge.records(),
    )
    assert not result["grounded"]
    assert "Made-up" not in result["answer"]


@pytest.mark.parametrize("missing", [True, False])
def test_deleted_or_changed_source_blocks_generation_and_becomes_stale(knowledge, missing):
    if missing:
        knowledge.drive.metadata.side_effect = RuntimeError("Source outside allowed root")
    else:
        knowledge.drive.metadata.return_value = {
            **knowledge.drive.metadata.return_value,
            "sha256": "changed",
        }
    with pytest.raises(KnowledgeUnavailable, match="source changed"):
        knowledge.answer("Rated torque?")
    knowledge.client.models.generate_content.assert_not_called()
    assert knowledge.records()[0]["status"] == "stale"


def test_unknown_selected_source_and_empty_index_fail_closed(knowledge):
    with pytest.raises(KnowledgeUnavailable, match="not ready"):
        knowledge.answer("Question", ["outside"])
    knowledge.update("pdf", status="indexing")
    with pytest.raises(KnowledgeUnavailable, match="No indexed"):
        knowledge.answer("Question")


def test_source_revision_rechecked_after_generation(knowledge):
    rows = knowledge.records()
    original = knowledge.drive.metadata.return_value
    knowledge.drive.metadata.side_effect = [original, {**original, "sha256": "changed"}]
    knowledge.client.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[candidate(rows)]
    )
    with pytest.raises(KnowledgeUnavailable, match="source changed"):
        knowledge.answer("Question")


def test_retrieval_is_filtered_to_verified_ready_revisions(knowledge):
    rows = knowledge.records()
    knowledge.client.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[candidate(rows)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10),
    )
    result = knowledge.answer("What is the torque?")
    config = knowledge.client.models.generate_content.call_args.kwargs["config"]
    assert config.tools[0].file_search.metadata_filter == f'source_key="{rows[0]["source_key"]}"'
    assert result["input_tokens"] == 10


def test_sync_reuses_ready_index_and_removes_orphan_derived_document(knowledge):
    row = knowledge.records()[0]
    remote = SimpleNamespace(
        name=row["document_name"],
        state="STATE_ACTIVE",
        custom_metadata=[SimpleNamespace(key="source_key", string_value=row["source_key"])],
    )
    orphan = SimpleNamespace(name="stores/test/documents/orphan", custom_metadata=[])
    documents = knowledge.client.file_search_stores.documents
    documents.list.return_value = [remote, orphan]
    result = knowledge.sync()
    assert result["ready_count"] == 1
    knowledge.drive.download_pdf.assert_not_called()
    documents.delete.assert_called_once_with(name=orphan.name, config={"force": True})


def test_failed_index_does_not_automatically_retry_upload(knowledge):
    knowledge.update("pdf", status="failed")
    knowledge.client.file_search_stores.documents.list.return_value = []
    knowledge.sync()
    knowledge.drive.download_pdf.assert_not_called()


def test_corpus_limits_report_failure_without_upload(knowledge):
    knowledge.drive.pdf_catalog.return_value = [
        {**knowledge.drive.metadata.return_value, "size": 500 * 1024 * 1024}
    ]
    assert knowledge.sync()["sync_error"]
    knowledge.client.file_search_stores.upload_to_file_search_store.assert_not_called()


def test_grounded_fragment_preserves_model_label_but_not_other_paragraphs(knowledge):
    rows = knowledge.records()
    result = candidate(rows)
    result.content = types.Content(
        parts=[
            types.Part(
                text="Unsupported opening.\n\nRS06 rated torque is 11 N.m.\n\nUnsupported ending."
            )
        ]
    )
    result.grounding_metadata.grounding_supports[0].segment.text = "m."
    answer = grounded_result(result, rows)
    assert answer["answer"] == "RS06 rated torque is 11 N.m. [S1]"


def test_sync_removes_source_even_if_remote_index_was_already_deleted(knowledge):
    knowledge.drive.pdf_catalog.return_value = []
    knowledge.client.file_search_stores.documents.delete.side_effect = errors.ClientError(
        404, {"error": {"message": "Gone", "code": 404}}
    )
    knowledge.client.file_search_stores.documents.list.return_value = []
    assert not knowledge.sync()["sync_error"]
    assert knowledge.records() == []
