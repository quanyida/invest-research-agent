from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    for path in ["/ui/login", "/ui/", "/ui/usage", "/assets/app.css", "/assets/app.js", "/"]:
        r = c.get(path, follow_redirects=False)
        print(path, r.status_code, r.headers.get("location", ""))
