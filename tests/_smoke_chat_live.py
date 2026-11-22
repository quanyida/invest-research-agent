import json
import sys
import uuid

from fastapi.testclient import TestClient

from app.main import app

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

question = sys.argv[1] if len(sys.argv) > 1 else "宁德时代 2025Q3 毛利率多少，同比变化？"
with TestClient(app) as client:
    email = f"live-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "live smoke"})
    assert response.status_code == 200, response.text
    print("QUESTION", question)
    with client.stream(
        "POST",
        "/api/v1/chat",
        json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": question},
    ) as stream:
        body = "".join(stream.iter_text())
        print("STATUS", stream.status_code)
    events = parse_sse(body)
    for name, data in events:
        if name in {"meta", "plan", "tool_start", "tool_end", "verify", "usage", "done", "error"}:
            print("EVENT", name, json.dumps(data, ensure_ascii=False)[:1200])
        elif name == "evidence":
            evidence = data.get("evidence") or {}
            print("EVIDENCE", evidence.get("id"), evidence.get("tool"), "facts", evidence.get("facts_total"), "quality", evidence.get("parse_quality"))
    answer = "".join(data.get("text", "") for name, data in events if name == "delta")
    print("ANSWER_START")
    print(answer)
    print("ANSWER_END")
    print("EVENT_NAMES", [name for name, _ in events])
    print("BODY_TAIL", body[-1000:])