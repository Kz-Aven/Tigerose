"""Configuration loader for Tigerose / Avent Agent (Hermes-aligned section naming).

Module filename ``avent_config`` kept for import compatibility (tigerose_config alias in docs).
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

CONFIG_VERSION = 1

DEFAULT_CONFIG: dict[str, Any] = {
    "_config_version": CONFIG_VERSION,
    "model": {
        "default": "qwen/qwen3.6-27b",
        "provider": "custom",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "context_length": 65536,
        "max_tokens": 8000,
        "fallback_model": None,
        "temperature": 0.7,
        "profiles": [
            {
                "id": "qwen/qwen3.6-27b",
                "label": "Qwen 3.6 27B (LM Studio)",
                "provider": "custom",
                "base_url": "http://localhost:1234/v1",
                "api_key": "lm-studio",
                "reasoning_effort": "none",
            },
            {
                "id": "deepseek-v4-flash",
                "label": "DeepSeek V4 Flash",
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": "",
                "reasoning_effort": "none",
            },
        ],
    },
    "agent": {
        "max_turns": 150,
        "goal_evaluator_model": "",
        "reasoning_effort": "none",
        "api_max_retries": 3,
        "verbose": False,
        "trusted_data_roots": [],
        "feature_flags": {
            "typesafe_judgments_v1": True,
        },
        "typesafe": {
            "intent_timeout_s": 5.0,
            "goal_timeout_s": 20.0,
            "intent_goal_activation_min_confidence": 0.70,
            "intent_durable_signal_min_probability": 0.70,
            "goal_completion_min_confidence": 0.70,
        },
    },
    "terminal": {
        "backend": "local",
        "cwd": ".",
        "timeout": 180,
    },
    "delegation": {
        "model": "",
        "provider": "",
        "base_url": "",
        "api_key": "",
        "max_iterations": 50,
        "reasoning_effort": "",
        "tools": ["bash", "read_file", "write_file", "edit_file", "glob"],
    },
    "session": {
        "persist_dir": ".sessions",
        "cli_default_scope": "cli:default",
        "feishu_one_session_per_chat": True,
        "save_every_turn": True,
        "reset": {
            "mode": "idle",
            "idle_minutes": 1440,
            "at_hour": 4,
        },
        "memory": {
            "auto_extract_scope": "session",
            "promote_on_session_end": False,
            "max_session_items": 10,
        },
    },
    "avent": {
        "llm": {
            "max_tokens_escalated": 16000,
            "max_consecutive_529": 2,
            "max_recovery_retries": 2,
            "base_delay_ms": 500,
            "continuation_prompt": (
                "Continue from the previous response. Do not repeat completed work."
            ),
        },
        "bash": {
            "max_output_chars": 50000,
            "background_keywords": [
                "install",
                "build",
                "test",
                "deploy",
                "compile",
                "docker build",
                "pip install",
                "npm install",
                "cargo build",
                "pytest",
                "make",
            ],
        },
    },
}

_CFG: dict[str, Any] | None = None
_CONFIG_PATH: Path | None = None
_ACTIVE_PROFILE_ID: str | None = None

_ENV_OVERRIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MODEL_ID", ("model", "default")),
    ("OLLAMA_BASE_URL", ("model", "base_url")),
    ("FALLBACK_MODEL_ID", ("model", "fallback_model")),
    ("OLLAMA_REASONING_EFFORT", ("agent", "reasoning_effort")),
    # Legacy AVENT_* then TIGEROSE_* (later wins when both set).
    ("AVENT_MODEL", ("model", "default")),
    ("AVENT_BASE_URL", ("model", "base_url")),
    ("AVENT_REASONING_EFFORT", ("agent", "reasoning_effort")),
    ("TIGEROSE_MODEL", ("model", "default")),
    ("TIGEROSE_BASE_URL", ("model", "base_url")),
    ("TIGEROSE_REASONING_EFFORT", ("agent", "reasoning_effort")),
)


def cfg_get(cfg: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _set_nested(cfg: dict[str, Any], keys: tuple[str, ...], value: str) -> None:
    node = cfg
    for key in keys[:-1]:
        child = node.setdefault(key, {})
        if not isinstance(child, dict):
            return
        node = child
    node[keys[-1]] = value


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    for env_name, path in _ENV_OVERRIDES:
        raw = os.getenv(env_name)
        if raw is not None and str(raw).strip() != "":
            _set_nested(out, path, str(raw).strip())
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        _set_nested(out, ("model", "api_key"), api_key.strip())
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def get_config_path(workdir: Path | None = None) -> Path:
    root = workdir or Path.cwd()
    return root / "config.yaml"


def load_config(config_path: Path | None = None, *, workdir: Path | None = None) -> dict[str, Any]:
    global _CONFIG_PATH
    root = workdir or Path.cwd()
    path = config_path or get_config_path(root)
    _CONFIG_PATH = path

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    local_path = root / "config.local.yaml"

    for candidate in (path, local_path):
        if not candidate.exists():
            continue
        try:
            cfg = _deep_merge(cfg, _load_yaml(candidate))
        except Exception as exc:
            _warn_config_parse_failure(candidate, exc)

    cfg = _apply_env_overrides(cfg)
    cfg["_config_version"] = int(cfg.get("_config_version") or CONFIG_VERSION)
    return cfg


def init_config(config_path: Path | None = None, *, workdir: Path | None = None) -> dict[str, Any]:
    global _CFG
    _CFG = load_config(config_path, workdir=workdir)
    return _CFG


def get_config() -> dict[str, Any]:
    if _CFG is None:
        return init_config()
    return _CFG


def _warn_config_parse_failure(path: Path, exc: Exception) -> None:
    msg = (
        f"Failed to parse {path}: {exc}. "
        f"Using defaults for keys from that file — fix YAML and restart."
    )
    try:
        sys.stderr.write(f"⚠️  avent config: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _non_empty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def list_model_profiles(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg or get_config()
    raw = cfg_get(cfg, "model", "profiles", default=[])
    if not isinstance(raw, list):
        return []
    profiles: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        profiles.append({
            "id": model_id,
            "label": str(item.get("label") or model_id).strip(),
            "provider": str(item.get("provider") or "").strip(),
            "base_url": str(item.get("base_url") or "").strip(),
            "api_key": item.get("api_key"),
            "reasoning_effort": str(item.get("reasoning_effort") or "").strip(),
        })
    return profiles


def mask_api_key(key: Any) -> str:
    s = "" if key is None else str(key).strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}****{s[-4:]}"


def _is_masked_key(value: str, original: Any) -> bool:
    """True if client sent back a masked placeholder instead of a new secret."""
    v = (value or "").strip()
    if not v:
        return True
    if "****" in v:
        return True
    masked = mask_api_key(original)
    return bool(masked) and v == masked


def _load_disk_config(path: Path | None = None) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    path = path or get_config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    data = _load_yaml(path)
    return data if isinstance(data, dict) else copy.deepcopy(DEFAULT_CONFIG)


def _write_disk_config(data: dict[str, Any], path: Path | None = None) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    path = path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Drop runtime-only keys
    out = {k: v for k, v in data.items() if not str(k).startswith("_") or k == "_config_version"}
    text = yaml.safe_dump(
        out,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    path.write_text(text, encoding="utf-8")


def reload_config(*, workdir: Path | None = None) -> dict[str, Any]:
    """Reload merged config into memory after disk edits."""
    return init_config(workdir=workdir)


def public_model_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile["id"],
        "label": profile.get("label") or profile["id"],
        "provider": profile.get("provider") or "",
        "base_url": profile.get("base_url") or "",
        "api_key_masked": mask_api_key(profile.get("api_key")),
        "has_api_key": bool(str(profile.get("api_key") or "").strip()),
        "reasoning_effort": profile.get("reasoning_effort") or "",
    }


def goal_evaluator_model_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the persisted evaluator selection and its safe global fallback."""
    cfg = cfg or get_config()
    configured = str(
        cfg_get(cfg, "agent", "goal_evaluator_model", default="") or ""
    ).strip()
    profile_ids = {profile["id"] for profile in list_model_profiles(cfg)}
    default_id = str(cfg_get(cfg, "model", "default", default="") or "").strip()
    valid = bool(configured) and configured in profile_ids
    return {
        "profile_id": configured if valid else "",
        "configured_profile_id": configured,
        "effective_profile_id": configured if valid else default_id,
        "fallback": bool(configured) and not valid,
    }


