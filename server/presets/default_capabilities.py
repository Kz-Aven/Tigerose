"""Default capability packs for preset assistants."""

from __future__ import annotations

# Core coding surface — enough to read/edit/run in a workspace.
CODING_TOOLS: list[str] = [
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "bash",
    "get_skill",
    "todo_write",
    "compact",
    "excel_read",
    "excel_write",
]

# Task board + light coordination (Coordinator / Review).
BOARD_TOOLS: list[str] = [
    "create_task",
    "list_tasks",
    "get_task",
    "claim_task",
    "complete_task",
    "fail_task",
    "cancel_task",
    "release_task",
]

# Full server tool surface (dangerous tools included; executor still gates).
FULL_TOOLS: list[str] = [
    *CODING_TOOLS,
    "remember",
    "task",
    *BOARD_TOOLS,
    "delete_task",
    "archive_completed_tasks",
    "schedule_cron",
    "list_crons",
    "cancel_cron",
    "spawn_teammate",
    "send_message",
    "check_inbox",
    "request_shutdown",
    "request_plan",
    "review_plan",
    "create_worktree",
    "remove_worktree",
    "keep_worktree",
    "web_search",
    "web_extract",
]

# Role → default capabilities (skills/plugins/mcp stay empty until user enables).
ROLE_DEFAULT_CAPABILITIES: dict[str, dict[str, list[str]]] = {
    "Code": {
        "skills": [],
        "plugins": [],
        "tools": list(FULL_TOOLS),
        "mcp_servers": ["mcp:dingtalk_calendar"],
    },
    "Research": {
        "skills": [],
        "plugins": [],
        "tools": [
            "read_file",
            "glob",
            "bash",
            "get_skill",
            "todo_write",
            "remember",
            "compact",
            "web_search",
            "web_extract",
            "excel_read",
            "excel_write",
        ],
        "mcp_servers": [],
    },
    "Review": {
        "skills": [],
        "plugins": [],
        "tools": ["read_file", "glob", "bash", "get_skill", "todo_write", "compact", *BOARD_TOOLS],
        "mcp_servers": [],
    },
    "Coordinator": {
        "skills": [],
        "plugins": [],
        "tools": [
            "read_file",
            "glob",
            "todo_write",
            "compact",
            "remember",
            *BOARD_TOOLS,
            "schedule_cron",
            "list_crons",
            "cancel_cron",
            "spawn_teammate",
            "send_message",
            "check_inbox",
        ],
        "mcp_servers": [],
    },
}


def defaults_for_role(role: str, name: str = "") -> dict[str, list[str]]:
    key = (role or name or "").strip()
    if key in ROLE_DEFAULT_CAPABILITIES:
        return {k: list(v) for k, v in ROLE_DEFAULT_CAPABILITIES[key].items()}
    # Unknown custom assistants: coding tools so they can work out of the box.
    return {
        "skills": [],
        "plugins": [],
        "tools": list(CODING_TOOLS),
        "mcp_servers": [],
    }


def is_empty_capabilities(caps: dict | None) -> bool:
    if not isinstance(caps, dict):
        return True
    return not any(caps.get(k) for k in ("skills", "plugins", "tools", "mcp_servers"))
