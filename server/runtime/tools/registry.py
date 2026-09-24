"""Single source of truth: executable builtin tool schemas for server runtime."""

from __future__ import annotations

import re
from typing import Any

# name -> {description, parameters, dangerous?}
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "ask_user_question": {
        "description": (
            "Ask the user 1-4 structured questions when critical information is missing. "
            "Execution pauses without a timeout and resumes after the UI supplies answers."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "header": {"type": "string"},
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multi_select": {"type": "boolean"},
                        },
                        "required": [
                            "id",
                            "header",
                            "question",
                            "options",
                            "multi_select",
                        ],
                    },
                }
            },
            "required": ["questions"],
        },
    },
    "request_clear_session": {
        "description": (
            "Request user approval to clear the current chat session. "
            "Never clears silently — shows an authorization banner. "
            "On approve, archives an immutable snapshot then clears messages."
        ),
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why clearing is needed (shown to the user).",
                }
            },
            "required": ["reason"],
        },
    },
    "read_file": {
        "description": (
            "Read text file contents in the workspace. If the user specifies an external "
            "absolute path, call this tool and the system will request permission. "
            "For .xlsx use excel_read instead."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "description": (
            "Write text content to a file (side-effect). If the user specifies an external "
            "absolute path, call this tool and the system will request permission. "
            "For .xlsx use excel_write instead."
        ),
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "description": (
            "Replace exact text in a text file once (side-effect). If the user specifies an "
            "external absolute path, call this tool and the system will request permission. "
            "For .xlsx use excel_write instead."
        ),
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    "glob": {
        "description": "Find files matching a glob pattern under workspace.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    "bash": {
        "description": "Run a shell command in workspace (side-effect). With run_in_background=true returns a job_id immediately. Use bash_wait to collect its final output before submitting results or ending the turn; unfinished jobs are stopped when the turn ends.",
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "run_in_background": {"type": "boolean"},
            },
            "required": ["command"],
        },
    },
    "get_skill": {
        "description": "Load an enabled skill by name or id.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    "remember": {
        "description": "Persist a reusable memory for this assistant.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "type": {"type": "string", "enum": ["user", "feedback", "project", "reference", "unknown"]},
                "summary": {"type": "string"},
            },
            "required": ["body"],
        },
    },
    "todo_write": {
        "description": "Replace the current session todo list.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    },
    "compact": {
        "description": "Summarize and compress earlier conversation history.",
        "dangerous": False,
        "parameters": {"type": "object", "properties": {}},
    },
    "task": {
        "description": "Delegate a one-shot sub-agent; returns a short summary (no further spawn).",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    "create_task": {
        "description": "Create a task on the persistent task board.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "blocked_by": {"type": "array", "items": {"type": "string"}},
                "bind_to_goal": {
                    "type": "boolean",
                    "description": (
                        "Whether to bind this task to the active Goal. "
                        "Default true; set false for temporary/gate-check tasks. "
                        "Gate-theatre titles are never bound."
                    ),
                },
            },
            "required": ["subject"],
        },
    },
    "list_tasks": {
        "description": "List tasks on the board for this scope.",
        "dangerous": False,
        "parameters": {"type": "object", "properties": {}},
    },
    "get_task": {
        "description": "Get one task by id.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    "claim_task": {
        "description": "Claim a pending task.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "owner": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    "complete_task": {
        "description": "Mark an in-progress task completed.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    "fail_task": {
        "description": "Mark a task failed.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    "cancel_task": {
        "description": "Cancel a task.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    "release_task": {
        "description": "Release an in-progress task back to pending.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    "delete_task": {
        "description": "Delete a task from the board.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    "archive_completed_tasks": {
        "description": "Archive completed tasks older than N days.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"older_than_days": {"type": "integer"}},
        },
    },
    "schedule_cron": {
        "description": (
            "Schedule a job. Provide either a 5-field cron expression, "
            "or delay_seconds for a one-shot reminder."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "cron": {"type": "string", "description": "5-field cron (min hour dom month dow)"},
                "delay_seconds": {"type": "integer", "description": "One-shot delay from now"},
                "prompt": {"type": "string"},
                "recurring": {"type": "boolean"},
            },
            "required": ["prompt"],
        },
    },
    "list_crons": {
        "description": "List scheduled cron / delay jobs for this assistant.",
        "dangerous": False,
        "parameters": {"type": "object", "properties": {}},
    },
    "cancel_cron": {
        "description": "Cancel a scheduled job by id.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    "spawn_teammate": {
        "description": "Spawn a background teammate agent (plan may require review_plan).",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "requires_plan": {"type": "boolean"},
            },
            "required": ["name", "prompt"],
        },
    },
    "send_message": {
        "description": "Send a message to a teammate by name.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["to", "content"],
        },
    },
    "check_inbox": {
        "description": "Check inbox messages for this agent / teammate name.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    },
    "request_shutdown": {
        "description": "Request graceful shutdown of a teammate.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    "request_plan": {
        "description": "Teammate submits a plan for Lead approval.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "plan": {"type": "string"},
            },
            "required": ["name", "plan"],
        },
    },
    "review_plan": {
        "description": "Lead approves or rejects a teammate plan.",
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "approve": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["name", "approve"],
        },
    },
    "create_worktree": {
        "description": "Create an isolated git worktree bound to an optional task.",
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    "remove_worktree": {
        "description": "Remove a git worktree.",
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "discard_changes": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    "keep_worktree": {
        "description": "Mark a worktree as kept (do not auto-remove).",
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    "web_search": {
        "description": (
            "Search the public web (DuckDuckGo). Returns titles, URLs, and snippets. "
            "Use when you need current facts or sources; follow up with web_extract on key URLs."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    "web_extract": {
        "description": (
            "Fetch and extract readable content from one or more URLs via Tavily. "
            "Prefer after web_search when you need full page text."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "url": {"type": "string"},
            },
        },
    },
    "excel_read": {
        "description": (
            "Read an Excel .xlsx workbook into markdown table or CSV text. "
            "Use this for .xlsx — do NOT use read_file on binary Office files."
        ),
        "dangerous": False,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "sheet": {"type": "string"},
                "range": {"type": "string", "description": "A1 range, e.g. A1:G50"},
                "max_rows": {"type": "integer"},
                "format": {"type": "string", "enum": ["markdown", "csv"]},
            },
            "required": ["path"],
        },
    },
    "excel_write": {
        "description": (
            "Create or update an Excel .xlsx file (set cells, append/replace rows). "
            "Prefers inplace save; falls back to *_edited.xlsx or CSV and reports mode. "
            "Do NOT use write_file/edit_file for .xlsx."
        ),
        "dangerous": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "sheet": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["set_cells", "append_rows", "replace_sheet", "export_csv"],
                },
                "cells": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Map of A1 refs to values for set_cells",
                },
                "rows": {
                    "type": "array",
                    "items": {"type": "array", "items": {}},
                },
                "range": {"type": "string"},
                "prefer": {
                    "type": "string",
                    "enum": ["inplace", "sidecar", "csv"],
                },
            },
            "required": ["path", "mode"],
        },
    },
}