def set_goal_evaluator_model(profile_id: str) -> dict[str, Any]:
    """Persist a global profile ID; empty means follow the current run profile."""
    profile_id = str(profile_id or "").strip()
    current = get_config()
    profile_ids = {profile["id"] for profile in list_model_profiles(current)}
    if profile_id and profile_id not in profile_ids:
        raise ValueError(f"model profile not found: {profile_id}")

    path = _CONFIG_PATH or get_config_path()
    disk = _load_disk_config(path)
    agent_node = disk.setdefault("agent", {})
    if not isinstance(agent_node, dict):
        agent_node = {}
        disk["agent"] = agent_node
    agent_node["goal_evaluator_model"] = profile_id
    _write_disk_config(disk, path)
    refreshed = init_config(config_path=path, workdir=path.parent)
    return goal_evaluator_model_status(refreshed)


def upsert_model_profile(
    *,
    model_id: str,
    base_url: str,
    api_key: str | None = None,
    label: str = "",
    provider: str = "",
    reasoning_effort: str = "none",
    replace_id: str | None = None,
) -> dict[str, Any]:
    """Create or update a profile in config.yaml and reload runtime config."""
    model_id = (model_id or "").strip()
    base_url = (base_url or "").strip()
    if not model_id:
        raise ValueError("model id is required")
    if not base_url:
        raise ValueError("base_url is required")

    path = get_config_path()
    disk = _load_disk_config(path)
    model_node = disk.setdefault("model", {})
    if not isinstance(model_node, dict):
        model_node = {}
        disk["model"] = model_node
    profiles = model_node.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
        model_node["profiles"] = profiles

    target_key = (replace_id or model_id).strip()
    existing_idx = None
    existing_item: dict[str, Any] | None = None
    for i, item in enumerate(profiles):
        if isinstance(item, dict) and str(item.get("id") or "").strip() == target_key:
            existing_idx = i
            existing_item = item
            break

    # Rename collision
    if replace_id and replace_id != model_id:
        for item in profiles:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == model_id:
                raise ValueError(f"model id already exists: {model_id}")

    if existing_idx is None and any(
        isinstance(item, dict) and str(item.get("id") or "").strip() == model_id for item in profiles
    ):
        raise ValueError(f"model id already exists: {model_id}")

    prev_key = (existing_item or {}).get("api_key")
    key_in = "" if api_key is None else str(api_key)
    if existing_item is not None and _is_masked_key(key_in, prev_key):
        final_key = prev_key if prev_key is not None else ""
    else:
        final_key = key_in.strip()

    entry = {
        "id": model_id,
        "label": (label or model_id).strip() or model_id,
        "provider": (provider or "").strip() or "custom",
        "base_url": base_url,
        "api_key": final_key,
        "reasoning_effort": (reasoning_effort or "none").strip() or "none",
    }
    if existing_idx is None:
        profiles.append(entry)
    else:
        profiles[existing_idx] = entry

    if replace_id and replace_id != model_id:
        if str(model_node.get("default") or "").strip() == replace_id:
            model_node["default"] = model_id
        agent_node = disk.get("agent")
        if (
            isinstance(agent_node, dict)
            and str(agent_node.get("goal_evaluator_model") or "").strip() == replace_id
        ):
            agent_node["goal_evaluator_model"] = model_id

    _write_disk_config(disk, path)
    reload_config()
    return public_model_profile(entry)


