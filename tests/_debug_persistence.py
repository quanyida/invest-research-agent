import json
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.db import tenant_scope
from app.models import Evidence, LlmCall, Message, ToolCall, UsageDaily

def parse_sse(text: str):
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is not None:
            events.append((event, data))
    return events

with TestClient(app) as client:
    email = f"persist-{uuid.uuid4().hex[:8]}@example.com"
    registered = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "persist smoke"})
    assert registered.status_code == 200, registered.text
    principal = registered.json()
    tenant_id = uuid.UUID(principal["tenant"]["id"])
    user_id = uuid.UUID(principal["user"]["id"])
    question = "宁德时代 2025Q3 毛利率多少，同比变化？"
    with client.stream("POST", "/api/v1/chat", json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": question}) as stream:
        body = "".join(stream.iter_text())
    events = parse_sse(body)
    done = next(data for name, data in events if name == "done")
    verify = next(data for name, data in events if name == "verify")
    evidence = next(data for name, data in events if name == "evidence")["evidence"]
    message_id = uuid.UUID(done["message_id"])
    print("DONE", done["status"], "VERIFY", verify["passed"], "FACTS", evidence["facts_total"])
    print("DERIVED", [fact for fact in evidence["facts"] if fact.get("metric") == "销售毛利率同比变化"])
    with tenant_scope(tenant_id, user_id) as db:
        counts = {
            "messages": db.scalar(select(func.count()).select_from(Message).where(Message.message_id if False else Message.id.is_not(None))),
            "evidence": db.scalar(select(func.count()).select_from(Evidence).where(Evidence.id.is_not(None))),
            "tool_calls": db.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.id.is_not(None))),
            "llm_calls": db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.id.is_not(None))),
            "usage_daily": db.scalar(select(func.count()).select_from(UsageDaily).where(UsageDaily.user_id == user_id)),
        }
        message = db.scalar(select(Message).where(Message.id == message_id))
        stored_evidence = db.scalar(select(Evidence).where(Evidence.message_id == message_id))
        usage = db.scalar(select(UsageDaily).where(UsageDaily.user_id == user_id))
        print("COUNTS", counts)
        print("MESSAGE", message.role if message else None, message.status if message else None, len(message.content) if message else 0)
        print("EVIDENCE", stored_evidence.tool if stored_evidence else None, len(stored_evidence.facts) if stored_evidence else 0)
        print("USAGE", usage.questions if usage else None, usage.tool_calls if usage else None, usage.llm_calls if usage else None)
