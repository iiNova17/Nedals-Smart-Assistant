# Personal calendars, timetable photos and meeting times

These features use the existing local backend, Gemini client and Google OAuth connection. No new hosted service is needed. The application stores member-to-source mappings and confirmed timetable intervals in SQLite, separately from conversation history and project decisions.

## Private reminders and group mentions

Ask naturally:

- “Remind me privately tomorrow at 6 pm to drink water.”
- “Remind Alex and Sam in this group on Friday at 10 am to bring the report.”
- “Send Alex a private reminder on Monday morning.”

The assistant resolves “me” from the authenticated sender. Private reminders use the member's canonical WhatsApp number, including when the incoming message used a WhatsApp LID. The preview names the private recipient or destination group and shows the exact time, timezone, text and recurrence. A separate confirmation is still required. Group reminders carry real WhatsApp mention metadata for the selected people; the requester is the default recipient. Merely sending a private reminder does not copy private conversation history into a group.

The `reminders` capability and any configured approval rule still apply. Unknown, blocked or inactive recipients are rejected. Current permission is checked again when the reminder runs and when it is delivered. Delivery remains dependent on the backend being online. A queued message is not proof that WhatsApp delivered it; uncertain sends are not automatically repeated.

## Google Calendar subscriptions

Example: “Subscribe to this university calendar for Alex and Sam.” Send its Google Calendar link or explicit calendar ID. Supported links include embed URLs with `src` and sharing URLs with raw or base64-encoded `cid`. An account selector such as `/u/3` does not identify the account the backend uses.

The assistant verifies access and previews the member mapping before confirmation. Multiple calendars can apply to one person, and a calendar can apply to multiple people. Each subscription is stored in this application and read when availability is requested. It does not copy events into the project calendar, modify the university calendar, or add the source to the connected Google account's sidebar.

Calendar subscriptions default to starting today and continuing until unlinked. You can restrict them to a semester or other date range. **All busy events on a subscribed calendar apply to the mapped members.** For a calendar containing unrelated classes or sections, use a more specific calendar or a confirmed timetable instead of mapping the entire calendar indiscriminately. ICS/webcal feeds and event-title filters are not implemented.

Private calendars must be shared with the backend's connected Google account with permission to see free/busy information. A link alone does not grant access. Existing Calendar OAuth scopes support the availability request; no broader scope is added for subscriptions. Failed access produces an explanation and does not create a confirmed source.

Meeting checks query calendar free/busy data live in a single bounded batch, including the shared project calendar and older personal-calendar mappings. Google handles recurring events and exceptions. The application stores check status, not private event titles. Provider errors produce unknown availability, never an empty “free” calendar. [Google free/busy API reference](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query).

## Timetable photos

Send or quote a clear timetable photo with a request such as:

“Remember this as my timetable, valid from October 15 to January 20, in Europe/London. It is my complete schedule for that period.”

In a group, mention the bot in the caption or while replying to the image. Gemini extracts the visible day/time intervals. The assistant asks about unreadable cells, ambiguous times, semester boundaries and who the timetable applies to, then presents a complete preview. Only a separate confirmation writes the structured schedule. If the image is no longer attached when more visual inspection is needed, quote or resend it; image pixels are not retained in chat history.

Supported timetable fields include weekdays, start/end times, timezone, validity dates, excluded dates such as holidays, and repetition every one to four weeks with an anchor date. An end time earlier than the start means the following day. Timetables are limited to one year per import so an old semester is not silently treated as permanent availability. Ambiguous daylight-saving intervals are reported as unknown.

An import defaults to **partial coverage**. A lecture-only timetable is not automatically the person's full availability. Mark a schedule complete only when the person explicitly confirms that it covers their availability for the stated period. Busy-time queries expose intervals and coverage status, not private course names or labels.

Imports have durable IDs, source-image hashes when supplied, author records and revision history. Ask to list, replace or forget a particular source. Replacement and removal use another preview/confirmation and affect only the selected source; unrelated manual slots and Google events are preserved. Identical confirmed imports are deduplicated. Expired or superseded records remain available for audit but do not silently become current schedules.

## Checking and suggesting meeting times

- “Does Tuesday at 6 pm work for everyone for an hour?”
- “Find a 90-minute slot for Alex and Sam next week, weekdays only, between 2 and 6 pm.”
- “Give us three options on Thursday, leaving a 30-minute break before and after classes.”

The assistant combines active members' manual slots, confirmed timetable imports, calendar subscriptions and the shared project calendar. Constraints include a query interval of up to 31 days, selected members, duration, permitted weekdays, daily hours, a buffer around busy times, and a bounded number of suggestions. Results include the timezone and check time. A class ending at noon does not conflict with a meeting starting at noon unless a buffer was requested.

If any relevant schedule is missing, partial, expired or inaccessible, suggested times are marked **tentative** and identify whose coverage is unknown. “No recorded conflicts” is never a guarantee of availability. Finding a slot does not create a meeting or invite anyone; calendar writes remain separate permission-checked actions.

## Permissions and approvals

- `personal_schedule` controls managing one's own sources and timetables.
- `schedule_manage_others` additionally controls changes to other members' records. It starts admin-only and can be granted by the owner.
- Existing allow, deny and designated-approver rules apply at preview, confirmation and approval execution. A confirmation is not a permission grant.
- Source proposals are scoped to their requester and conversation, expire after 15 minutes, and cannot be confirmed in the same model request that created them. Permission approval requests use the existing 24-hour approval workflow.
- If several proposals are pending, the assistant resolves which one is being confirmed. Labels and detailed source data are omitted from group source listings.

The permanent owner can manage these permissions conversationally. Normal access modes control who can interact with the assistant; switching to owner-only mode does not erase the rest of the team's recorded availability.

## Verification

Regression tests cover private DM destination resolution, real group mention payloads, separate confirmation, duplicate imports, revision/removal, permission approvals and revocation, link parsing, batched Google errors, live-result changes, partial coverage, recurring/overnight slots, holidays, DST ambiguity and constrained meeting suggestions. Tests use isolated databases and mocked providers. Development also verified natural-language private-reminder preview and timetable-photo extraction against Gemini, and performed a read-only check of a configured shared calendar. No unsolicited member messages or external calendar writes were used for those checks.
