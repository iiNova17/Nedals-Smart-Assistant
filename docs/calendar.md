# Calendar and availability

Calendar uses backend OAuth; the model never receives credentials. Sharing a calendar and enabling the Calendar API are separate requirements.

1. Enable Calendar API in the Cloud project used for the Desktop OAuth client.
2. Share the team calendar with the account authorizing the bot, with **Make changes to events** for writes.
3. Run `.\.venv\Scripts\python.exe -m app.calendar_auth`. This saves `secrets/google-calendar-token.json` separately from Drive authorization.
4. Tell the assistant the shared Calendar ID as the owner. Find it in Calendar settings → Integrate calendar.

Scopes are `calendar.events.freebusy` and `calendar.events`. A shared-calendar 404 may mean the account cannot access that ID. A working primary calendar does not prove shared-calendar access. Token presence means configured authorization; live calls verify actual access.

“What is on our calendar this week?” calls Calendar directly. Relative weeks use the Cairo team’s Saturday–Friday convention; answers identify the dates. Relative dates use the requester’s timezone, default Africa/Cairo. Missing duration, ambiguous event identity or ambiguous DST times warrant a short clarification; users need not supply ISO dates.

Approved members may request event changes by default. The owner can require approval through a `calendar_write` capability rule, globally or per member. Optional commands pass through the same enforcement. Recurring Calendar series/instances must currently be edited in Google Calendar; bot reminders support recurrence independently. Event writes do not send Google attendee invitations or enable default Google alarms.

Each person can provide weekly or dated busy intervals. Records belong to that person. Conflict answers reveal intervals, not private labels. Personal Calendar IDs need sharing with the connected account and a personal mapping. An empty timetable means **unknown** unless the member declares it complete through a date. An inaccessible linked calendar also means unknown. Shared-calendar conflicts are listed separately.

Approval requests persist for 24 hours and use the WhatsApp outbox. Any listed approver may approve; an all-approvers-must-agree workflow is not implemented. Account access and current rules are rechecked before execution. Crashed or ambiguous writes remain uncertain rather than being silently repeated.

Reminders run locally. One-off, weekly and every-N-day schedules use saved local time. Occurrences missed by more than 15 minutes while offline are skipped rather than flooding the group after restart.
