"""Builtin tools facade — schemas/handlers live in server.runtime.tools."""

from __future__ import annotations

from server.runtime.tools.registry import (
    DANGEROUS_TOOLS,
    TOOL_SPECS,
    openai_schema,
    schemas_for_names,
)

# Back-compat aliases
EXECUTABLE_BUILTINS = TOOL_SPECS


def openai_tool_schema(name: str):
    return openai_schema(name)


def schemas_for_allowlist(tool_names: list[str]):
    return schemas_for_names(tool_names)


def is_dangerous(name: str) -> bool:
    if name.startswith("mcp__"):
        return True
    return name in DANGEROUS_TOOLS
