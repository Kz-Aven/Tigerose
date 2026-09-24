"""Explicit human feedback ingress for Agent execution data assets."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/agent-assets", tags=["agent-assets"])

class FeedbackBody(BaseModel):
    assistant_id: str
    execution_id: str
    feedback_type: str = Field(pattern="^(copied|regenerated|edited|accepted|rejected)$")
    reason: str = ""
    content: str | None = None

@router.post("/feedback")
def record_feedback(body: FeedbackBody):
    from server.agent_assets import recorder
    try:
        recorder.feedback(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}