from server.runtime.tools.mesh import MESH_TOOL_SPECS, QUERY_NAMES as MESH_QUERY_NAMES, CONTROL_NAMES as MESH_CONTROL_NAMES

for _name in ("bash_status", "bash_wait", "bash_cancel"):
    TOOL_SPECS[_name] = {
        "description": {"bash_status": "Read status and output of your background bash job.", "bash_wait": "Wait up to 20 seconds for your background bash job and return status/output; repeat if still running.", "bash_cancel": "Stop your background bash process group."}[_name],
        "dangerous": False,
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, **({"timeout_s": {"type": "number", "minimum": 0, "maximum": 20}} if _name == "bash_wait" else {})}, "required": ["job_id"]},
    }
TOOL_SPECS.update(MESH_TOOL_SPECS)
DANGEROUS_TOOLS = frozenset(n for n, s in TOOL_SPECS.items() if s.get("dangerous"))

# Query tools never enter Goal unresolved_tool_failures.
QUERY_TOOLS = frozenset(
    {
        "bash_status", "bash_wait",
        "read_file",
        "glob",
        "list_tasks",
        "get_task",
        "get_skill",
        "web_search",
        "web_extract",
        "excel_read",
        "list_crons",
        "check_inbox",
    }
)
# Control / meta tools — also never Goal-unresolved.
CONTROL_TOOLS = frozenset(
    {
        "bash_cancel",
        "ask_user_question",
        "request_clear_session",
        "compact",
        "check_inbox",
    }
)

