import json
import uuid
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    email = f"debug-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "debug"})
    with client.stream("POST", "/api/v1/chat", json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": "帮我看看这份 PDF 的公告原文"}) as s:
        body = "".join(s.iter_text())
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is not None:
            events.append((name, data))
    meta = next(data for name, data in events if name == "meta")
    items = client.get(f"/api/v1/conversations/{meta['conversation_id']}/messages").json()["items"]
    print("COUNT", len(items))
    print("CONTENT_REPR", repr(items[1]["content"]))
    print("CONTENT", items[1]["content"])
