"""Avent Project Group API."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

from avent_paths import code_root, data_root, ensure_data_dirs

# Load repo/code .env first, then TIGEROSE_HOME/.env (AVENT_HOME compat).
# Home wins for app-specific keys (e.g. FEISHU_*), but a placeholder
# A local placeholder must not clobber a real sk- key from the repo .env.
ensure_data_dirs()
_env_home = data_root() / ".env"
_env_code = code_root() / ".env"
if _env_code.exists():
    load_dotenv(_env_code, override=False)
_code_openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
if _env_home.exists():
    load_dotenv(_env_home, override=True)
_home_openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
_local_key_placeholders = {"ollama", "lm-studio"}
if (
    _code_openai_key
    and _code_openai_key.lower() not in _local_key_placeholders
    and (
        not _home_openai_key
        or _home_openai_key.lower() in _local_key_placeholders
    )
):
    os.environ["OPENAI_API_KEY"] = _code_openai_key

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.api import (
    agent_mesh,
    agent_assets,
    assistants,
    capabilities,
    connectors,
    groups,
    hooks,
    im_channels,
    memory_jobs,
    models,
    permissions,
    questions,
    sse,
    uploads,
    usage,
    web_settings,
    workflows,
)
from server.db.schema import init_db
from server.scheduler import group_scheduler as scheduler
from server.seed import seed_presets_if_empty
from avent_config import init_active_model_profile, init_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    import secrets

    # Remove the management capability before any agent subprocess is started.
    app.state.mesh_admin_token = os.environ.pop("TIGEROSE_MESH_ADMIN_TOKEN", "") or secrets.token_urlsafe(32)
    app.state.hooks_admin_token = app.state.mesh_admin_token
    init_config(workdir=data_root())
    init_db()
    from server.db import repos

    repos.interrupt_stale_runtime_runs()
    repos.delete_setting("feishu_bound_template_id")
    from server.runtime.web.credentials import remove_legacy_feishu_credentials

    remove_legacy_feishu_credentials()
    from server.runtime.run_coordinator import suspend_interrupted_goals
    from server.runtime.turn import _get_sessions

    suspend_interrupted_goals(_get_sessions())
    seed_presets_if_empty()
    from server.capabilities.mcp_config import ensure_user_mcp_seeded
    ensure_user_mcp_seeded()
    from server.capabilities.hooks_config import ensure_user_hooks_seeded
    ensure_user_hooks_seeded()
    init_active_model_profile()
    from server.runtime.memory_jobs import MemoryWorker, recover_outboxes

    recover_outboxes(_get_sessions())
    memory_worker = MemoryWorker()
    from server.runtime.workflow_runtime import scheduler as workflow_scheduler
    from server.runtime.workflow_adapters import code_worktree, dingtalk_message

    workflow_scheduler.register_executor(
        "dingtalk_group_message", dingtalk_message.execute, reconciler=dingtalk_message.reconcile
    )
    workflow_scheduler.register_executor(
        "local_git_worktree", code_worktree.execute, reconciler=code_worktree.reconcile
    )
    workflow_scheduler.start()
    scheduler.set_event_loop(asyncio.get_running_loop())
    from server.runtime.mesh_runtime import scheduler as mesh_scheduler

    from server.db.mesh_repos import recover_stopped_after_restart
    recover_stopped_after_restart()
    mesh_scheduler.start()
    from server.runtime import cron_scheduler

    cron_scheduler.start()
    # IM bots start independently so a failed provider cannot affect local chat.
    try:
        from server.im_channels import manager as im_manager

        im_manager.start_all()
    except Exception:
        pass
    memory_worker.start()
    try:
        yield
    finally:
        mesh_scheduler.stop()
        await asyncio.to_thread(memory_worker.stop)
        workflow_scheduler.stop()
        cron_scheduler.stop()


app = FastAPI(title="Tigerose", version="0.1.7", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(assistants.router)
app.include_router(memory_jobs.router)
app.include_router(capabilities.router)
app.include_router(connectors.router)
app.include_router(groups.router)
app.include_router(im_channels.router)
app.include_router(sse.router)
app.include_router(models.router)
app.include_router(uploads.router)
app.include_router(usage.router)
app.include_router(web_settings.router)
app.include_router(permissions.router)
app.include_router(questions.router)
app.include_router(workflows.router)
app.include_router(agent_mesh.router)
app.include_router(agent_assets.router)
app.include_router(hooks.router)


@app.get("/api/health")
def health():
    return {"ok": True}
