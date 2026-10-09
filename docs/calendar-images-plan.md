# Calendar subscriptions and image understanding — implementation handoff

Status: Calendar subscriptions, timetable imports and web image upload remain planned. Basic WhatsApp direct/quoted image understanding and saved-sticker sending are now implemented. Updated 9 October 2026.

## Objective and boundaries

Extend the existing assistant so a user can send a Google Calendar link, explain who it applies to, and ask questions using those events together with confirmed personal timetables and the team calendar. Add general image understanding, including importing a timetable image into a member's availability after review and confirmation. Interaction should be natural language; structured tools and backend permissions enforce the resulting actions.

Apply changes to the existing personal deployment and the generic public template. Preserve existing uncommitted work, authenticated sender attribution, incoming/outgoing quoted replies, quiet-group triggers, light/dark web UI, Drive integration, and configured approval rules. Do not reset either repository or replace its configuration with the other's. Do not add Discord, 24/7 hosting, a new agent framework, or a vector database for calendars.

Read this document and the actual code first. A previous implementation summary overstated completion. Passing the existing tests alone does not establish that an external workflow works.

## 0. Current audit and prerequisites

The follow-up audit corrected:

- Voice-note lookup used the wrong spool filename and directory. It now uses the configured incoming directory and the bridge's `<token>.bin` convention.
- Reply attachment references could contain paths; references now require a bounded hash token, and quoted chat context must match the current chat.
- Sticker saving used a nonexistent settings field and a global newest-upload search. The current request now supplies its own attachment reference. Another user cannot change the ownership, visibility, or description of a private duplicate sticker.
- Scheduled tasks bypassed capability and approval policies. Proposal, confirmation, execution, and delivery now use the original requester's current permissions. Destinations must remain authorized.
- Confirmation could be consumed more than once and in the same model turn that proposed the task. Consumption is now transactional and requires a later user request. Proposals expire after 15 minutes.
- Rescheduling now produces another preview requiring confirmation. Group listings and history no longer expose unrelated private tasks.
- Legacy reminders and sticker operations now honor current capability grants; recurring tasks recover after multi-year offline gaps. Late approvals cannot activate a task whose scheduled time has already passed.
- An interrupted claimed occurrence is marked uncertain at startup, not silently replayed. Queued output is distinguished from confirmed delivery.
- Scheduled calendar/report handlers claimed success without doing the work. These task types are now explicitly rejected.

Current verified automated baseline: personal deployment 110 Python tests and 16 Node tests; template 113 Python tests and 17 Node tests. External providers were mocked in the relevant regression tests. Real audio dialect accuracy and full media delivery have not been certified by these tests.

Before describing the prior feature set as complete, finish or explicitly retain these limitations:

1. **Outbound stickers (updated):** saved stickers can now be sent to the current WhatsApp conversation using `sticker_send`, an authenticated media endpoint and the persistent outbox. Sender access, sticker visibility, current grants and media integrity are rechecked before delivery. Uncertain sends are not replayed. Complete WebP decoding/dimension/animation validation remains future work. Implement explicitly requested Drive storage with a mapping and useful status, or document local-only storage accurately. Do not silently upload every sticker.
2. **Scheduled Calendar/Drive work:** implement typed executors using the existing immediate tools and exact required capabilities, or leave them rejected. A report must return a real Drive file ID/link. Calendar execution must return the actual created/updated event ID. Permission and designated approval changes must stop execution; consent to a reminder is not permission for another action. Do not execute arbitrary shell commands or open-ended agent instructions as scheduled jobs.
3. **Scheduler edge cases:** retain the regression tests for multi-year offline recurrence and legacy reminder revocation; broaden tests for overdue approvals, DST gaps/overlaps, cancellation while a notification is pending, and revoked destination access. Do not claim exactly-once delivery across an external WhatsApp network failure. Preserve uncertain outcomes for inspection. Current approvals require a common designated approver across required capabilities; disjoint approval chains are explicitly unsupported.
4. **Real media smoke tests:** verify an authorized DM voice note, a mentioned reply to another person's voice note, a sticker save, and outbound sticker delivery. Use owner-controlled test chats and do not send unsolicited test messages to members. A transcription should not bypass confirmation for sensitive actions.
5. **Sticker approvals:** allow/deny grants are enforced. A designated-approver sticker workflow remains unsupported and requests needing it are blocked explicitly.

