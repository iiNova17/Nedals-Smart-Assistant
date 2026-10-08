"""Model-facing structured actions; the user never needs the internal command syntax."""

ACTIONS = [
    "memory_save",
    "memory_replace",
    "memory_forget",
    "schedule_add",
    "schedule_remove",
    "schedule_complete",
    "timezone_set",
    "calendar_link",
    "event_create",
    "event_reschedule",
    "event_cancel",
    "member_update",
    "settings_update",
    "group_manage",
    "reminder_create",
    "reminder_cancel",
    "work_package_update",
    "context_update",
    "drive_folder",
    "drive_rename",
    "drive_move",
    "drive_report",
    "permission_set",
    "message_send",
    "approval_resolve",
]
ACTION_DESCRIPTION = """Execute one explicitly requested action using live backend permissions.
Ordinary authorized actions execute immediately; configured approval rules hold them
automatically.
memory_save: title,text; memory_replace: id,title,text; memory_forget: id.
schedule_add: start,end (ISO date/times, or HH:MM with weekdays=Mon,Wed),title;
schedule_remove: id; schedule_complete: end=YYYY-MM-DD only if user says the timetable is
complete.
timezone_set: timezone; calendar_link: target=Calendar ID or unlink.
event_create: title,start,end,text=optional description; event_reschedule: id,start,end;
event_cancel: id. Uses shared calendar. Infer relative dates; ask only for missing intent.
member_update: operation=whitelist|block|unblock|remove|promote|demote|member,
target=full phone; title=name; member profile additionally role,text=duties.
settings_update: operation=mode target=public|whitelist|owner; trigger target=mention|all;
pause/resume (no target); limit target='user 40' or 'team 200'; calendar target='shared
CALENDAR_ID'.
group_manage: operation=remember,target=group name; target/forget use target=known
group ID.
reminder_create: recurrence='once ISO_DATETIME' or 'daily N HH:MM' or 'weekly Mon,Wed
HH:MM',text.
Uses current group or saved reminder target. reminder_cancel: id.
work_package_update: title=name,status=active|planned|awaiting
planning|proposed|completed|blocked,text.
context_update: target=identity|project|team,text=complete JSON object (preserve other
known fields).
drive_folder: title=name,target=optional parent ID; drive_rename: id,target=new name;
drive_move: id,target=folder ID; drive_report: title,text=complete composed Markdown
report.
permission_set:
capability=memory|personal_schedule|calendar_write|members|settings|groups|
reminders|drive_write|project_status|context|send_message; target=* or full member phone;
effect=allow|deny|approval|reset; approvers=phone list for approval (ANY listed person may
approve).
Owner only. More specific member rules override the global rule. Does not grant admin
status.
message_send: target=approved full phone or registered group ID,text=exact message to
send.
approval_resolve: id=real pending request ID,decision=approve|reject.
Do not send slash commands to the user. Report actual result or approval state naturally."""

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        **{
            k: {"type": "string"}
            for k in [
                "id",
                "title",
                "text",
                "start",
                "end",
                "weekdays",
                "timezone",
                "target",
                "operation",
                "role",
                "recurrence",
                "status",
                "capability",
                "effect",
                "decision",
            ]
        },
        "approvers": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    },
    "required": ["action"],
    "additionalProperties": False,
}

ACTION_GROUPS = {
    "project_memory_write": {
        "actions": ["memory_save", "memory_replace", "memory_forget"],
        "fields": ["id", "title", "text"],
        "description": "Save, replace or retire a shared project decision on explicit request. "
        "Save uses title/text; replace uses id/title/text; forget uses id. "
        "Retrieve existing IDs with project_memory. Available to approved members.",
    },
    "personal_schedule_update": {
        "actions": [
            "schedule_add",
            "schedule_remove",
            "schedule_complete",
            "timezone_set",
            "calendar_link",
        ],
        "fields": ["id", "title", "start", "end", "weekdays", "timezone", "target"],
        "description": "Maintain the current user's PRIVATE busy timetable, not shared meetings. "
        "schedule_add uses start/end ISO dates, or HH:MM with weekdays=Mon,Wed for classes. "
        "schedule_remove uses id; schedule_complete end=YYYY-MM-DD only if declared complete. "
        "timezone_set uses timezone; calendar_link uses target=Calendar ID or unlink. "
        "For scheduling a team meeting, ALWAYS use calendar_event_write instead.",
    },
    "calendar_event_write": {
        "actions": ["event_create", "event_reschedule", "event_cancel"],
        "fields": ["id", "title", "text", "start", "end"],
        "description": "Schedule, reschedule or cancel a real meeting on the SHARED GOOGLE "
        "CALENDAR. Available to all approved members subject to dynamic permission/approval "
        "rules. event_create uses title,start,end (ISO inferred from natural dates), optional "
        "text=description. event_reschedule uses id,start,end; event_cancel uses id. Retrieve "
        "IDs with calendar_events. Distinct from a person's class timetable.",
    },
}


def action_declarations():
    from google.genai import types

    separated = {a for group in ACTION_GROUPS.values() for a in group["actions"]}
    declarations = []
    for name, group in ACTION_GROUPS.items():
        properties = {k: v for k, v in ACTION_SCHEMA["properties"].items() if k in group["fields"]}
        properties["action"] = {"type": "string", "enum": group["actions"]}
        declarations.append(
            types.FunctionDeclaration(
                name=name,
                description=group["description"],
                parameters_json_schema={
                    "type": "object",
                    "properties": properties,
                    "required": ["action"],
                    "additionalProperties": False,
                },
            )
        )
    properties = {
        **ACTION_SCHEMA["properties"],
        "action": {"type": "string", "enum": [a for a in ACTIONS if a not in separated]},
    }
    declarations.append(
        types.FunctionDeclaration(
            name="assistant_action",
            description=ACTION_DESCRIPTION,
            parameters_json_schema={**ACTION_SCHEMA, "properties": properties},
        )
    )
    return declarations
