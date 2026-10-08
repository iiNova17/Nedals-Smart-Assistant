# PDF answers

The assistant now uses the same basic interaction pattern as NotebookLM: ask about project sources, follow up, and receive document references. It uses Gemini Developer API File Search, not the personal Gemini/NotebookLM application. There is no notebook UI or audio overview feature in this increment.

## Use from WhatsApp

In the group, mention the bot, reply to it, or use `/assistant`. DMs do not need a prefix.

- `/assistant What is the nominal torque of RS06 in our datasheet?`
- `/assistant And what is its voltage range?`
- `/assistant Summarize the RS06 specifications from our datasheet.`
- `/assistant Which documents are indexed?`

Post PDFs as before. They go to Drive/Inbox unless an explicit existing destination is selected. The upload confirmation reports the current indexing state; it does not claim indexing has already succeeded. The next background scan discovers the PDF, downloads it temporarily, verifies its checksum, counts its pages, and submits it for multimodal indexing. Later scans check completion. Allow several minutes; processing time and free-tier quotas can vary. No unsolicited completion notification is sent.

## Sources and freshness

The local database maps each derived index document to its Drive ID and content revision. A question searches only ready revisions. Before and after retrieval, the backend rechecks that the source still exists under the project root with the same content. Changed or inaccessible sources block delivery until reconciliation. Removed/changed index entries are deleted from File Search; original Drive files are never deleted by indexing.

Source links come from verified mappings, not filenames or model-generated URLs. Page numbers appear only when returned by retrieval and within the known PDF page count. Unmapped grounding passages are discarded. Short paragraphs with grounding support are retained to avoid cutting a model name or unit in half. This checks source identity and freshness; it is not a mathematical guarantee that every model interpretation is correct. Check the cited table for consequential engineering decisions.

Scanned PDFs are supported by explicitly selecting Gemini Embedding 2. Summaries cover retrieved material and can miss material elsewhere in a long document; they are not guaranteed exhaustive reviews. Ambiguous or unsupported questions should return an insufficient-evidence answer. Non-PDF files remain available through Drive filename/metadata search.

## Limits and cost

The pilot allows 20 PDFs, 200 MiB of total original PDF content, 25 MiB per file and 500 pages per PDF. Encrypted PDFs are unsupported. Folder scans stop safely at 100 folders/2,000 items; they do not silently exclude sources. Original files remain in Drive, with only temporary local downloads during indexing. The local metadata database does not hold PDF binaries or embeddings.

These conservative corpus caps keep the pilot below the documented 1 GB free File Search storage allowance (derived storage may be larger than source files). Google's current [pricing page](https://ai.google.dev/gemini-api/docs/pricing) lists File Search free-tier usage as free, subject to account limits. No billing or paid hosting was enabled. If free quotas run out, requests fail safely; there is no automatic paid upgrade. A PDF answer usually uses a router request plus a File Search generation. Chat caps count user attempts, while indexing uses separate bounded background requests.

The bot still runs on the owner's computer. This feature does not provide always-on hosting.

## Operator inspection and retry

`GET /api/v1/knowledge` returns indexing status with the existing member bearer token. Chat can also request indexing status. Failed indexing is not retried indefinitely; it preserves the original in Drive and waits for an explicit retry or changed content.

Read-only local status:

```powershell
.\.venv\Scripts\python.exe -m app.documents_cli status
```

Stop the local services before running a manual sync or retry, so only one process reconciles the index:

```powershell
.\scripts\stop-local.ps1
.\.venv\Scripts\python.exe -m app.documents_cli retry --file-id "DRIVE_FILE_ID"
.\scripts\start-local.ps1
```

Use `sync` in place of `retry --file-id ...` for a single reconciliation pass. A pending remote upload may need a subsequent pass. Credentials and raw provider errors are never printed by this CLI.

## Live validation

On 8 October 2026, the pilot's eight-page scanned datasheet was indexed successfully. Live retrieval distinguished rated and peak torque and cited the correct model's page. A two-turn assistant conversation retained RS06 across a torque question and a voltage-range follow-up, citing page 8 both times. A three-fact summary was sourced to that page, and an unsupported Wi-Fi-password question returned no evidence and no citations. The backend was restarted and WhatsApp reconnected to the pinned group; no unsolicited WhatsApp test message was sent.

All 57 Python tests and 8 WhatsApp tests passed. Automated tests cover mapped citations, missing evidence, invalid page numbers, stale/missing sources, filtered retrieval, index reuse and limits. Provider timeouts/errors occurred during development and remain possible on the free service; these produce a clear retry response rather than an invented answer.