Relevant code: `app/whatsapp.py`, `app/audio.py`, `app/stickers.py`, `app/scheduler.py`, `app/task_policy.py`, `app/scheduling.py`, `app/actions.py`, `app/project_tools.py`, `app/workspace.py`, and `whatsapp/src/{main,policy,reply}.mjs`. Follow existing transaction, tool receipt, and approval mechanisms rather than inventing parallel ones.

## 1. Calendar links: persistent application subscriptions

### User experience

Example: “This is the second-year lecture calendar. Alice and Omar attend these lectures; use it when checking our availability.”

The assistant should inspect the link, verify backend access, resolve those members, and show a concise preview containing calendar name, timezone, affected members/cohort, applicable dates, event filters, and visibility. Ask only for missing or ambiguous details. On confirmation, save the source and bindings. Later changes to the source must affect future availability without reimporting the link manually.

Use **application-level subscriptions** by default: persist the source in this application's database and refresh it through the Calendar API. This does not copy lectures into the team's writable calendar or require the source to appear in the Google account's sidebar. Actually adding a calendar to that account's CalendarList is a separate optional action and may require an additional OAuth scope; do not request wider permissions for the default flow. [Google CalendarList insertion reference](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/insert).

### Link parsing and access

- Accept raw Google Calendar IDs and known Google Calendar HTTPS links: embed links with one or more `src` values, subscription links with `cid`, and common share links. Parse URLs structurally. Percent-decode and decode a valid URL-safe base64 `cid` where appropriate; retain a valid raw ID when the parameter already contains one. Bound input and decoded lengths. Test malformed encodings and fragments.
- A `/u/3/` account selector says nothing about which account the backend uses. A `ctz` query value is a display preference, not proof of the source timezone.
- Do not mistake an event invitation link or event ID for a whole calendar. Explain that distinction and request the calendar link if necessary.
- Verify metadata and readable events using backend OAuth. Report inaccessible/missing sources clearly. A link does not grant access; do not attempt to change ACLs automatically. Keep the existing writable project-calendar configuration separate.
- Multiple `src` calendars become separate candidates with separate member mappings. Deduplicate by provider and canonical calendar ID.
- Treat event descriptions and calendar names as untrusted data, not tool instructions. Never put OAuth credentials or secret feed URLs into a model prompt or logs.

### Scope and membership

Represent what a source means explicitly: team events, member personal events, university cohort lectures, optional classes, or a reference calendar that does not block anyone by default. Do not infer that every university event applies to every member. Store confirmed member/cohort bindings, course/section filters, exclusions, and semester validity. If rules are ambiguous, ask the requester and keep the source informational until resolved.

Members can subscribe sources for themselves if permitted. An admin or delegated editor can map a source to others. Sharing busy intervals is distinct from sharing event titles and descriptions. A group should see “Alice is busy” when event details are private. Do not automatically disclose another member's personal calendar because the backend account can read it.

## 2. Calendar data and refresh design

Use additive SQLite migrations with foreign keys/indexes and durable source IDs. Suggested records:

| Record | Important fields |
| --- | --- |
| Calendar source | ID, provider, canonical calendar ID, label, timezone, creator, visibility, enabled state, access state, version, last successful refresh, last error category |
| Source binding | Source ID, member/cohort ID, confirmed inclusion/exclusion rules, validity start/end, busy/detail-sharing policy, provenance |
| Cached event | Source ID, provider event/instance identity, start/end, timezone, all-day flag, status, transparency, authorized display fields, fetched version |
| Coverage | Source ID, fetched interval, completed-at timestamp, refresh outcome |
| Import proposal | Requester/chat, source candidates and bindings, content hash/version, expiry, confirmation state |

