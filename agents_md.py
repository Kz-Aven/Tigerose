"""Load project AGENTS.md for system-prompt injection (CLI + App).

Resolution order:
1. ``TIGEROSE_HOME/AGENTS.md`` (optional user override after install; ``AVENT_HOME`` compat)
2. ``code_root()/AGENTS.md`` (repo in dev, bundled Resources/runtime in packaged app)
"""

from __future__ import annotations

from pathlib import Path

from avent_paths import code_root, data_root

_SUMMARY_HEADERS = frozenset(
    {
        "conventions",
        "task board",
        "do / don't",
        "verification",
        "security",
        "delegation",
    }
)
_cache: dict = {"path": None, "mtime": None, "content": "", "summary": ""}


def resolve_agents_md_path() -> Path | None:
    for p in (data_root() / "AGENTS.md", code_root() / "AGENTS.md"):
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def _parse_sections(content: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()
    return sections


def _build_summary(content: str) -> str:
    if not content:
        return ""
    parts = []
    for name, body in _parse_sections(content).items():
        if name.lower() in _SUMMARY_HEADERS and body:
            parts.append(f"## {name}\n{body}")
    if parts:
        return "\n\n".join(parts)
    return content[:2000]


def load_agents_md() -> str:
    path = resolve_agents_md_path()
    if path is None:
        _cache.update({"path": None, "mtime": None, "content": "", "summary": ""})
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.update({"path": None, "mtime": None, "content": "", "summary": ""})
        return ""
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["content"]
    content = path.read_text(encoding="utf-8").strip()
    _cache["path"] = str(path)
    _cache["mtime"] = mtime
    _cache["content"] = content
    _cache["summary"] = _build_summary(content)
    return content


def agents_md_summary() -> str:
    load_agents_md()
    return str(_cache.get("summary") or "")


def agents_md_for_prompt(*, full: bool = True) -> str:
    """Return a system-prompt fragment, or empty string if missing."""
    content = load_agents_md()
    if not content:
        return ""
    body = content if full else (agents_md_summary() or content[:2000])
    if not body:
        return ""
    return f"## Project instructions (AGENTS.md)\n\n{body}"
