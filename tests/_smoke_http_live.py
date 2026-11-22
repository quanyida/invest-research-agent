import json
import uuid

import httpx


def main() -> None:
    base = "http://127.0.0.1:8000"
    with httpx.Client(base_url=base, timeout=120) as client:
        email = "http-" + uuid.uuid4().hex[:8] + "@example.com"
        reg = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "http smoke"})
        print("REGISTER", reg.status_code)
        events = []
        answer = []
        with client.stream("POST", "/api/v1/chat", json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": "宁德时代 2025Q3 毛利率多少，同比变化？"}) as stream:
            event = None
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    events.append(event)
                    if event in {"plan", "tool_end", "verify", "done"}:
                        print(event.upper(), json.dumps(data, ensure_ascii=False)[:800])
                    if event == "delta":
                        answer.append(data.get("text", ""))
            print("STATUS", stream.status_code)
        print("EVENTS", events)
        print("ANSWER_START")
        print("".join(answer))
        print("ANSWER_END")
        assert "done" in events
        assert "verify" in events


if __name__ == "__main__":
    main()