Keep Google Calendar canonical for its events. The database contains a replaceable bounded cache and source mappings, not a second independently edited lecture calendar. Do not put calendars into project decision memory or RAG by default.

For the MVP, prefer a **bounded-window full refresh** of the next 90 days, with on-demand refresh for other requested ranges. Fetch every page with recurring instances expanded. Commit a replacement window only after the entire fetch succeeds. Do not turn a partial/error response into an empty schedule. Refresh at a configurable interval while the local backend is running, with a modest default such as 30 minutes, backoff, and request budgets. Show cache freshness when serving stale data. No new hosting is required.

Handle recurring instances, moved/cancelled occurrences, exclusive all-day end dates, timezone offsets, and transparent or declined events. Preserve provider identity when deduplicating; equal titles are insufficient. [Google event-list reference](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).

Do not combine moving `timeMin`/`timeMax` windows with an incremental sync token. If incremental sync is introduced later, maintain a separate consistent query/cache contract and rebuild it after a `410 Gone`. [Google synchronization guide](https://developers.google.com/workspace/calendar/api/guides/sync).

If a source becomes inaccessible, disable its fresh-data claims and retain an explicit stale/unknown status. It must not make a person look free. Unsubscribing removes that source's contribution without deleting unrelated manual entries or external events.

**Optional follow-up: ICS/webcal.** Google Calendar links are the initial milestone. If university feeds require ICS, add a separate adapter with a maintained parser for recurrence, exceptions, cancellations, and timezones. Restrict fetches to approved HTTPS hosts, block local/private/link-local targets after DNS resolution and at redirects, enforce size/time/redirect limits, use conditional requests, and prevent DNS rebinding. Treat private ICS URLs as credentials. Do not fetch arbitrary URLs from image text or a model response. Document ICS as unsupported until the adapter and its tests exist.

## 3. Unified availability and natural-language tools

Extend the current scheduling service rather than creating another availability engine. Merge confirmed manual timetables, confirmed image imports, subscribed calendar instances, and relevant shared project events. Carry provenance, validity, coverage, and visibility through the result.

Suggested tool responsibilities (names may follow existing conventions):

- Inspect a calendar link and propose subscription/bindings.
- Confirm, list, inspect status, update, refresh, and unsubscribe sources.
- Query an agenda for a member/cohort/team within a bounded interval.
- Check conflicts or find candidate meeting slots for explicit members and duration.
- Propose and confirm a timetable import; revise or remove an identified import batch.

Use the current date and configured IANA timezone to interpret “this week,” “tomorrow,” and “Tuesday at six.” Ask about AM/PM or the intended date only when materially ambiguous. Never ask users to provide ISO timestamps for ordinary conversation. Resolve names to canonical members; current speaker identity still comes only from the authenticated envelope.

Return known conflicts and unknown coverage separately. “No conflicts in recorded schedules” is different from “everyone is available.” Define busy-interval overlap using half-open intervals so a class ending at 14:00 does not conflict with a meeting starting at 14:00. Treat an unavailable source, expired semester, or unconfirmed timetable as incomplete coverage.

Creating a meeting remains a separate permitted calendar write using the existing approval rules. Availability checks, subscribing, or timetable import must not create events, invite attendees, or schedule reminders implicitly.

## 4. General image understanding

### Transport and validation

Add image attachments to the normalized incoming envelope for WhatsApp and the web interface. Carry a request-scoped opaque attachment token, MIME type, byte count, and provenance. Reuse reply context so “@assistant explain this” replying to an image processes that image. Direct image DMs should work with a caption or a brief follow-up. Quiet groups must not start responding to every photo.

Authorize the sender and apply trigger rules before media download/model calls. Resolve attachment tokens inside the configured spool directory. Do not use newest-file globs, arbitrary model-provided paths, or data from another request. Apply limits to actual downloaded bytes and decoded image dimensions/pixel count, reject corrupt/unsupported files, handle EXIF orientation, and strip unnecessary metadata. Start with JPEG, PNG, and static WebP. Animated stickers belong to the separate sticker pipeline.

Use an application limit such as 8 MB and 20 megapixels, with a documented bounded downsampling policy that preserves timetable legibility. Do not enlarge a blurry image and claim restored details. Clean temporary data in `finally` and through startup cleanup after crashes; retries and concurrent quoted replies must not share a destructively consumed spool path.

### Model integration

Add an image-capable method to the existing Gemini provider abstraction. Supply validated image bytes as multimodal parts along with the current question, caption, permitted recent context, and isolated quote metadata. Keep the provider replaceable and reuse quotas/timeouts/error reporting. This is image **understanding**, not an image-generation feature. Gemini supports multimodal image inputs directly; no separate OCR service is required for the initial design. [Gemini image-understanding documentation](https://ai.google.dev/gemini-api/docs/image-understanding).

Support general tasks such as explaining a diagram, comparing visible components, reading a screenshot, or understanding a chart. Distinguish visible evidence from inference and ask for a clearer crop when necessary. Do not invent unreadable labels. Instructions printed inside an image are reference content and confer no privileges.

Ordinary image questions should not permanently save media or update timetables. Persist an image to Drive only when requested or after an explicit import workflow that states storage and visibility. Do not add every image to the document index. If a user follows up on an image, use a bounded authorized attachment lease; otherwise explain that it expired and request it again rather than guessing.

## 5. Timetable-image import: extract, review, confirm

1. **Resolve intent and owner.** “This is Alice's timetable; remember it” selects an import workflow. Resolve Alice from team records and check permission to edit her schedule. For “my timetable,” use the authenticated sender. Never identify the owner from an image title alone.
2. **Extract a structured draft.** Capture days, start/end times, course labels, location if readable, timezone, start/end dates or semester range, recurrence/alternate weeks, section, exceptions, and per-field uncertainty. Retain source hash, draft version, and source regions or readable evidence for corrections. Validate schema and intervals server-side.
3. **Resolve ambiguity.** Ask about missing semester dates, 12/24-hour ambiguity, unclear cells, merged columns, alternating weeks, holidays, and whether optional classes should block time. Support Arabic and English labels and mixed numerals. Do not infer missing timetable cells or assume the schedule repeats forever.
4. **Preview.** Show the target member/cohort, timezone, date range, recurring intervals, exclusions, and who can see details. Use compact WhatsApp-native formatting, sensible grouping, and restrained emojis. Long previews may use a linked web review, but the user must be able to identify every interval before confirming.
5. **Confirm separately.** A later message from the authorized requester confirms the exact draft version/hash in the same conversation. Expire drafts and invalidate confirmation after edits. Reject same-model-turn self-confirmation, stale confirmations, wrong users, and changed permissions. Ambiguous “yes” with several proposals must ask which one.
6. **Commit atomically.** Store a versioned import batch and its linked schedule entries. Reimporting the same confirmed source must not double-count busy time. Default to additive import; replacement requires an explicit preview showing which prior batch entries change. Never wipe unrelated manual entries or Google events.
7. **Correct and forget.** “Move my Monday lab to 2 pm” should identify the affected entries, preview the change, and preserve provenance/history. Removing an import removes only its derived availability. Deleting a Google subscription does not delete the actual Google calendar.

The persisted schedule is structured data, not just a paragraph in chat memory. An extraction confidence score never substitutes for permission or user confirmation.

## 6. Permissions, privacy, and failure behavior

Integrate new capabilities with the existing live policy/approval system. Suggested distinctions: read permitted schedules, manage own sources/timetable, manage shared sources, edit another member's timetable, and calendar writes. Do not hardcode “all members allowed” or rely on system-prompt claims. Existing admin delegation must continue to work in natural language.

Recheck at proposal, confirmation, background refresh where relevant, response disclosure, and any later scheduled action. Subscription metadata is not authority to message attendees or edit events. Visibility filtering must happen before event details enter the model prompt. Revoked membership must stop private data access and pending writes.

Keep user identity, quoted author, timetable owner, and calendar owner separate. No source can turn another person into the creator. Missing data or provider outages must yield an actionable explanation and unknown/stale coverage, never an invented empty calendar or a PDF-evidence error.

## 7. Implementation sequence and acceptance tests

Work in small patches. After each phase, port generic code/tests/docs to the template and preserve private configuration only in the personal deployment.

**Phase A — prerequisites and schema.** Resolve/document the unfinished items in section 0. Add migrations, source/binding/import models, permissions, and link parser. Test migration against existing databases; restart without data loss.

**Phase B — Google read subscriptions.** Implement inspect/preview/confirm, access checks, bounded refresh, agenda and conflict integration. Tests: raw ID, encoded `src`, base64 and raw `cid`, multiple calendars, malformed link, event link, duplicates, 403/404, paginated responses, moved/cancelled recurrence, exclusive all-day end, DST (including Africa/Cairo), stale cache, private event masking, wrong-member bindings, subscription removal, semester expiry, and changed permissions. Do not send invitations during tests.

**Phase C — image transport and general reasoning.** WhatsApp direct/replied image handling is implemented and passes a live synthetic-image Gemini check. Extend it to web uploads with the same validation rules; keep testing the existing path. Tests: unauthorized sender is rejected before expensive work; unmentioned group photo stays quiet; quoted author never becomes current speaker; simultaneous attachments cannot cross users; invalid token/path, corrupt image, huge decoded dimensions, oversized stream, EXIF rotation, timeout, cleanup, and provider quota. Model responses may be mocked, but validate the actual binary decoding path.

**Phase D — timetable imports.** Implement typed extraction, draft review, correction, confirmation, versioned writes, and availability results. Use checked-in nonpersonal Arabic/English image fixtures, including deliberately ambiguous and unreadable examples. Test intervals crossing midnight, alternate-week schedules, missing term bounds, wrong requester, wrong chat, expired draft, changed draft, same-turn confirmation, duplicate import, revoked editor, and selective replacement/removal.

**Phase E — end-to-end and documentation.** Run complete Python/Node suites and lint. Exercise owner-controlled real Calendar reads and image questions. Confirm one timetable import against a manually checked fixture; inspect stored rows and conflict answers. Test an inaccessible calendar and unreadable image so failures remain useful. Distinguish mocked tests from real provider results in the completion report. Check the template's tracked and newly added files for secrets and personal details; never copy local databases, OAuth tokens, media, or credentials.

Acceptance conversations:

- “Subscribe to this lecture calendar for Alice and Omar this semester.” → Verified source and explicit preview; later confirmed mapping is durable.
- “Can we meet next Tuesday at 6 pm for an hour?” → Relevant lectures/manual schedules/shared events considered, with timezone and missing coverage disclosed.
- “This timetable is mine; remember it.” + image → Correct authenticated member, readable draft, questions for uncertainty, separate confirmation, then structured availability.
- Reply to a circuit/diagram image with “@assistant explain this.” → Image-informed answer without an unsolicited schedule import or permanent upload.
- “Stop using that lecture calendar.” → Authorized removal of its contribution; external events and unrelated personal schedules remain intact.
- Revoke schedule-edit permission before confirming a draft → No writes, with a clear explanation.

## Required completion report from the implementing model

List changed files and migrations, what actually works, what remains incomplete, exact test counts, external calls exercised, and any setup required. Include representative previews and failure responses. Never mark a feature complete merely because a tool schema exists or a stub returns success. Do not claim 24/7 operation: refreshes and reminders still require this local backend to be running.
