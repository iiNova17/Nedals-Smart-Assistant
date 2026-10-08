# Optional command reference

Natural language is the normal interface. These shortcuts are for diagnosis/recovery and pass through the same live permission rules.

```text
Chat: ask normally; use /assistant or mention/reply in groups.
/about — identity, project and capabilities
/status — current editable work-package status
/remember subject | decision — explicitly save a shared decision
/memories [query] — search active decisions
/replace ID | subject | new decision — retain the old decision in history
/forget ID — retire your decision (admins can retire any)
/history [query] — superseded decisions
/busy START END | label — add your busy interval (ISO date/time)
/busy weekly Mon,Wed 09:00 11:00 | label — weekly timetable
/schedule — list your timetable; /schedule remove ID
/schedule complete YYYY-MM-DD — declare manual timetable complete through this date
/timezone Africa/Cairo — set your timezone
/availability START END — check team conflicts and unknown availability
/calendar ID — link a calendar shared with the connected Google account
/calendar unlink — disconnect your calendar mapping
/confirm ID — execute your reviewed command proposal
/help admin — admin controls
Dates use your saved timezone unless an explicit offset is included.

Admin commands:
/admin status | members | groups | reminders | audit
/admin mode public|whitelist|owner
/admin whitelist +PHONE | Name — approve a regular member
/admin remove +PHONE — remove whitelist access
/admin block +PHONE | unblock +PHONE — block takes precedence over public mode
/admin promote +PHONE | demote +PHONE — permanent owner only
/admin member +PHONE | Name | Project role | Duties
/admin group remember Name — register this group and select it for reminders
/admin group target GROUP_ID — select a registered reminder target
/admin group forget GROUP_ID — disable group and cancel pending reminders
/admin trigger mention|all — group trigger behavior
/admin pause | resume — suspend ordinary replies and reminder delivery
/admin context identity|project|team | JSON_OBJECT
/admin work-package Name | active|planned|awaiting planning|proposed|completed|blocked | Notes
/admin calendar shared CALENDAR_ID
/admin event create START END | Title | Description
/admin event reschedule EVENT_ID START END
/admin event list START END
/admin event cancel EVENT_ID
/admin remind once YYYY-MM-DDTHH:MM | Message
/admin remind daily N HH:MM | Message — every N days, starting at next occurrence
/admin remind weekly Mon,Wed HH:MM | Message
/admin cancel REMINDER_ID
/admin limit user|team NUMBER — daily request caps
/admin reset-context — clear this conversation history
/admin report Title | Content — create a Markdown report in Drive
/admin folder Name | PARENT_ID — optional parent defaults to project root
/admin rename FILE_ID | New name
/admin move FILE_ID | DESTINATION_FOLDER_ID
Use /help for member commands. Admin roles are granted only by the permanent owner.
Public mode exposes shared project knowledge to anyone who contacts the bot.
Reminders run only while this computer and the bridge are online; old occurrences are skipped.
```
