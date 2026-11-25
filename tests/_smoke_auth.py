import uuid
from fastapi.testclient import TestClient
from app.api_main import app

email = f"test-{uuid.uuid4().hex[:8]}@example.com"
with TestClient(app) as c:
    print("health", c.get("/healthz").status_code, c.get("/healthz").json())
    print("ready", c.get("/readyz").status_code, c.get("/readyz").json())
    r = c.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "测试租户"})
    print("register", r.status_code, r.json())
    print("cookie", "ira_session" in r.cookies)
    me = c.get("/api/v1/auth/me")
    print("me", me.status_code, me.json())
    c.post("/api/v1/auth/logout")
    print("me_after_logout", c.get("/api/v1/auth/me").status_code)
    login = c.post("/api/v1/auth/login", json={"email": email, "password": "test-pass-123"})
    print("login", login.status_code, login.json())
    print("me2", c.get("/api/v1/auth/me").status_code)