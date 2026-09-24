"""Model catalog: live LM Studio models + configured profiles."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from avent_config import (
    find_model_profile,
    goal_evaluator_model_status,
    list_model_profiles,
    resolve_base_url,
    set_goal_evaluator_model,
)
from server.db import repos
from server.runtime import turn as turn_runtime

router = APIRouter(prefix="/api/models", tags=["models"])

_EMBED_HINTS = ("embed", "embedding", "bge-", "nomic-embed", "mxbai-embed")


def _is_embedding(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _EMBED_HINTS)


def _local_openai_base() -> str:
    """Prefer a configured localhost OpenAI endpoint, else LM Studio."""
    for p in list_model_profiles():
        base = str(p.get("base_url") or "")
        if "localhost" in base.lower() or "127.0.0.1" in base:
            return base.rstrip("/")
    return "http://localhost:1234/v1"


def fetch_local_models() -> list[dict[str, str]]:
    base = _local_openai_base()
    url = f"{base}/models"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    out: list[dict[str, str]] = []
    for item in data.get("data") or []:
        name = str(item.get("id") or "").strip()
        if not name or _is_embedding(name):
            continue
        out.append(
            {
                "id": name,
                "label": name,
                "provider": "custom",
                "base_url": base,
            }
        )
    return out


def build_model_catalog() -> list[dict[str, str]]:
    """LM Studio live list (filtered) + profiles from config.yaml."""
    by_id: dict[str, dict[str, str]] = {}
    live_ids: set[str] = set()
    for p in fetch_local_models():
        by_id[p["id"]] = p
        live_ids.add(p["id"])
    for p in list_model_profiles():
        pid = str(p.get("id") or "").strip()
        if not pid or _is_embedding(pid):
            continue
        base = str(p.get("base_url") or "")
        if pid not in by_id:
            by_id[pid] = {
                "id": pid,
                "label": pid,  # real model id as display name
                "provider": str(p.get("provider") or ""),
                "base_url": base,
            }
    local = sorted(
        [v for key, v in by_id.items() if key in live_ids],
        key=lambda x: x["id"],
    )
    configured = sorted(
        [v for key, v in by_id.items() if key not in live_ids],
        key=lambda x: x["id"],
    )
    return local + configured


class SwitchBody(BaseModel):
    profile_id: str
    template_id: str


class ProfileBody(BaseModel):
    model: str  # profile id / model name
    base_url: str
    api_key: str = ""
    label: str = ""
    provider: str = ""
    reasoning_effort: str = "none"


class ProfileUpdateBody(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    label: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None


class GoalEvaluatorBody(BaseModel):
    profile_id: str = ""


@router.get("")
def get_models():
    profiles = build_model_catalog()
    return {
        "default_id": turn_runtime.default_model_profile_id(),
        "profiles": profiles,
    }


@router.get("/profiles")
def list_managed_profiles():
    from avent_config import list_model_profiles, public_model_profile

    return {
        "profiles": [public_model_profile(p) for p in list_model_profiles()],
        "default_id": turn_runtime.default_model_profile_id(),
    }


@router.get("/goal-evaluator")
def get_goal_evaluator_model():
    return goal_evaluator_model_status()


@router.put("/goal-evaluator")
def update_goal_evaluator_model(body: GoalEvaluatorBody):
    try:
        return set_goal_evaluator_model(body.profile_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/profiles")
def create_managed_profile(body: ProfileBody):
    from avent_config import upsert_model_profile

    try:
        return upsert_model_profile(
            model_id=body.model,
            base_url=body.base_url,
            api_key=body.api_key,
            label=body.label or body.model,
            provider=body.provider,
            reasoning_effort=body.reasoning_effort,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.put("/profiles/{profile_id}")
def update_managed_profile(profile_id: str, body: ProfileUpdateBody):
    from avent_config import find_model_profile, upsert_model_profile

    existing = find_model_profile(profile_id)
    if not existing:
        raise HTTPException(404, f"model not found: {profile_id}")
    try:
        updated = upsert_model_profile(
            model_id=(body.model if body.model is not None else existing["id"]),
            base_url=(body.base_url if body.base_url is not None else existing["base_url"]),
            api_key=("" if body.api_key is None else body.api_key),
            label=(body.label if body.label is not None else existing.get("label") or ""),
            provider=(body.provider if body.provider is not None else existing.get("provider") or ""),
            reasoning_effort=(
                body.reasoning_effort
                if body.reasoning_effort is not None
                else existing.get("reasoning_effort") or "none"
            ),
            replace_id=profile_id,
        )
        new_id = str(updated.get("id") or profile_id)
        if new_id != profile_id:
            for template in repos.list_templates():
                if str(template.get("model_profile_id") or "") == profile_id:
                    repos.update_template(
                        template["template_id"],
                        model_profile_id=new_id,
                    )
        return updated
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/profiles/{profile_id}")
def delete_managed_profile(profile_id: str):
    from avent_config import delete_model_profile

    try:
        delete_model_profile(profile_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True, "deleted": profile_id}


@router.post("/switch")
def switch_model(body: SwitchBody):
    """Bind a model profile to an assistant template. Affects subsequent turns only."""
    catalog = {p["id"] for p in build_model_catalog()}
    profile = find_model_profile(body.profile_id)
    if body.profile_id not in catalog and not profile:
        available = ", ".join(sorted(catalog)) or "(none)"
        raise HTTPException(400, f"Unknown model '{body.profile_id}'. Available: {available}")

    tpl = repos.get_template(body.template_id)
    if not tpl:
        raise HTTPException(404, "assistant not found")
    updated = repos.update_template(body.template_id, model_profile_id=body.profile_id)
    return {
        "ok": True,
        "message": f"{tpl['name']} → {body.profile_id}",
        "template": updated,
        "active_id": body.profile_id,
        "active_model": body.profile_id,
        "model_label": body.profile_id,
    }