def delete_model_profile(model_id: str) -> None:
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("model id is required")
    path = get_config_path()
    disk = _load_disk_config(path)
    model_node = disk.get("model")
    if not isinstance(model_node, dict):
        raise ValueError(f"model not found: {model_id}")
    profiles = model_node.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError(f"model not found: {model_id}")
    new_profiles = [
        item
        for item in profiles
        if not (isinstance(item, dict) and str(item.get("id") or "").strip() == model_id)
    ]
    if len(new_profiles) == len(profiles):
        raise ValueError(f"model not found: {model_id}")
    model_node["profiles"] = new_profiles
    if str(model_node.get("default") or "").strip() == model_id and new_profiles:
        first = new_profiles[0]
        if isinstance(first, dict) and first.get("id"):
            model_node["default"] = first["id"]
    _write_disk_config(disk, path)
    reload_config()


def active_model_profile_id(cfg: dict[str, Any] | None = None) -> str:
    global _ACTIVE_PROFILE_ID
    if _ACTIVE_PROFILE_ID:
        return _ACTIVE_PROFILE_ID
    cfg = cfg or get_config()
    return str(cfg_get(cfg, "model", "default", default="")).strip()


def find_model_profile(query: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    query = query.strip()
    if not query:
        return None
    profiles = list_model_profiles(cfg)
    exact = [p for p in profiles if p["id"] == query]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in profiles if query.lower() in p["id"].lower()]
    if len(partial) == 1:
        return partial[0]
    return None


def _apply_model_profile(profile: dict[str, Any], cfg: dict[str, Any]) -> None:
    model_node = cfg.setdefault("model", {})
    model_node["default"] = profile["id"]
    if profile.get("provider"):
        model_node["provider"] = profile["provider"]
    if profile.get("base_url"):
        model_node["base_url"] = profile["base_url"]
    if _non_empty(profile.get("api_key")):
        model_node["api_key"] = str(profile["api_key"]).strip()
    if profile.get("reasoning_effort"):
        cfg.setdefault("agent", {})["reasoning_effort"] = profile["reasoning_effort"]


