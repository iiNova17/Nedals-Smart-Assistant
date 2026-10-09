"""Small natural-language tool boundary for shared availability records."""

from app.team_availability import TeamAvailability

DEFINITIONS = [
    (
        "schedule_source_propose",
        "Preview adding, replacing or removing a Google Calendar source or extracted timetable. "
        "Persists after separate confirmation. Calendar busy times refresh on each query. "
        "Resolve team_directory IDs. For images transcribe visible slots; ask about "
        "unclear cells and semester dates first. complete=true ONLY if user explicitly says this "
        "covers their full availability. Never infer that from a lecture-only calendar.",
        {
            "operation": {"type": "string", "enum": ["add", "replace", "remove"]},
            "source_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["calendar", "timetable"]},
            "label": {"type": "string"},
            "members": {"type": "array", "items": {"type": "string"}},
            "calendar_link": {"type": "string"},
            "timezone": {"type": "string"},
            "valid_from": {"type": "string", "description": "First date YYYY-MM-DD"},
            "valid_until": {"type": "string", "description": "Last date YYYY-MM-DD inclusive"},
            "complete": {"type": "boolean"},
            "from_image": {"type": "boolean"},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
            "excluded_dates": {"type": "array", "items": {"type": "string"}},
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "weekdays": {"type": "array", "items": {"type": "integer"}},
                        "start": {"type": "string", "description": "HH:MM"},
                        "end": {
                            "type": "string",
                            "description": "HH:MM; earlier than start means next day",
                        },
                        "label": {"type": "string"},
                        "every_weeks": {"type": "integer"},
                        "anchor_date": {
                            "type": "string",
                            "description": "A date in an active week, YYYY-MM-DD",
                        },
                    },
                    "required": ["weekdays", "start", "end"],
                    "additionalProperties": False,
                },
            },
        },
        ["operation"],
    ),
    (
        "schedule_source_confirm",
        "Commit the exact proposed calendar/timetable mapping only after "
        "its requester confirms the preview separately. Permission approvals are enforced.",
        {"proposal_id": {"type": "string"}},
        [],
    ),
    (
        "schedule_sources",
        "List recorded calendar/timetable mappings, validity, IDs and check status. "
        "Resolve IDs before editing/forgetting a source. Timetable labels stay out of groups.",
        {},
        [],
    ),
    (
        "meeting_suggestions",
        "Find nonconflicting meeting slots using all selected members' "
        "Google calendars and timetables plus the shared project calendar. Infer concrete "
        "dates from the current date. Honor duration, daily hours and weekdays. Missing schedules "
        "make suggestions tentative, never guaranteed free. This does not create an event.",
        {
            "start": {"type": "string"},
            "end": {"type": "string"},
            "timezone": {"type": "string"},
            "members": {"type": "array", "items": {"type": "string"}},
            "duration_minutes": {"type": "integer"},
            "buffer_minutes": {
                "type": "integer",
                "description": "Break before/after busy times, default 0",
            },
            "day_start": {"type": "string"},
            "day_end": {"type": "string"},
            "weekdays": {"type": "array", "items": {"type": "integer"}},
            "limit": {"type": "integer"},
        },
        ["start", "end"],
    ),
]


def execute(commands, actor, name, arguments):
    actor = commands.actions.fresh(actor)
    if not actor.trusted:
        raise PermissionError("Approved team membership is required")
    if name == "schedule_source_propose":
        return commands.schedule_sources.propose(actor, arguments)
    if name == "schedule_source_confirm":
        return commands.schedule_sources.confirm(actor, arguments.get("proposal_id", ""))
    if name == "schedule_sources":
        return commands.schedule_sources.list(actor)
    if name == "meeting_suggestions":
        args = dict(arguments)
        members = commands.schedule_sources.members(args.pop("members", None))
        args["member_ids"] = [m["id"] for m in members]
        args.setdefault("timezone", commands.schedule.timezone(actor.user.id))
        return TeamAvailability(commands.schedule).suggest(**args)
    raise ValueError("Unknown availability tool")
