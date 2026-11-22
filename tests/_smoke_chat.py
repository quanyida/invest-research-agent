import json
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

with TestClient(app) as client:
    email = f"chat-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "chat smoke"})
    assert response.status_code == 200, response.text
    with client.stream(
        "POST",
        "/api/v1/chat",
        json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": "帮我看看这份 PDF 的公告原文"},
    ) as stream:
        body = "".join(stream.iter_text())
        print("chat_status", stream.status_code)
    events = parse_sse(body)
    names = [name for name, _ in events]
    print("events", names)
    assert "meta" in names and "done" in names, body[-2000:]
    meta = next(data for name, data in events if name == "meta")
    done = next(data for name, data in events if name == "done")
    print("meta", meta)
    print("done", done)
    messages = client.get(f"/api/v1/conversations/{meta['conversation_id']}/messages")
    print("messages", messages.status_code, len(messages.json().get("items", [])))
    items = messages.json()["items"]
    assert len(items) == 2
    assert items[1]["status"] == "done"
    assert "能力边界" in items[1]["content"]
    usage = client.get("/api/v1/usage")
    print("usage", usage.status_code, usage.json()["today"])
    assert usage.json()["today"]["questions"] >= 1