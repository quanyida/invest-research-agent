import json
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.agent.loop as loop_module
from app.api_main import app
from app.db import tenant_scope
from app.models import Evidence, LlmCall, Message, ToolCall, UsageDaily

original_verify = loop_module.verify_answer


def traced_verify(answer, evidences):
    result = original_verify(answer, evidences)
    print("---- ANSWER UNDER VERIFY ----")
    print(answer)
    print("---- VERIFY ----")
    print(json.dumps({"passed": result.passed, "issues": result.issues, "unverified": result.unverified, "cited": result.cited_fact_ids, "numbers": result.number_count}, ensure_ascii=False, indent=2))
    return result


loop_module.verify_answer = traced_verify


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
    for name, data in events:
        if name in {"plan", "tool_start", "tool_end", "verify", "usage", "done", "error"}:
            print("EVENT", name, json.dumps(data, ensure_ascii=False)[:1600])
    done = next(data for name, data in events if name == "done")
    message_id = uuid.UUID(done["message_id"])
    print("FINAL_ANSWER:")
    print(next(data for name, data in events if False) if False else "")
    with tenant_scope(tenant_id, user_id) as db:
        message = db.scalar(select(Message).where(Message.id == message_id))
        counts = {
            "tool_calls_all": db.scalar(select(func.count()).select_from(ToolCall)),
            "tool_calls_main": db.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.parent_id.is_(None))),
            "tool_calls_child": db.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.parent_id.is_not(None))),
            "llm_calls": db.scalar(select(func.count()).select_from(LlmCall)),
        }
        print("COUNTS", counts)
        print("MESSAGE", message.role, message.status, len(message.content))
        print(message.content)
