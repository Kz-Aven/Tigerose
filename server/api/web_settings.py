"""Web / Tavily settings API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.runtime.web import credentials as web_creds

router = APIRouter(prefix="/api/web", tags=["web"])


class TavilyKeyUpdate(BaseModel):
    api_key: str


@router.get("/tavily")
def get_tavily():
    return web_creds.tavily_status()


@router.put("/tavily")
def put_tavily(body: TavilyKeyUpdate):
    try:
        return web_creds.save_tavily_key(body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
