from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import require_principal
from app.tools.registry import public_tools

router = APIRouter(prefix="/api/v1", tags=["catalog"])


@router.get("/tools")
def list_tools(request: Request) -> dict:
    require_principal(request)
    return {"items": public_tools()}