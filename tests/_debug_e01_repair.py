import json
import uuid

from fastapi.testclient import TestClient

import app.agent.loop as loop_module
from app.main import app

original_repair = loop_module.repair_citations
original_verify = loop_module.verify_answer

def traced_repair(answer, evidences):
    fixed, count = original_repair(answer, evidences)
    print("---- BEFORE REPAIR ----")
    print(answer)
    print("---- AFTER REPAIR ----")
    print(fixed)
    print("REPAIRS", count)
    return fixed, count

def traced_verify(answer, evidences):
    result = original_verify(answer, evidences)
    print("---- VERIFY ----")
    print(json.dumps({"passed": result.passed, "issues": result.issues}, ensure_ascii=False))
    return result

loop_module.repair_citations = traced_repair
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
    email = f"debug-e01-{uuid.uuid4().hex[:8]}@example.com"
    registered = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "debug e01"})
    assert registered.status_code == 200, registered.text
    with client.stream("POST", "/api/v1/chat", json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": "宁德时代今年分红方案"}) as stream:
        body = "".join(stream.iter_text())
    events = parse_sse(body)
    for name, data in events:
        if name in {"verify", "done", "error"}:
            print("EVENT", name, json.dumps(data, ensure_ascii=False))