def init_active_model_profile(cfg: dict[str, Any] | None = None) -> str:
    """Bind runtime active profile to model.default on startup."""
    global _ACTIVE_PROFILE_ID
    cfg = cfg or get_config()
    default = str(cfg_get(cfg, "model", "default", default="")).strip()
    for profile in list_model_profiles(cfg):
        if profile["id"] == default:
            _apply_model_profile(profile, cfg)
            _ACTIVE_PROFILE_ID = profile["id"]
            return profile["id"]
    _ACTIVE_PROFILE_ID = default
    return default


def switch_model_profile(profile_id: str, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Switch active model profile at runtime. Returns (ok, message)."""
    global _ACTIVE_PROFILE_ID
    cfg = cfg or get_config()
    profile = find_model_profile(profile_id, cfg)
    if not profile:
        available = ", ".join(p["id"] for p in list_model_profiles(cfg)) or "(none configured)"
        return False, f"Unknown model '{profile_id}'. Available: {available}"
    _apply_model_profile(profile, cfg)
    _ACTIVE_PROFILE_ID = profile["id"]
    label = profile.get("label") or profile["id"]
    return True, f"Switched to {profile['id']} ({label}) · {profile.get('base_url') or resolve_base_url()}"


def resolve_model_name(*, delegation: bool = False) -> str:
    cfg = get_config()
    if delegation:
        delegated = cfg_get(cfg, "delegation", "model", default="")
        if _non_empty(delegated):
            return str(delegated).strip()
    return str(cfg_get(cfg, "model", "default", default="qwen/qwen3.6-27b")).strip()


def resolve_api_key(*, delegation: bool = False) -> str:
    cfg = get_config()
    if delegation:
        delegated = cfg_get(cfg, "delegation", "api_key", default="")
        if _non_empty(delegated):
            return str(delegated).strip()
    return str(cfg_get(cfg, "model", "api_key", default="lm-studio")).strip()


def resolve_base_url(*, delegation: bool = False) -> str:
    cfg = get_config()
    if delegation:
        delegated = cfg_get(cfg, "delegation", "base_url", default="")
        if _non_empty(delegated):
            return str(delegated).strip()
    return str(cfg_get(cfg, "model", "base_url", default="http://localhost:1234/v1")).strip()


def resolve_reasoning_effort(*, delegation: bool = False) -> str | None:
    cfg = get_config()
    if delegation:
        delegated = cfg_get(cfg, "delegation", "reasoning_effort", default="")
        if _non_empty(delegated):
            effort = str(delegated).strip()
        else:
            effort = str(cfg_get(cfg, "agent", "reasoning_effort", default="none")).strip()
    else:
        effort = str(cfg_get(cfg, "agent", "reasoning_effort", default="none")).strip()

    if not effort or effort.lower() in ("off", "false", "0", "skip"):
        return None
    return effort


def reasoning_effort_kwargs(*, delegation: bool = False) -> dict[str, str]:
    effort = resolve_reasoning_effort(delegation=delegation)
    if effort is None:
        return {}
    if effort.lower() == "none":
        return {}
    return {"reasoning_effort": effort}


def delegation_tool_names() -> list[str]:
    cfg = get_config()
    tools = cfg_get(cfg, "delegation", "tools", default=[])
    if isinstance(tools, list) and tools:
        return [str(name) for name in tools]
    return list(DEFAULT_CONFIG["delegation"]["tools"])


def bash_background_keywords() -> list[str]:
    cfg = get_config()
    keywords = cfg_get(cfg, "avent", "bash", "background_keywords", default=[])
    if isinstance(keywords, list) and keywords:
        return [str(kw).lower() for kw in keywords]
    return list(DEFAULT_CONFIG["avent"]["bash"]["background_keywords"])


def config_summary_lines() -> list[str]:
    cfg = get_config()
    path = _CONFIG_PATH or get_config_path()
    lines = [
        f"[config] file={path.name}",
        (
            f"[config] model={resolve_model_name()} "
            f"provider={cfg_get(cfg, 'model', 'provider', default='custom')} "
            f"base_url={resolve_base_url()}"
        ),
        (
            f"[config] agent.reasoning_effort="
            f"{cfg_get(cfg, 'agent', 'reasoning_effort', default='none')} "
            f"api_max_retries={cfg_get(cfg, 'agent', 'api_max_retries', default=3)}"
        ),
        (
            f"[config] delegation.max_iterations="
            f"{cfg_get(cfg, 'delegation', 'max_iterations', default=50)} "
            f"model={resolve_model_name(delegation=True)}"
        ),
        f"[config] terminal.timeout={cfg_get(cfg, 'terminal', 'timeout', default=180)}",
    ]
    return lines
