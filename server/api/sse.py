from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from server.api.sse_bus import bus, format_sse
from server.db import repos

router = APIRouter(prefix="/api", tags=["sse"])


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    from server.runtime.run_coordinator import coordinator

    row = coordinator.status(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    return row


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str):
    from server.runtime.run_coordinator import coordinator

    if not coordinator.cancel(run_id):
        raise HTTPException(404, "run not found or already terminal")
    return {"accepted": True, "run_id": run_id}


@router.get("/sse")
async def sse_stream(request: Request, channel: str = Query(...)):
    if not (channel.startswith("group:") or channel.startswith("assistant:")):
        raise HTTPException(400, "channel must be group:{id} or assistant:{id}")
    q = await bus.subscribe(channel)

    async def gen():
        try:
            yield format_sse({"type": "connected", "data": {"channel": channel}})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield format_sse(payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await bus.unsubscribe(channel, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/summaries")
def multi_summaries(group_ids: str = Query(..., description="comma-separated group ids")):
    ids = [g.strip() for g in group_ids.split(",") if g.strip()]
    out = []
    for gid in ids:
        g = repos.get_group(gid)
        if not g:
            continue
        out.append(
            {
                "group_id": gid,
                "name": g["name"],
                "l1": repos.list_l1_feed(gid),
                "task_stats": repos.task_stats(gid),
            }
        )
    return out
