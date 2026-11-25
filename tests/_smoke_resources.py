import uuid
from fastapi.testclient import TestClient

from app.api_main import app


def register(client: TestClient, label: str):
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": label})
    assert response.status_code == 200, response.text
    return email


with TestClient(app) as a, TestClient(app) as b:
    register(a, "alice")
    register(b, "bob")
    me = a.get("/api/v1/me")
    print("me", me.status_code, me.json())
    created = a.post("/api/v1/conversations", json={"title": "A 的会话"})
    print("create", created.status_code, created.json())
    conv_id = created.json()["id"]
    listed = a.get("/api/v1/conversations")
    print("list_a", listed.status_code, len(listed.json()["items"]))
    messages = a.get(f"/api/v1/conversations/{conv_id}/messages")
    print("messages_a", messages.status_code, messages.json())
    forbidden = b.get(f"/api/v1/conversations/{conv_id}/messages")
    print("b_reads_a", forbidden.status_code, forbidden.json())
    b_list = b.get("/api/v1/conversations")
    print("list_b", b_list.status_code, len(b_list.json()["items"]))
    deleted = a.delete(f"/api/v1/conversations/{conv_id}")
    print("delete", deleted.status_code, deleted.json())
    print("messages_after_delete", a.get(f"/api/v1/conversations/{conv_id}/messages").status_code)
    print("tools", a.get("/api/v1/tools").status_code, len(a.get("/api/v1/tools").json()["items"]))