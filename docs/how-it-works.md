# How the local assistant works

Two hidden processes run on the owner’s Windows computer: FastAPI on 127.0.0.1:8000 and the Baileys bridge on 127.0.0.1:8787. WhatsApp uses a persistent linked-device connection. Closing the browser does not stop them; sleeping/shutting down the PC does. Start/stop scripts track process IDs/start times and preserve login.

The bridge resolves senders, checks access and queues addressed group messages or DMs durably. The backend checks authorization independently and loads conversation context, editable project background and verified sender identity. Gemini interprets the request and calls bounded tools. Natural introductions and memory requests are no longer intercepted into canned responses.

Backend services own credentials and enforce permissions. Approval rules persist an exact action and notify designated people through WhatsApp. The assistant distinguishes queued, pending approval, completed and uncertain states. It cannot grant itself authority through prompt text.

Drive is the canonical repository. Uploads validate size/name/signature, hash bytes and reuse duplicates. Missing destinations use Inbox; same-name different-content files stay separate. Preallocated Drive IDs make retries safer. Temporary bytes are removed after processing. A two-minute PDF scan maps derived Gemini File Search documents to Drive IDs and revisions. Answers return mapped source links and valid supplied page references. Non-PDF files remain searchable by metadata.

SQLite stores users, bounded recent history, decisions/revisions, work-package status, index metadata, schedules, live permissions, approvals and reminders. `data/whatsapp/session.sqlite3` separately stores WhatsApp credentials/keys and message jobs. `.env` and `secrets/` hold API/OAuth secrets, never passed to the model.

The web page uses the same backend. A same-origin loopback request establishes an HttpOnly owner session without token entry. This is ready for the owner’s machine; public hosting needs a separate authentication/deployment design. Each tab has its own conversation; theme preference persists in the browser.

No-key web search supplies external snippets and source links. It can fail or rate-limit and does not pretend to have read full pages. Conversation prefers Gemini 3.5 Flash with a quota fallback to Flash-Lite; PDF retrieval uses Flash-Lite. No paid account change or cloud deployment was made.
