# Nedal’s Smart Assistant

A conversational project assistant for WhatsApp groups, DMs and a local web client. Gemini reasons over project context and calls backend tools for Drive, PDFs, decisions, schedules, permissions and team coordination.

## Use it

Open http://127.0.0.1:8000/ on the owner’s computer: ready to chat, no token entry. The centered interface supports light/dark themes, conversation history, uploads and document status. In WhatsApp, DM normally or mention/reply to the bot in a registered group. Approved members’ documents are received without a mention.

Try natural requests:

- “What do we have on our calendar this week?”
- “Remember that RS06 is our final shoulder motor.”
- “Forget the test decision I just added.”
- “I have class every Monday from 9 to 11. Add it to my timetable.”
- “Schedule a design review tomorrow at 3 PM for one hour.”
- Owner: “Stop scheduling events without my permission.”
- Owner: “Allow [approved person] to manage reminders.”
- Owner: “Block +COUNTRYCODE_NUMBER.”
- Owner, in a group: “Remember this group for meeting reminders.”
- Owner: “Remind this group every Monday at 6 PM about our meeting.”
- Owner: “Summarize our arm decisions and save the report to Drive.”

The model resolves dates, people and records. It asks when intent is missing or ambiguous. Ordinary authorized actions execute without extra confirmation; opening public access and promoting admins require owner confirmation. Slash shortcuts are optional diagnostics, not the primary interface.

## Implemented

- Conversational Gemini tool use, editable identity/project/team files, separate DM/group/web context.
- Drive search, metadata/links, folder listing/creation, download for indexing, upload, explicit rename/move, generated Markdown reports.
- SHA-256 duplicate handling; original files remain in Drive. A two-minute sync discovers PDFs added directly to Drive.
- Gemini File Search for PDF answers with mapped Drive links and available page references. Other formats are stored/searchable by metadata, not content-indexed.
- Persistent decisions with source/author/revision history; editable work-package status separate from static background.
- Public/whitelist/owner modes, blocked accounts, scoped permission grants, approval workflows, permanent-owner protection.
- Manual timetables, optional personal Calendar mappings, shared Calendar read/create/reschedule/cancel, conflict checks that report unknown availability.
- Durable one-off/recurring group reminders, requested text/link messages to approved members/groups, approval requests through WhatsApp DM.
- Responsive **local owner** web client. Internet-facing multi-user browser login is not deployed.

## Permissions

The default remains whitelist. Bystanders may remain in groups but cannot invoke the assistant. Answers are visible to every group participant.

Approved members can save decisions, maintain their own timetable, read the shared calendar and request event changes by default. Editing another person’s decision is author/admin restricted. The owner can grant/restrict capabilities globally or per member through conversation. Member-specific rules override global rules; project role descriptions do not grant authority.

An approval rule holds the exact action and messages designated approvers. Any listed approver may approve; requests expire after 24 hours. Current rules and account access are checked again before execution. Reply naturally: “Approve the design review.” A direct request by a listed approver supplies their approval. A scoped grant does not appoint an admin.

Messages remain queued until WhatsApp confirms delivery. Ambiguous interrupted sends are not automatically replayed. No reminders run while the PC is offline.

## Setup

Use Python 3.12+ and Node.js 24+. In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm --prefix whatsapp ci --omit=optional
.\.venv\Scripts\python.exe -m app.setup --owner-phone "+COUNTRYCODE_NUMBER" --group-name "Team group" --drive-folder-id "FOLDER_ID"
```

Set the Gemini Developer API key in `.env`. Copy generic JSON files from `context/examples/` to `context/local/` and customize them; preserve existing personal context. Complete [Drive OAuth](docs/drive-setup.md) and [Calendar setup](docs/calendar.md). Private keys, data and local context are ignored by Git.

```powershell
.\scripts\start-local.ps1
.\scripts\stop-local.ps1
```

Pair the **bot** phone through Linked devices at http://127.0.0.1:8787/. Both services bind to loopback. Run one backend worker and one bridge. Restarting preserves the linked login. A recovery CLI can approve members:

```powershell
.\.venv\Scripts\python.exe -m app.admin approve-whatsapp-member --phone "+COUNTRYCODE_NUMBER" --name "Member"
```

Existing `PLUME_*` settings and the `data/plume.sqlite3` filename remain compatible.

## Models and cost

Conversation/tool selection prefers **Gemini 3.5 Flash**, falling back to **3.5 Flash-Lite** on quota exhaustion. PDF retrieval uses Flash-Lite. The live account returned a 20-request/day limit for Flash; a tool-assisted turn can use several requests. Free availability is not guaranteed. No paid tier was enabled.

Web search defaults to a no-key DDGS/DuckDuckGo adapter. Gemini reasons over snippets and the backend appends actual result links. Snippets do not prove the latest firmware version or substitute for reading full pages. Upstream rate limits/changes can affect availability. The optional Gemini grounding adapter remains modular: older 2.5 models reject this API account; current 3.x pricing excludes Search grounding on the API free tier. See [Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

The bot uses the Developer API and backend OAuth, not a personal Gemini web session. Request caps are not a billing guarantee.

## Storage and operation

See [how it works](docs/how-it-works.md), [context](docs/context.md), [Calendar](docs/calendar.md), [optional commands](docs/chat-commands.md), and [PDF limits](docs/pdf-answers.md). The original [architecture research](docs/architecture.md) is historical; this README describes current behavior.

Defaults: 25 MiB uploads, 20 PDFs/200 MiB indexed, six model rounds and four tool calls per round, 20 recent turns/seven days. Decisions and policy persist separately. Back up SQLite consistently with its backup API or stopped services, including WAL state.

Automatic web login trusts the owner’s computer. Do not expose port 8000 publicly: internet hosting needs separate browser authentication/TLS. Baileys is unofficial and carries account-restriction and maintenance risk.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
npm --prefix whatsapp test
```

Tests cover permissions, approvals/replays, calendar windows, timetables/DST, source mapping, duplicates, browser-origin checks, group triggers and durable queues. Live-model checks supplement these tests; they do not guarantee every phrasing.

24/7 hosting is deliberately not implemented. The generic [Smart Project Assistant AI template](https://github.com/iiNova17/Smart-Project-Assistant-AI) is now separate from this personal deployment. Advanced CAD parsing, custom agents, full task-management systems and NotebookLM audio features remain outside this MVP.