QUERY_TOOLS = QUERY_TOOLS | MESH_QUERY_NAMES
CONTROL_TOOLS = CONTROL_TOOLS | MESH_CONTROL_NAMES

# Sub-agent may use a safe subset (no spawn/cron/teammate recursion).
SUBAGENT_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "glob", "bash", "get_skill",
    "todo_write", "create_task", "list_tasks", "get_task", "claim_task",
    "complete_task", "fail_task", "cancel_task", "release_task",
    "excel_read", "excel_write",
})

# Conservative read-only bash: single simple command, no shell chaining.
_BASH_QUERY_PREFIX = re.compile(
    r"^\s*(ls|pwd|test|head|tail|wc|file|stat|which|type|dirname|basename|echo)\b",
    re.IGNORECASE,
)
_BASH_QUERY_FORBIDDEN = re.compile(r"[|;&><`]|\$\(|&&|\|\|")


def bash_is_query(command: str) -> bool:
    """True when the command planner can prove every segment is read-only."""
    from server.runtime.command_plan import classify_bash_command

    return classify_bash_command(command).risk == "verified_read"


def resolve_tool_kind(name: str, args: dict[str, Any] | None = None) -> str:
    """Return query | mutation | control. Unknown names → mutation (fail-safe)."""
    try:
        from server.runtime.feature_flags import flag_enabled
        from server.runtime.tool_semantics import legacy_tool_kind, resolve_tool_semantics

        if flag_enabled("tool_semantics_v2"):
            return legacy_tool_kind(resolve_tool_semantics(name, args))
    except Exception:
        pass
    if name in CONTROL_TOOLS:
        return "control"
    if name in QUERY_TOOLS:
        return "query"
    if name == "bash":
        command = str((args or {}).get("command") or "")
        if bash_is_query(command):
            return "query"
        return "mutation"
    if name in TOOL_SPECS:
        if name in QUERY_TOOLS:
            return "query"
        return "mutation"
    if name.startswith("mcp__"):
        # Without semantics flag: verb heuristic so search/list are query.
        tool_tail = name.rsplit("__", 1)[-1].lower()
        if any(
            tool_tail.startswith(v) or f"_{v}_" in f"_{tool_tail}_"
            for v in ("get", "list", "search", "find", "read", "query", "check", "describe")
        ):
            return "query"
        return "mutation"
    return "mutation"


def tool_kind_for_spec(name: str) -> str:
    """Static kind without args (bash defaults to mutation)."""
    return resolve_tool_kind(name, None)


def all_tool_names() -> list[str]:
    return list(TOOL_SPECS.keys())


def catalog_entries() -> list[dict[str, str]]:
    return [
        {"name": name, "description": str(spec["description"])}
        for name, spec in TOOL_SPECS.items()
    ]


def openai_schema(name: str) -> dict[str, Any] | None:
    spec = TOOL_SPECS.get(name)
    if not spec:
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }


def schemas_for_names(names: list[str]) -> list[dict[str, Any]]:
    out = []
    for name in names:
        schema = openai_schema(name)
        if schema:
            out.append(schema)
    return out
