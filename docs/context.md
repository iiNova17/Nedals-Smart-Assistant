# Context, memory and live permissions

`context/local/identity.json` defines name, creator, purpose and style. `project.json` holds background and authority rules. `team.json` holds team descriptions. They reload for each model turn and are excluded from Git; generic examples are in `context/examples/`.

The supplied PLUME background is installed locally. Work-package status is separate: SQL `work_packages` and `work_package_history` contain current status and provenance. Local `status-seed.json` records the one-time starting state; it is not automatically reapplied. Restarting does not reset later updates. Mentioning a component candidate does not finalize its selection.

`runtime_config`, `access_members`, `registered_groups` and `capability_rules` hold live settings and authorization. Context files cannot grant permissions. Sender identity comes from WhatsApp provider mappings or the backend session, never someone claiming to be Nedal in a message.

Modes: public, whitelist, owner. Blocks override public access. The permanent owner remains protected. The current default is whitelist. Group triggers remain mention/reply/optional shortcut or document unless changed to all.

Capabilities: memory, personal_schedule, calendar_write, members, settings, groups, reminders, drive_write, project_status, context, send_message. Global rules use `*`; member-specific rules use the canonical phone and override the global rule. Effects: allow, deny, approval, reset. Only the owner changes these rules. A scoped grant neither appoints an admin nor grants access to another person’s private timetable. Promotion and opening public access require owner confirmation.

Recent chat, durable decisions and work-package status are distinct. Explicit “remember” saves shared memory; ordinary conversation does not automatically become a fact. Replacement preserves the old decision; forgetting retires it from active retrieval while retaining audit history. Timetables have a separate store. DM history is not automatically sent to groups.

Natural requests become structured tool arguments. The backend checks every tool mutation and saves a receipt per incoming request. Approval captures a specific action rather than granting blanket authority. Duplicate deliveries reuse committed results. A crash after starting a write produces an uncertain result that requires inspection before retrying.
