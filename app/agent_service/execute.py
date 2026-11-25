"""Agent Service 的 ``POST /execute`` 端点。

第一批只固定契约与监听形态，执行逻辑在第二批接入 ``AgentScopeRunner``。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["agent-service"])

class ExecuteBody(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    question: str = Field(min_length=1, max_length=2000)
    turn: int = Field(ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0)

@router.post("/execute")
def execute(_: ExecuteBody) -> None:
    raise HTTPException(
        status_code=501,
        detail={"code": "NOT_IMPLEMENTED", "message": "同步执行尚未接入"},
    )