"""Minimal plugin tools module for smoke tests / example bindable plugins.

Plugins that declare `tools: { module: tools_example.py }` can ship TOOL_DEFS + HANDLERS.
"""

TOOL_DEFS = [
    {
        "name": "ping",
        "description": "Health ping from plugin tools example.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }
]

HANDLERS = {
    "ping": lambda: "pong from plugin tools",
}
