"""Env helpers + Tavily credential status for TIGEROSE_HOME/.env."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from avent_paths import data_root

_LEGACY_FEISHU_ENV_KEYS = frozenset({
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DOMAIN",
    "LARK_APP_ID",
    "LARK_APP_SECRET",
})


def env_path() -> Path:
    return data_root() / ".env"


def upsert_env_file(updates: dict[str, str]) -> None:
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    keys = set(updates.keys())
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in keys:
            k = m.group(1)
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    for k, v in updates.items():
        os.environ[k] = v


def remove_legacy_feishu_credentials() -> None:
    """Drop credentials used only by the retired global Feishu channel."""
    path = env_path()
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        retained = [
            line for line in lines
            if (match := re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)) is None
            or match.group(1) not in _LEGACY_FEISHU_ENV_KEYS
        ]
        if retained != lines:
            path.write_text("\n".join(retained).rstrip() + "\n", encoding="utf-8")
    for key in _LEGACY_FEISHU_ENV_KEYS:
        os.environ.pop(key, None)


def tavily_status() -> dict[str, Any]:
    key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    masked = ""
    if key:
        masked = ("*" * max(0, len(key) - 4)) + key[-4:]
    return {
        "configured": bool(key),
        "api_key_masked": masked,
        "has_api_key": bool(key),
        "search_backend": "ddgs",
        "extract_backend": "tavily",
    }


def save_tavily_key(api_key: str) -> dict[str, Any]:
    key = api_key.strip()
    if not key:
        raise ValueError("Tavily API Key 不能为空")
    upsert_env_file({"TAVILY_API_KEY": key})
    return tavily_status()
