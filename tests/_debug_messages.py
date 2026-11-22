import json, uuid
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    e = "chat-" + uuid.uuid4().hex[:8] + "@example.com"
    c.post("/api/v1/auth/register", json={"email": e, "password": "test-pass-123", "tenant_name": "x"})
    with c.stream("POST", "/api/v1/chat", json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": "帮我看看这份 PDF 的公告原文"}) as s:
        body = "".join(s.iter_text())
    meta = None
    for block in body.replace("\r\n", "\n").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:") and "conversation_id" in line:
                meta = json.loads(line[5:].strip())
                break
        if meta:
            break
    msgs = c.get("/api/v1/conversations/" + meta["conversation_id"] + "/messages").json()
    for i, item in enumerate(msgs["items"]):
        print("ITEM", i, item.get("role"), item.get("status"), repr(item.get("content"))[:500])
