import json, uuid
from app.tools.ifind_gateway import get_gateway

args = {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}
for i in range(3):
    out = get_gateway().call("ifind.stock_financials", args, uuid.uuid4())
    ev = out.evidence or {}
    print("RUN", i, "STATUS", out.status, "CACHED", out.cached, "FACTS", json.dumps(ev.get("facts", []), ensure_ascii=False)[:1000])
    print("SECTIONS", json.dumps(ev.get("sections", []), ensure_ascii=False)[:1000])